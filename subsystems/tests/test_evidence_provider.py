"""StoreBackedEvidenceProvider 六 seam hermetic 测试（S3.5c，FR-EX-007）。

全程不起端口/模拟器/真实 git：账本用 tmp sqlite，git 走 tests/fixtures/git
录制夹具（真实演示仓录制，sha 跨机可复现），os_log 用同构 Fake 端口喂合成
NDJSON 形态条目。覆盖：重跑历史口径、日志查询缓存/族分类/降级、断言注入、
录制 ChangeSet → DiffSignal/变更记录、堆栈匹配、工厂装配。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from testpilot_adapters.recorded_git import RecordedGitRepository
from testpilot_adapters.store import bootstrap_store
from testpilot_core.attribution import AssertionCheck
from testpilot_core.enums import CaseStatus, FailureCategory, RunStatus
from testpilot_core.errors import AdapterUnavailableError
from testpilot_core.models import (
    CaseResult,
    CaseSelector,
    EnvironmentInfo,
    RepoRef,
    TestExecutionRequest,
    TestRun,
)
from testpilot_workers.attribution_service import AttributionService
from testpilot_workers.evidence_provider import (
    StoreBackedEvidenceProvider,
    classify_log_families,
)

_SUBSYSTEMS = Path(__file__).resolve().parents[1]
_GIT_FIXTURES = _SUBSYSTEMS / "tests" / "fixtures" / "git"
_BASE = "4a01f1fe4b442b0caa9a574ce51881e618564a7c"
_HEAD = "0719b0756b39c6b429d672df2f195abb954b413d"
_COMMIT = "deadbeef"
_CASE_ID = "AlarmKitSpikeTests/t2"


# ── 夹具与构造助手 ───────────────────────────────────────────────────────────


def _case(status: CaseStatus = CaseStatus.FAILED, *, stack_top: str = "",
          message: str = "") -> CaseResult:
    return CaseResult(
        external_case_id=_CASE_ID, status=status,
        stack_top=stack_top, message=message,
    )


def _run(*, cases: list[CaseResult] | None = None, commit: str = _COMMIT,
         with_window: bool = True, uncovered: list[str] | None = None) -> TestRun:
    now = datetime(2026, 11, 11, 2, 0, tzinfo=timezone.utc)
    return TestRun(
        run_id="run-current",
        idempotency_key="idem-current",
        project="demo",
        commit_sha=commit,
        status=RunStatus.FAILED,
        environment=EnvironmentInfo(provider="ios-simulator", env_id="sim-1"),
        started_at=now if with_window else None,
        finished_at=(now + timedelta(seconds=70)) if with_window else None,
        case_results=cases or [_case()],
        uncovered_layers=uncovered or ["ios.device", "signing"],
    )


def _seed_run(store, *, run_id: str, commit: str = _COMMIT, project: str = "demo",
              case_id: str = _CASE_ID, status: CaseStatus = CaseStatus.PASSED,
              raw: bool = True) -> None:
    """在账本里种一条历史 run（raw=False 模拟 queued/running 幻影行）。"""
    idem = f"idem-{run_id}"
    request = TestExecutionRequest(
        request_id=f"req-{run_id}", idempotency_key=idem, project=project,
        selector=CaseSelector(), commit_sha=commit,
    )
    store.insert_test_run_ledger(
        request=request, run_id=run_id, status=RunStatus.QUEUED.value)
    if not raw:
        return
    run = TestRun(
        run_id=run_id, idempotency_key=idem, project=project, commit_sha=commit,
        status=RunStatus.FAILED,
        environment=EnvironmentInfo(provider="junit-local", env_id="jdk21"),
        case_results=[CaseResult(external_case_id=case_id, status=status)],
    )
    store.update_test_run(
        run_id, status=RunStatus.FAILED.value, raw_json=run.model_dump_json())


class _FakeLogs:
    """与 OsLogQueryAdapter.query 同构的假端口；可注入条目或抛不可达。"""

    def __init__(self, entries: list[dict] | None = None, *, explode: bool = False):
        self._entries = entries or []
        self._explode = explode
        self.calls: list[dict] = []

    async def query(self, **kwargs):
        self.calls.append(kwargs)
        if self._explode:
            raise AdapterUnavailableError("simctl spawn failed rc=64")
        return {
            "run_id": kwargs["run_id"],
            "entries": list(self._entries),
            "bad_lines": 0,
            "query": {"predicate": kwargs["predicate"]},
        }


class _ExplodingScm:
    def __init__(self):
        self.calls = 0

    async def get_change_set(self, repo, base, head):
        self.calls += 1
        raise AdapterUnavailableError("录制夹具缺少 ChangeSet")


def _recorded_scm() -> RecordedGitRepository:
    repo = RepoRef(project="demo", repo_path=str(_GIT_FIXTURES),
                   default_branch="main")
    return RecordedGitRepository(repo, _GIT_FIXTURES)


@pytest.fixture
def store(tmp_path):
    s = bootstrap_store(tmp_path / "data")
    yield s
    s.close()


# ── seam 1：reruns 账本历史口径 ──────────────────────────────────────────────


async def test_reruns_current_observation_plus_terminal_history(store):
    _seed_run(store, run_id="run-pass", status=CaseStatus.PASSED)
    _seed_run(store, run_id="run-queued", raw=False)  # 幻影行不计入
    _seed_run(store, run_id="run-other-project", project="other")
    _seed_run(store, run_id="run-other-case", case_id="Someone/else")
    _seed_run(store, run_id="run-skipped", status=CaseStatus.SKIPPED)

    provider = StoreBackedEvidenceProvider(store)
    records = await provider.reruns(_case(), _run())

    assert [(r.run_id, r.status) for r in records] == [
        ("run-pass", CaseStatus.PASSED),       # 唯一合格历史观察
        ("run-current", CaseStatus.FAILED),    # 当前轮自身补在末尾
    ]
    assert all(r.commit_sha == _COMMIT for r in records)


async def test_reruns_history_drives_flaky_verdict(store):
    # 同 commit 历史通过 + 当前失败：flaky 规则的 passed+failed 双态齐了
    _seed_run(store, run_id="run-pass", status=CaseStatus.PASSED)
    verdict = await AttributionService(
        StoreBackedEvidenceProvider(store)
    ).attribute_case(_run(), _case())
    assert verdict.category == FailureCategory.FLAKY
    assert verdict.confidence == 0.9 and verdict.evidence_refs == ["ev-rerun"]


async def test_reruns_repeated_failure_with_defect_message_is_real_defect(store):
    _seed_run(store, run_id="run-fail", status=CaseStatus.FAILED)
    case = _case(message='XCTAssertEqual failed: ("1") is not equal to ("2")')
    verdict = await AttributionService(
        StoreBackedEvidenceProvider(store)
    ).attribute_case(_run(cases=[case]), case)
    assert verdict.category == FailureCategory.REAL_DEFECT
    assert verdict.confidence == 0.75


async def test_no_history_single_failure_stays_needs_human(store):
    verdict = await AttributionService(
        StoreBackedEvidenceProvider(store)
    ).attribute_case(_run(), _case())
    assert verdict.category == FailureCategory.NEEDS_HUMAN
    # 当前轮自身失败仍是账本可回溯观察（ev-rerun），但单观察达不到任何规则门槛
    assert verdict.evidence_refs == ["ev-rerun"]


# ── seam 3/5：os_log 信号与环境信号 ──────────────────────────────────────────


def test_classify_log_families_keyword_table():
    assert classify_log_families(
        {"subsystem": "com.apple.TCC", "category": "default",
         "message": "authorization prompt denied"}) == ["authorization_race"]
    assert classify_log_families(
        {"subsystem": "x", "category": "y", "message": "unsupported entitlement"}) \
        == ["capability_gap"]
    assert classify_log_families(
        {"subsystem": "x", "category": "y", "message": "app entered background"}) \
        == ["lifecycle_background"]
    assert classify_log_families(
        {"subsystem": "x", "category": "y", "message": "schedule timer fired"}) == []


async def test_log_signals_families_and_query_cache(store):
    entries = [
        {"timestamp": "2026-11-11T02:00:10", "subsystem": "com.apple.TCC",
         "category": "default", "message": "authorization prompt denied by TCC"},
        {"timestamp": "2026-11-11T02:00:20", "subsystem": "com.thames.x",
         "category": "scheduler", "message": "backup timer fired"},
    ]
    fake_logs = _FakeLogs(entries)
    provider = StoreBackedEvidenceProvider(
        store, logs=fake_logs,
        log_predicates=['subsystem == "x"', 'process == "AlarmKitSpike"'])

    signals = await provider.log_signals(_case(), _run())
    assert len(signals) == 2  # 每谓词一条，零命中才丢弃
    assert signals[0].query_ref == "oslog:run-current:1"
    assert signals[0].predicate == 'subsystem == "x"'
    assert signals[0].matched_count == 2
    assert signals[0].families == ["authorization_race"]

    env = await provider.environment_signals(_case(), _run())
    assert len(env) == 1 and env[0].family == "authorization_race"
    assert "authorization prompt denied" in env[0].detail
    assert env[0].uncovered_layers == ["ios.device", "signing"]
    # 双 seam + 多用例共用一次查询：两个谓词各只打一次端口
    assert len(fake_logs.calls) == 2
    assert fake_logs.calls[0]["run_id"] == "run-current"


async def test_log_skipped_without_time_window(store):
    fake_logs = _FakeLogs([{"timestamp": "2026-11-11T02:00:10",
                            "message": "authorization denied"}])
    provider = StoreBackedEvidenceProvider(
        store, logs=fake_logs, log_predicates=['subsystem == "x"'])
    run = _run(with_window=False)
    assert await provider.log_signals(_case(), run) == []
    assert await provider.environment_signals(_case(), run) == []
    assert fake_logs.calls == []  # 不可复现窗口不查


async def test_log_port_unavailable_fails_closed(store):
    provider = StoreBackedEvidenceProvider(
        store, logs=_FakeLogs(explode=True),
        log_predicates=['process == "x"'])
    assert await provider.log_signals(_case(), _run()) == []
    assert await provider.environment_signals(_case(), _run()) == []


async def test_no_logs_port_means_empty_log_evidence(store):
    provider = StoreBackedEvidenceProvider(store, log_predicates=['x == "y"'])
    assert await provider.log_signals(_case(), _run()) == []
    assert await provider.environment_signals(_case(), _run()) == []


# ── seam 4/6：git ChangeSet → diff / 变更记录 ─────────────────────────────────


async def test_recorded_change_set_routes_stack_hit_to_real_defect(store):
    provider = StoreBackedEvidenceProvider(
        store, repo=_recorded_scm().repo, scm=_recorded_scm(), diff_base=_BASE)
    run = _run(commit=_HEAD)
    case = _case(stack_top="C.keep()")

    diff = await provider.diff_signal(case, run)
    assert diff.base == _BASE and diff.head == _HEAD
    assert diff.changed_symbols == ["C", "C.fresh()", "C.keep()"]
    assert diff.stack_symbol == "C.keep()" and diff.stack_symbol_changed is True

    records = await provider.change_records(case, run)
    assert len(records) == 1 and records[0].ref == f"git:{_HEAD[:8]}"
    assert "C.keep()" in records[0].symbols

    verdict = await AttributionService(provider).attribute_case(run, case)
    assert verdict.category == FailureCategory.REAL_DEFECT
    assert verdict.confidence == 0.9 and verdict.evidence_refs == ["ev-diff"]


async def test_stack_miss_keeps_needs_human_but_change_record_remains(store):
    provider = StoreBackedEvidenceProvider(
        store, repo=_recorded_scm().repo, scm=_recorded_scm(), diff_base=_BASE)
    run = _run(commit=_HEAD)
    case = _case(stack_top="AlarmMissionTests.swift:88")  # 文件行号形态，保守不匹配

    diff = await provider.diff_signal(case, run)
    assert diff.stack_symbol_changed is False
    records = await provider.change_records(case, run)
    assert len(records) == 1  # 变更记录独立于堆栈命中，仍可供人审/产品改动规则

    verdict = await AttributionService(provider).attribute_case(run, case)
    assert verdict.category == FailureCategory.NEEDS_HUMAN


async def test_diff_unavailable_when_scm_or_base_missing(store):
    provider = StoreBackedEvidenceProvider(store)  # 无 scm、无 diff_base
    assert await provider.diff_signal(_case(), _run(commit=_HEAD)) is None
    assert await provider.change_records(_case(), _run(commit=_HEAD)) == []


async def test_scm_unavailable_fails_closed_and_is_not_cached(store):
    scm = _ExplodingScm()
    provider = StoreBackedEvidenceProvider(
        store, repo=_recorded_scm().repo, scm=scm, diff_base=_BASE)
    run = _run(commit=_HEAD)
    assert await provider.diff_signal(_case(), run) is None
    assert await provider.change_records(_case(), run) == []
    # None 不缓存：端口恢复后下个 run 自动重试（两次调用都真的打了端口）
    assert scm.calls == 2


# ── seam 2：断言注入（用例过期 / 产品改动二分）────────────────────────────────


async def test_assertion_source_without_change_record_is_case_outdated(store):
    async def assertion_source(case, run):
        return AssertionCheck(
            expected="90", actual_constant="30", matches=False,
            symbol_id="BackupInterval.s")

    provider = StoreBackedEvidenceProvider(store, assertion_source=assertion_source)
    verdict = await AttributionService(provider).attribute_case(_run(), _case())
    assert verdict.category == FailureCategory.CASE_OUTDATED
    assert verdict.confidence == 0.85


async def test_assertion_mismatch_plus_covering_change_record_is_product_change(store):
    async def assertion_source(case, run):
        return AssertionCheck(
            expected="keep()", actual_constant="fresh()", matches=False,
            symbol_id="C.keep()")

    provider = StoreBackedEvidenceProvider(
        store, repo=_recorded_scm().repo, scm=_recorded_scm(),
        diff_base=_BASE, assertion_source=assertion_source)
    # 堆栈刻意不命中变更符号：隔离断言+变更记录这条证据链，排除真缺陷候选
    run = _run(commit=_HEAD)
    case = _case(stack_top="OtherTests.swift:1")
    verdict = await AttributionService(provider).attribute_case(run, case)
    assert verdict.category == FailureCategory.PRODUCT_CHANGE
    assert verdict.confidence == 0.85
    assert {"ev-assert", "ev-change-1"} <= set(verdict.evidence_refs)


# ── 工厂装配 ─────────────────────────────────────────────────────────────────


def test_factory_wires_store_backed_provider(tmp_path):
    from testpilot_orchestrator.config_loader import load_profile
    from testpilot_orchestrator.factory import build_container
    from testpilot_workers.attribution_service import AttributionService

    config = load_profile(
        _SUBSYSTEMS / "config" / "profiles" / "offline-demo.yaml")
    config.paths["data_dir"] = str(tmp_path / "data")
    container = build_container(config, _SUBSYSTEMS)
    try:
        svc = container.get("attribution")
        assert isinstance(svc, AttributionService)
        provider = svc._provider
        assert isinstance(provider, StoreBackedEvidenceProvider)
        # offline-demo：scm=recorded 已接，logs=stub 为 None，未配 diff_base/谓词
        assert isinstance(provider._scm, RecordedGitRepository)
        assert provider._logs is None and provider._diff_base is None
        assert provider._predicates == []
    finally:
        container.get("store").close()
