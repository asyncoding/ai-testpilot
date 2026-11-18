"""ExecutionServiceChildRun（H4 真实 ChildRunPort）hermetic 测试。

真 Store+SqliteQueue 落 tmp_path，SCM/runner 用假件，队列由测试注入的
sleeper 回调在端口轮询期间经 execution_service.run_once 排空——确定性建模
「另一进程的 runner worker 在排空队列」。覆盖：

① 提交即入队、返回时 run 必已终态（摘掉等待会立刻变红），请求全字段落
  request_json（含 launch_arguments 追溯字段、hybrid: 命名空间）；
② 同幂等键重放：同一 run_id、runner 只调一次、队列无第二笔 job；
③ failed→ChildRunFailedError；error→ChildRunUnavailableError（blocked 口径）；
④ scm 缺失 fail-loud；无人排空时等待超时带 runbook 提示；
⑤ 与 hybrid_runner 串通的集成测试：真实端口驱动 xcodebuild 步骤，
  失败按 failure_policy 分 abort/block 两姿态。
"""

from __future__ import annotations

import json
import types
from datetime import datetime, timezone
from typing import Any

import pytest

from testpilot_adapters.hybrid_session_store import FileHybridSessionStore
from testpilot_adapters.store import SqliteQueue, bootstrap_store
from testpilot_core.enums import (
    CaseStatus,
    RunStatus,
    SessionStatus,
    StepActor,
    StepStatus,
)
from testpilot_core.errors import AdapterUnavailableError
from testpilot_core.hybrid_plan import HybridPlanError, PlanStep, build_hybrid_plan
from testpilot_core.models import (
    CaseResult,
    EnvironmentInfo,
    Oracle,
    OracleType,
    TestCase,
    TestExecutionRequest,
    TestRun,
)
from testpilot_workers import execution_service as svc
from testpilot_workers.hybrid_child_run import ExecutionServiceChildRun
from testpilot_workers.hybrid_probes import ProbeContext
from testpilot_workers.hybrid_runner import (
    ChildRunFailedError,
    ChildRunRequest,
    ChildRunUnavailableError,
    drive_until_wait,
    start_session,
)

_UDID = "00008140-001A39590162801C"
_BUNDLE = "com.example.AlarmKitSpike"
_COMMIT = "0123456789abcdef0123456789abcdef01234567"
_CHILD_KEY = "s1|TC-A1|schedule"
_ONLY_TESTING = ["AlarmKitSpikeUITests/testLockRelay_schedule"]
_LAUNCH_ARGS = ["--ak-clear-all", "--ak-purge-dates", "--ak-accept-privacy"]


class FakeScm:
    def __init__(self, commit: str = _COMMIT) -> None:
        self._commit = commit
        self.calls = 0

    async def head_sha(self, repo=None) -> str:
        self.calls += 1
        return self._commit


class ScriptedRunner:
    """模拟 xcode runner：submit 记账，get_run 回指定终态的 TestRun。"""

    def __init__(self, status: RunStatus = RunStatus.PASSED, *,
                 error: str = "") -> None:
        self._status = status
        self._error = error
        self.submit_calls = 0
        self.last_request: TestExecutionRequest | None = None
        self.canceled: list[str] = []

    async def submit(self, request: TestExecutionRequest) -> str:
        self.submit_calls += 1
        self.last_request = request
        return "adapter-internal"

    async def get_run(self, run_id: str) -> TestRun:
        request = self.last_request
        assert request is not None
        # TestRun 无 error 列：失败文本随 CaseResult.message 落，worker 异常路径
        # 才写账本 error 列；这里模拟 runner 返回的终态 TestRun，不越模型字段。
        return TestRun(
            run_id="adapter-internal",
            idempotency_key=request.idempotency_key,
            project=request.project,
            commit_sha=request.commit_sha,
            status=self._status,
            environment=EnvironmentInfo(provider="ios-device", env_id=_UDID),
            case_results=[CaseResult(
                external_case_id=_ONLY_TESTING[0],
                status=CaseStatus.PASSED if self._status is RunStatus.PASSED
                else CaseStatus.FAILED,
                duration_ms=42,
                message=self._error)],
        )

    async def cancel(self, run_id: str) -> None:
        self.canceled.append(run_id)


class _Limits:
    daily_rerun_quota = 20
    request_timeout_s = 30
    max_concurrency = 1
    execution_max_attempts = 3
    execution_lease_seconds = 300


