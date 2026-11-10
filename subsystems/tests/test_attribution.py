"""FR-EX-007 归因引擎纯逻辑测试（S3.4，零 IO）。"""

from __future__ import annotations

import pytest

from testpilot_core.attribution import (
    ATTRIBUTION_RULE_VERSION,
    CONFIDENCE_FLOOR,
    AssertionCheck,
    AttributionCandidate,
    ChangeRecord,
    DiffSignal,
    EnvironmentSignal,
    EvidenceItem,
    EvidenceKind,
    FailureEvidence,
    LogSignal,
    RerunRecord,
    attribute_failure,
)
from testpilot_core.enums import CaseStatus, FailureCategory
from testpilot_core.errors import SchemaValidationError

COMMIT = "abc1234def56"


def make_evidence(
    case_id: str = "Case/Tests/test1()",
    *,
    commit: str = COMMIT,
    reruns: list[RerunRecord] | None = None,
    assertion: AssertionCheck | None = None,
    log_signals: list[LogSignal] | None = None,
    diff: DiffSignal | None = None,
    environment: list[EnvironmentSignal] | None = None,
    change_records: list[ChangeRecord] | None = None,
    uncovered_layers: list[str] | None = None,
    message: str = "",
    stack_top: str = "",
) -> FailureEvidence:
    """按结构化信号自动配同源账本条目，保证 ref 可回溯。"""
    ledger: list[EvidenceItem] = []
    if reruns:
        ledger.append(
            EvidenceItem(ref="ev-rerun", kind=EvidenceKind.RERUN, summary="历史重跑观察")
        )
    if assertion is not None:
        ledger.append(
            EvidenceItem(ref="ev-assert", kind=EvidenceKind.ASSERTION, summary="断言对照")
        )
    if log_signals:
        for i, sig in enumerate(log_signals, 1):
            ledger.append(
                EvidenceItem(
                    ref=f"ev-log-{i}",
                    kind=EvidenceKind.LOG,
                    summary=f"os_log 查询 {sig.query_ref}",
                    data={"families": sig.families},
                )
            )
    if diff is not None:
        ledger.append(
            EvidenceItem(ref="ev-diff", kind=EvidenceKind.DIFF, summary="近期符号 diff")
        )
    if environment:
        for i, sig in enumerate(environment, 1):
            ledger.append(
                EvidenceItem(
                    ref=f"ev-env-{i}",
                    kind=EvidenceKind.ENVIRONMENT,
                    summary=sig.detail,
                    data={"family": sig.family},
                )
            )
    if change_records:
        for i, record in enumerate(change_records, 1):
            ledger.append(
                EvidenceItem(
                    ref=f"ev-change-{i}",
                    kind=EvidenceKind.CHANGE_RECORD,
                    summary=record.summary,
                )
            )
    return FailureEvidence(
        case_id=case_id,
        commit_sha=commit,
        message=message,
        stack_top=stack_top,
        reruns=reruns or [],
        assertion=assertion,
        log_signals=log_signals or [],
        diff=diff,
        environment=environment or [],
        change_records=change_records or [],
        uncovered_layers=uncovered_layers or [],
        ledger=ledger,
    )


# ── flaky ──────────────────────────────────────────────────────────────────


def test_flaky_same_commit_pass_and_fail():
    ev = make_evidence(
        reruns=[
            RerunRecord(run_id="run-1", commit_sha=COMMIT, status=CaseStatus.FAILED),
            RerunRecord(run_id="run-2", commit_sha=COMMIT, status=CaseStatus.PASSED),
        ]
    )
    verdict = attribute_failure(ev)
    assert verdict.category == FailureCategory.FLAKY
    assert verdict.confidence == 0.9
    assert "ev-rerun" in verdict.evidence_refs


def test_flaky_needs_two_observations():
    ev = make_evidence(
        reruns=[RerunRecord(run_id="run-1", commit_sha=COMMIT, status=CaseStatus.FAILED)]
    )
    # 单次失败不构成 flaky，且没有别的正向证据 → needs_human
    verdict = attribute_failure(ev)
    assert verdict.category == FailureCategory.NEEDS_HUMAN


