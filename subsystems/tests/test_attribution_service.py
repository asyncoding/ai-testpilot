"""AttributionService 组包服务 hermetic 测试（S3.4，全程假件无真实端口）。"""

from __future__ import annotations

import pytest

from testpilot_core.attribution import (
    AssertionCheck,
    ChangeRecord,
    DiffSignal,
    EnvironmentSignal,
    LogSignal,
    RerunRecord,
)
from testpilot_core.enums import CaseStatus, FailureCategory, RunStatus
from testpilot_core.models import (
    CaseResult,
    EnvironmentInfo,
    TestRun,
)
from testpilot_workers.attribution_service import (
    AttributionService,
    NullEvidenceProvider,
)

COMMIT = "abc1234"


def _case(status: CaseStatus = CaseStatus.FAILED, message: str = "") -> CaseResult:
    return CaseResult(
        external_case_id="AlarmKitSpikeTests/testKillBackup()",
        status=status,
        message=message,
        stack_top="ReminderScheduler.backupFallback()",
    )


def _run(cases: list[CaseResult] | None = None) -> TestRun:
    return TestRun(
        run_id="run-1",
        idempotency_key="idem-1",
        project="demo",
        commit_sha=COMMIT,
        status=RunStatus.FAILED,
        environment=EnvironmentInfo(provider="ios-simulator", env_id="sim-1"),
        case_results=cases or [],
        uncovered_layers=["ios.device", "codesigning"],
    )


class FakeProvider:
    def __init__(self, **overrides) -> None:
        self.overrides = overrides
        self.calls: list[tuple[str, str]] = []

    async def reruns(self, case, run):
        self.calls.append(("reruns", case.external_case_id))
        return self.overrides.get("reruns", [])

    async def assertion_check(self, case, run):
        return self.overrides.get("assertion")

    async def log_signals(self, case, run):
        return self.overrides.get("log_signals", [])

    async def diff_signal(self, case, run):
        return self.overrides.get("diff")

    async def environment_signals(self, case, run):
        return self.overrides.get("environment", [])

    async def change_records(self, case, run):
        return self.overrides.get("change_records", [])


@pytest.mark.asyncio
async def test_null_provider_yields_needs_human():
    service = AttributionService()
    assert isinstance(service._provider, NullEvidenceProvider)
    verdict = await service.attribute_case(_run(), _case())
    assert verdict.category == FailureCategory.NEEDS_HUMAN
    assert verdict.evidence_refs == []  # 无证据即无账本


@pytest.mark.asyncio
async def test_flaky_packet_assembled_and_attributed():
    provider = FakeProvider(
        reruns=[
            RerunRecord(run_id="run-1", commit_sha=COMMIT, status=CaseStatus.FAILED),
            RerunRecord(run_id="run-2", commit_sha=COMMIT, status=CaseStatus.PASSED),
        ]
    )
    verdict = await AttributionService(provider).attribute_case(_run(), _case())
    assert verdict.category == FailureCategory.FLAKY
    assert verdict.evidence_refs == ["ev-rerun"]
    assert ("reruns", "AlarmKitSpikeTests/testKillBackup()") in provider.calls


@pytest.mark.asyncio
async def test_attribute_run_only_failed_and_error():
    failed_case = _case(CaseStatus.FAILED)
    error_case = CaseResult(
        external_case_id="AlarmKitSpikeTests/testErrorPath()",
        status=CaseStatus.ERROR,
        message="XCTAssertTrue failed",
    )
    cases = [
        _case(CaseStatus.PASSED),
        failed_case,
        _case(CaseStatus.SKIPPED),
        error_case,
    ]
    verdicts = await AttributionService(FakeProvider()).attribute_run(_run(cases))
    assert len(verdicts) == 2
    assert {v.case_id for v in verdicts} == {
        failed_case.external_case_id,
        error_case.external_case_id,
    }


@pytest.mark.asyncio
async def test_attributing_passed_case_rejected():
    with pytest.raises(ValueError, match="不进归因流程"):
        await AttributionService().attribute_case(_run(), _case(CaseStatus.PASSED))


@pytest.mark.asyncio
async def test_uncovered_layers_pass_through():
    verdict = await AttributionService(FakeProvider()).attribute_case(_run(), _case())
    assert verdict.uncovered_layers == ["ios.device", "codesigning"]


@pytest.mark.asyncio
async def test_full_evidence_packet_ledger_complete():
    provider = FakeProvider(
        assertion=AssertionCheck(
            expected="90", actual_constant="30", matches=False, symbol_id="BackupInterval.s"
        ),
        change_records=[
            ChangeRecord(ref="CR-7", summary="90s→30s", symbols=["BackupInterval.s"])
        ],
        log_signals=[
            LogSignal(
                query_ref="q1", predicate='subsystem == "x"',
                matched_count=3, families=["authorization_race"],
            )
        ],
        environment=[
            EnvironmentSignal(family="authorization_race", detail="授权弹窗竞态")
        ],
    )
    verdict = await AttributionService(
        provider
    ).attribute_case(_run(), _case())
    assert verdict.category == FailureCategory.PRODUCT_CHANGE
    assert {"ev-assert", "ev-change-1"} <= set(verdict.evidence_refs)
    # 环境候选同样开火（服务为 env/log 信号配了账本，否则引擎校验会响），
    # 人审可在候选列表里看到未被裁决采用的证据链
    env_candidate = next(
        c for c in verdict.candidates if c.category == FailureCategory.ENVIRONMENT
    )
    assert set(env_candidate.evidence_refs) == {"ev-env-1", "ev-log-1"}


@pytest.mark.asyncio
async def test_replicated_defect_uses_case_message():
    provider = FakeProvider(
        reruns=[
            RerunRecord(run_id="run-1", commit_sha=COMMIT, status=CaseStatus.FAILED),
            RerunRecord(run_id="run-2", commit_sha=COMMIT, status=CaseStatus.FAILED),
        ]
    )
    verdict = await AttributionService(provider).attribute_case(
        _run(), _case(message="XCTAssertEqual failed: (\"1\") is not equal to (\"2\")")
    )
    assert verdict.category == FailureCategory.REAL_DEFECT
    assert verdict.confidence == 0.75


@pytest.mark.asyncio
async def test_diff_provider_routes_to_real_defect():
    provider = FakeProvider(
        diff=DiffSignal(
            base="111aaaa", head="222bbbb",
            changed_symbols=["ReminderScheduler.backupFallback()"],
            stack_symbol="ReminderScheduler.backupFallback()",
            stack_symbol_changed=True,
        )
    )
    verdict = await AttributionService(provider).attribute_case(_run(), _case())
    assert verdict.category == FailureCategory.REAL_DEFECT
    assert verdict.confidence == 0.9
    assert verdict.evidence_refs == ["ev-diff"]