class Hermetic:
    """真实 store/queue/会话存储 + 假 scm/runner 的最小容器。"""

    def __init__(self, tmp_path, runner: ScriptedRunner, *,
                 scm: FakeScm | None = None) -> None:
        store = bootstrap_store(tmp_path / "data")
        self._adapters: dict[str, Any] = {
            "store": store,
            "queue": SqliteQueue(store.conn),
            "runner": runner,
            "scm": scm if scm is not None else FakeScm(),
            "hybrid_session_store": FileHybridSessionStore(tmp_path / "data"),
        }
        self.config = types.SimpleNamespace(
            project=types.SimpleNamespace(name="AlarmKitSpike"),
            limits=_Limits(),
        )

    def get(self, name: str):
        return self._adapters.get(name)

    def draining_sleeper(self):
        """首次轮询等待时排空一个队列 job，模拟独立 runner worker 进程。"""
        state = {"drained": False}

        async def _sleep(_delay: float) -> None:
            if not state["drained"]:
                state["drained"] = True
                await svc.run_once(self)

        return _sleep

    def port(self, **kwargs) -> ExecutionServiceChildRun:
        kwargs.setdefault("sleeper", self.draining_sleeper())
        kwargs.setdefault("poll_interval_s", 0.01)
        return ExecutionServiceChildRun(self, **kwargs)

    def wire_port(self, **kwargs) -> ExecutionServiceChildRun:
        """装配进容器（对齐 factory 的 hybrid_child_run 键），供 hybrid_runner 取用。"""
        port = self.port(**kwargs)
        self._adapters["hybrid_child_run"] = port
        return port


def _child_request() -> ChildRunRequest:
    return ChildRunRequest(
        only_testing=list(_ONLY_TESTING),
        launch_arguments=list(_LAUNCH_ARGS),
        idempotency_key=_CHILD_KEY)


# ── 1. 提交入队 + 终态守候 + 请求落账 ─────────────────────────────────────────


async def test_submit_waits_for_terminal_and_persists_full_request(tmp_path):
    box = Hermetic(tmp_path, ScriptedRunner())
    port = box.port()

    run_ref = await port.submit(_child_request())

    assert run_ref.startswith("run-")
    row = box.get("store").get_test_run_row(run_ref)
    # 红证守卫：返回时必须已是终态（若端口退化为只入队即返回，这里仍是 queued）
    assert row["status"] == RunStatus.PASSED.value
    persisted = TestExecutionRequest.model_validate_json(row["request_json"])
    assert persisted.selector.test_ids == _ONLY_TESTING
    assert persisted.launch_arguments == _LAUNCH_ARGS
    assert persisted.idempotency_key == f"hybrid:{_CHILD_KEY}"
    assert persisted.request_id == f"hybrid-req:{_CHILD_KEY}"
    assert persisted.project == "AlarmKitSpike"
    assert persisted.commit_sha == _COMMIT
    assert persisted.triggered_by == "agent"
    assert persisted.daily_budget_key is None  # 计划内首跑不占重跑配额
    # runner 实际收到的请求同样携带追溯参数（尽管 argv 不消费）
    assert box.get("runner").last_request.launch_arguments == _LAUNCH_ARGS
    assert box.get("scm").calls == 1


async def test_submit_returns_run_ref_from_ledger(tmp_path):
    box = Hermetic(tmp_path, ScriptedRunner())
    run_ref = await box.port().submit(_child_request())
    # 账本身份与执行服务内容寻址一致
    from testpilot_core.execution import run_id_for

    assert run_ref == run_id_for(f"hybrid:{_CHILD_KEY}")


# ── 2. 幂等重放 ──────────────────────────────────────────────────────────────


async def test_replay_same_key_returns_same_run_without_second_job(tmp_path):
    box = Hermetic(tmp_path, ScriptedRunner())
    port = box.port()

    first = await port.submit(_child_request())
    # 第二次：账本已终态，首轮询即返回；sleeper 不会再排空任何 job
    second = await port.submit(_child_request())

    assert first == second
    assert box.get("runner").submit_calls == 1
    assert await svc.run_once(box) is None  # 队列无残留第二笔


# ── 3. 失败分型 ──────────────────────────────────────────────────────────────


async def test_failed_run_raises_child_run_failed(tmp_path):
    box = Hermetic(tmp_path, ScriptedRunner(
        RunStatus.FAILED, error="XCTAssertEqual failed: relay page missing"))
    with pytest.raises(ChildRunFailedError, match="产品失败"):
        await box.port().submit(_child_request())


async def test_error_run_raises_child_run_unavailable(tmp_path):
    box = Hermetic(tmp_path, ScriptedRunner(
        RunStatus.ERROR, error="xcodebuild exited 65"))
    with pytest.raises(ChildRunUnavailableError, match="基础设施收口"):
        await box.port().submit(_child_request())
    assert isinstance(ChildRunUnavailableError(), AdapterUnavailableError)


async def test_timeout_status_maps_to_unavailable(tmp_path):
    box = Hermetic(tmp_path, ScriptedRunner(RunStatus.TIMEOUT))
    with pytest.raises(ChildRunUnavailableError, match="timeout"):
        await box.port().submit(_child_request())


# ── 4. 装配缺口与等待超时 fail-loud ───────────────────────────────────────────