def test_all_failures_same_commit_are_not_flaky():
    ev = make_evidence(
        message="XCTAssertEqual failed: (\"1\") is not equal to (\"2\")",
        reruns=[
            RerunRecord(run_id="run-1", commit_sha=COMMIT, status=CaseStatus.FAILED),
            RerunRecord(run_id="run-2", commit_sha=COMMIT, status=CaseStatus.FAILED),
        ],
    )
    verdict = attribute_failure(ev)
    assert verdict.category == FailureCategory.REAL_DEFECT
    assert verdict.confidence == 0.75


def test_flaky_requires_same_commit():
    ev = make_evidence(
        reruns=[
            RerunRecord(run_id="run-1", commit_sha=COMMIT, status=CaseStatus.FAILED),
            RerunRecord(run_id="run-2", commit_sha="other999", status=CaseStatus.PASSED),
        ]
    )
    verdict = attribute_failure(ev)
    assert verdict.category == FailureCategory.NEEDS_HUMAN
    assert all(c.category != FailureCategory.FLAKY for c in verdict.candidates)


# ── 用例过期 / 产品改动 ──────────────────────────────────────────────────────


def test_case_outdated_on_assertion_mismatch():
    ev = make_evidence(
        assertion=AssertionCheck(
            expected="90", actual_constant="30", matches=False, symbol_id="BackupInterval.s"
        )
    )
    verdict = attribute_failure(ev)
    assert verdict.category == FailureCategory.CASE_OUTDATED
    assert verdict.confidence == 0.85
    assert verdict.evidence_refs == ["ev-assert"]


def test_product_change_when_record_covers_symbol():
    ev = make_evidence(
        assertion=AssertionCheck(
            expected="90", actual_constant="30", matches=False, symbol_id="BackupInterval.s"
        ),
        change_records=[
            ChangeRecord(
                ref="CR-7", summary="kill backup 90s→30s", symbols=["BackupInterval.s"]
            )
        ],
    )
    verdict = attribute_failure(ev)
    assert verdict.category == FailureCategory.PRODUCT_CHANGE
    assert set(verdict.evidence_refs) == {"ev-assert", "ev-change-1"}


def test_unrelated_change_record_still_case_outdated():
    ev = make_evidence(
        assertion=AssertionCheck(
            expected="90", actual_constant="30", matches=False, symbol_id="BackupInterval.s"
        ),
        change_records=[
            ChangeRecord(ref="CR-8", summary="改了别的", symbols=["Unrelated.symbol"])
        ],
    )
    verdict = attribute_failure(ev)
    assert verdict.category == FailureCategory.CASE_OUTDATED


def test_matching_assertion_produces_no_candidate():
    ev = make_evidence(
        assertion=AssertionCheck(expected="30", actual_constant="30", matches=True)
    )
    verdict = attribute_failure(ev)
    assert verdict.category == FailureCategory.NEEDS_HUMAN
    assert verdict.candidates == []


# ── 环境失败 ────────────────────────────────────────────────────────────────


def test_environment_single_signal_confidence():
    ev = make_evidence(
        environment=[
            EnvironmentSignal(family="capability_gap", detail="模拟器不暴露后台音频")
        ]
    )
    verdict = attribute_failure(ev)
    assert verdict.category == FailureCategory.ENVIRONMENT
    assert verdict.confidence == 0.7


def test_environment_confidence_growth_and_cap():
    ev = make_evidence(
        environment=[
            EnvironmentSignal(family="authorization_race", detail="授权竞态"),
            EnvironmentSignal(family="capability_gap", detail="能力缺口"),
        ]
    )
    assert attribute_failure(ev).confidence == 0.77

    ev_many = make_evidence(
        environment=[
            EnvironmentSignal(family=f"f{i}", detail="d") for i in range(4)
        ]
    )
    assert attribute_failure(ev_many).confidence == 0.85  # 0.7+3*0.07=0.91 触顶


def test_environment_pulls_same_family_log_evidence():
    ev = make_evidence(
        environment=[
            EnvironmentSignal(family="authorization_race", detail="授权弹窗竞态")
        ],
        log_signals=[
            LogSignal(
                query_ref="q1",
                predicate='subsystem == "com.thames.alarmkitspike"',
                matched_count=12,
                families=["authorization_race"],
            )
        ],
    )
    verdict = attribute_failure(ev)
    assert verdict.category == FailureCategory.ENVIRONMENT
    assert "ev-env-1" in verdict.evidence_refs
    assert "ev-log-1" in verdict.evidence_refs