async def test_missing_scm_fail_loud(tmp_path):
    box = Hermetic(tmp_path, ScriptedRunner(), scm=None)
    # 显式覆盖：Hermetic 默认装了假 scm，这里摘掉模拟 stub profile
    box._adapters["scm"] = None
    with pytest.raises(HybridPlanError, match="scm"):
        await box.port().submit(_child_request())


async def test_wait_timeout_without_drainer_carries_runbook_hint(tmp_path):
    box = Hermetic(tmp_path, ScriptedRunner())

    async def _no_sleep(_delay: float) -> None:
        return None

    port = ExecutionServiceChildRun(
        box, max_wait_s=0.05, poll_interval_s=0.02, sleeper=_no_sleep)
    with pytest.raises(ChildRunUnavailableError,
                       match="testpilot-runner worker"):
        await port.submit(_child_request())
    # 超时只挂起会话口径，账本行仍是 queued，不伪造终态
    row = box.get("store").get_test_run_by_idempotency(
        f"hybrid:{_CHILD_KEY}")
    assert row["status"] == RunStatus.QUEUED.value


# ── 5. 与 hybrid_runner 串通的集成测试 ────────────────────────────────────────


def _case() -> TestCase:
    return TestCase(
        urn="urn:testpilot:fs-cases:test_case:cases.md#TC-A1",
        external_id="TC-A1", title="锁屏响铃侧键接力",
        module="RoutineAlarm", priority="P0",
        steps=["清场排钟", "人工锁屏", "到点响铃", "侧键进接力页"],
        oracles=[Oracle(type=OracleType.LOG_ASSERTION,
                        expectation="os_log 响铃事件链", confidence=0.9)],
        lifecycle="active", exploratory=False)


def _single_step_plan(*, failure_policy: str = "abort_session") -> dict:
    return build_hybrid_plan(
        case=_case(), profile="ios-local-poc-device", project="AlarmKitSpike",
        steps=[PlanStep(
            step_id="s1-purge-and-schedule",
            actor=StepActor.AUTO_XCODEBUILD, action="run_selector",
            params={"only_testing": _ONLY_TESTING},
            idempotency_key=_CHILD_KEY,
            failure_policy=failure_policy)],  # type: ignore[arg-type]
        now=datetime.now(timezone.utc))


async def test_hybrid_runner_drives_xcode_step_via_real_port(tmp_path):
    box = Hermetic(tmp_path, ScriptedRunner())
    box.wire_port()
    record = start_session(box, _single_step_plan())
    record = await drive_until_wait(
        box, record.session_id, ProbeContext(udid=_UDID, bundle_id=_BUNDLE))

    assert record.checkpoint.status is SessionStatus.PASSED
    assert record.checkpoint.step_statuses["s1-purge-and-schedule"] \
        is StepStatus.COMPLETED
    completed = [e for e in record.ledger if e["type"] == "auto_step_completed"]
    assert len(completed) == 1
    assert completed[0]["detail"]["run_ref"].startswith("run-")
    assert completed[0]["detail"]["only_testing"] == _ONLY_TESTING


async def test_hybrid_runner_child_failure_aborts_session(tmp_path):
    box = Hermetic(tmp_path, ScriptedRunner(RunStatus.FAILED, error="boom"))
    box.wire_port()
    record = start_session(box, _single_step_plan())
    record = await drive_until_wait(
        box, record.session_id, ProbeContext(udid=_UDID, bundle_id=_BUNDLE))

    assert record.checkpoint.status is SessionStatus.FAILED
    assert record.checkpoint.step_statuses["s1-purge-and-schedule"] \
        is StepStatus.FAILED


async def test_hybrid_runner_child_failure_block_policy_suspends(tmp_path):
    box = Hermetic(tmp_path, ScriptedRunner(RunStatus.FAILED, error="boom"))
    box.wire_port()
    record = start_session(box, _single_step_plan(
        failure_policy="block_for_human"))
    record = await drive_until_wait(
        box, record.session_id, ProbeContext(udid=_UDID, bundle_id=_BUNDLE))

    assert record.checkpoint.status is SessionStatus.BLOCKED
    assert record.checkpoint.step_statuses["s1-purge-and-schedule"] \
        is StepStatus.BLOCKED


async def test_hybrid_runner_infra_error_blocks_not_fails(tmp_path):
    box = Hermetic(tmp_path, ScriptedRunner(RunStatus.ERROR, error="rc=65"))
    box.wire_port()
    record = start_session(box, _single_step_plan())
    record = await drive_until_wait(
        box, record.session_id, ProbeContext(udid=_UDID, bundle_id=_BUNDLE))

    assert record.checkpoint.status is SessionStatus.BLOCKED
    assert record.checkpoint.step_statuses["s1-purge-and-schedule"] \
        is StepStatus.BLOCKED
    # 基础设施失败的 blocked 理由经 AdapterUnavailable 分支落账本（产品失败不进此路）
    blocked = [e for e in record.ledger if e["type"] == "step_blocked"]
    assert blocked and "基础设施收口" in json.dumps(blocked, ensure_ascii=False)