# ── 真缺陷 ──────────────────────────────────────────────────────────────────


def test_real_defect_stack_symbol_in_diff():
    ev = make_evidence(
        stack_top="ReminderScheduler.backupFallback()",
        diff=DiffSignal(
            base="111aaaa",
            head="222bbbb",
            changed_symbols=["ReminderScheduler.backupFallback()"],
            stack_symbol="ReminderScheduler.backupFallback()",
            stack_symbol_changed=True,
        ),
    )
    verdict = attribute_failure(ev)
    assert verdict.category == FailureCategory.REAL_DEFECT
    assert verdict.confidence == 0.9
    assert verdict.evidence_refs == ["ev-diff"]


def test_replicated_defect_requires_defect_family_message():
    good = make_evidence(
        message="XCTAssertEqual failed: (\"90\") is not equal to (\"30\")",
        reruns=[
            RerunRecord(run_id="run-1", commit_sha=COMMIT, status=CaseStatus.FAILED),
            RerunRecord(run_id="run-2", commit_sha=COMMIT, status=CaseStatus.FAILED),
        ],
    )
    assert attribute_failure(good).category == FailureCategory.REAL_DEFECT

    weak_message = make_evidence(
        message="application launch stuck",
        reruns=[
            RerunRecord(run_id="run-1", commit_sha=COMMIT, status=CaseStatus.FAILED),
            RerunRecord(run_id="run-2", commit_sha=COMMIT, status=CaseStatus.FAILED),
        ],
    )
    assert attribute_failure(weak_message).category == FailureCategory.NEEDS_HUMAN


def test_single_failure_without_positive_evidence_is_needs_human():
    ev = make_evidence(message="XCTAssertTrue failed")
    verdict = attribute_failure(ev)
    assert verdict.category == FailureCategory.NEEDS_HUMAN
    assert "证据不足" in verdict.needs_human_reason


# ── 护栏与冲突 ──────────────────────────────────────────────────────────────


def test_flaky_tie_with_diff_defect_goes_needs_human():
    ev = make_evidence(
        reruns=[
            RerunRecord(run_id="run-1", commit_sha=COMMIT, status=CaseStatus.FAILED),
            RerunRecord(run_id="run-2", commit_sha=COMMIT, status=CaseStatus.PASSED),
        ],
        diff=DiffSignal(
            base="111aaaa",
            head="222bbbb",
            changed_symbols=["S.x()"],
            stack_symbol="S.x()",
            stack_symbol_changed=True,
        ),
    )
    verdict = attribute_failure(ev)
    assert verdict.category == FailureCategory.NEEDS_HUMAN
    cats = {c.category for c in verdict.candidates}
    assert FailureCategory.FLAKY in cats and FailureCategory.REAL_DEFECT in cats


def test_decisive_flaky_overrides_replicated_defect():
    # 缺陷复现置信 0.75，flaky 决定性证据 0.9，差 0.15 > 冲突区间 → flaky 赢
    ev = make_evidence(
        message="XCTAssertTrue failed",
        reruns=[
            RerunRecord(run_id="run-1", commit_sha=COMMIT, status=CaseStatus.FAILED),
            RerunRecord(run_id="run-2", commit_sha=COMMIT, status=CaseStatus.FAILED),
            RerunRecord(run_id="run-3", commit_sha=COMMIT, status=CaseStatus.PASSED),
        ],
    )
    verdict = attribute_failure(ev)
    assert verdict.category == FailureCategory.FLAKY


def test_guard_blocks_environment_closure_over_real_defect():
    # 复现缺陷 0.75 vs 环境顶格 0.85，差 0.1 落在冲突区间 → 不许收口环境，交人审
    ev = make_evidence(
        message="XCTAssertTrue failed",
        reruns=[
            RerunRecord(run_id="run-1", commit_sha=COMMIT, status=CaseStatus.FAILED),
            RerunRecord(run_id="run-2", commit_sha=COMMIT, status=CaseStatus.FAILED),
        ],
        environment=[
            EnvironmentSignal(family=f"f{i}", detail="d") for i in range(4)
        ],
    )
    verdict = attribute_failure(ev)
    assert verdict.category == FailureCategory.NEEDS_HUMAN
    assert "真缺陷" in verdict.needs_human_reason


def test_diff_defect_beats_environment_noise():
    # 缺陷案挂环境噪声日志是常见诱饵：0.9 vs 0.7，缺陷必须赢
    ev = make_evidence(
        stack_top="S.x()",
        diff=DiffSignal(
            base="111aaaa",
            head="222bbbb",
            changed_symbols=["S.x()"],
            stack_symbol="S.x()",
            stack_symbol_changed=True,
        ),
        environment=[
            EnvironmentSignal(family="authorization_race", detail="噪声信号")
        ],
    )
    verdict = attribute_failure(ev)
    assert verdict.category == FailureCategory.REAL_DEFECT


def test_outdated_beats_distant_environment_signal():
    ev = make_evidence(
        assertion=AssertionCheck(
            expected="90", actual_constant="30", matches=False, symbol_id="K.s"
        ),
        environment=[
            EnvironmentSignal(family="capability_gap", detail="模拟器能力缺口")
        ],
    )
    verdict = attribute_failure(ev)
    assert verdict.category == FailureCategory.CASE_OUTDATED


# ── 包校验 / 结构不变量 ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "kwargs",
    [
        {"case_id": "  "},
        {"commit": ""},
    ],
)
def test_blank_identity_rejected(kwargs):
    with pytest.raises(SchemaValidationError):
        attribute_failure(make_evidence(**kwargs))


def test_structured_signal_without_ledger_entry_rejected():
    ev = make_evidence(reruns=[RerunRecord(run_id="r", commit_sha=COMMIT, status=CaseStatus.FAILED)])
    ev.ledger = []  # 模拟组包漏记账本
    with pytest.raises(SchemaValidationError, match="rerun"):
        attribute_failure(ev)


def test_duplicate_ledger_refs_rejected():
    ev = make_evidence(
        assertion=AssertionCheck(expected="1", actual_constant="2", matches=False)
    )
    ev.ledger.append(ev.ledger[0].model_copy())
    with pytest.raises(SchemaValidationError, match="ref 重复"):
        attribute_failure(ev)


def test_evidence_refs_resolve_in_ledger():
    ev = make_evidence(
        assertion=AssertionCheck(expected="90", actual_constant="30", matches=False)
    )
    verdict = attribute_failure(ev)
    known = {item.ref for item in ev.ledger}
    assert set(verdict.evidence_refs) <= known


def test_verdict_stamps_rule_version_and_uncovered_layers():
    ev = make_evidence(
        uncovered_layers=["ios.device", "codesigning"],
        environment=[
            EnvironmentSignal(
                family="device_only", detail="真机能力", uncovered_layers=["ios.device"]
            )
        ],
    )
    verdict = attribute_failure(ev)
    assert verdict.rule_version == ATTRIBUTION_RULE_VERSION
    assert verdict.uncovered_layers == ["ios.device", "codesigning"]


def test_deterministic_and_sorted():
    ev = make_evidence(
        stack_top="S.x()",
        diff=DiffSignal(
            base="b",
            head="h",
            changed_symbols=["S.x()"],
            stack_symbol="S.x()",
            stack_symbol_changed=True,
        ),
        environment=[EnvironmentSignal(family="x", detail="d")],
    )
    first = attribute_failure(ev)
    second = attribute_failure(ev)
    assert first == second
    confs = [c.confidence for c in first.candidates]
    assert confs == sorted(confs, reverse=True)
    assert all(c.confidence >= CONFIDENCE_FLOOR for c in first.candidates)


def test_needs_human_returns_all_ledger_refs():
    ev = make_evidence(
        log_signals=[
            LogSignal(query_ref="q", predicate="p == 1", matched_count=0, families=[])
        ]
    )
    verdict = attribute_failure(ev)
    assert verdict.category == FailureCategory.NEEDS_HUMAN
    assert verdict.confidence == 0.0
    assert verdict.evidence_refs == ["ev-log-1"]
    assert verdict.candidates == []


def test_minimal_packet_is_needs_human_not_error():
    ev = FailureEvidence(case_id="c", commit_sha=COMMIT)
    verdict = attribute_failure(ev)
    assert verdict.category == FailureCategory.NEEDS_HUMAN
    assert isinstance(verdict.candidates, list)
