"""S3.2 FR-EX-004 执行服务 × EnvironmentPort 集成测试（tmp 库 hermetic）。

覆盖"先租后跑"编排语义：
- 正常路径：acquire → udid/request_id 透传进 runner → 收尾必 release；
- 能力不支持/设备全忙：run 显式 error，runner 零调用，不留租约幻影；
- 超时/适配器异常：租约照样释放，设备不泄漏；
- release 自身失败不能掩盖执行结论；
- environment=stub（未装配）时走 S3.1 旧路径，请求对象不被改写。
"""

from __future__ import annotations

import asyncio
import types

import pytest

from testpilot_adapters.store import SqliteQueue, bootstrap_store
from testpilot_core.enums import CaseStatus, RunStatus
from testpilot_core.errors import (
    AdapterUnavailableError,
    UnsupportedCapabilityError,
)
from testpilot_core.models import (
    CaseResult,
    CaseSelector,
    EnvironmentInfo,
    EnvironmentRequirements,
    TestExecutionRequest,
    TestRun,
)
from testpilot_workers import execution_service as svc


class _Limits:
    daily_rerun_quota = 2
    request_timeout_s = 1
    max_concurrency = 1
    execution_max_attempts = 3
    execution_lease_seconds = 300


class Shim:
    def __init__(self, tmp_path, runner, environment=None):
        store = bootstrap_store(tmp_path / "data")
        self._adapters = {
            "store": store,
            "queue": SqliteQueue(store.conn),
            "runner": runner,
            "environment": environment,
        }
        self.config = types.SimpleNamespace(limits=_Limits())

    def get(self, name):
        return self._adapters.get(name)

    def close(self):
        self._adapters["store"].close()


class FakeEnvironment:
    """记录 acquire/release 的 EnvironmentPort 假件，可脚本化失败。"""

    def __init__(self, *, env_id="UDID-A", acquire_exc=None, release_exc=None):
        self.env_id = env_id
        self._acquire_exc = acquire_exc
        self._release_exc = release_exc
        self.acquired: list[EnvironmentRequirements] = []
        self.released: list[str] = []
        self._active: set[str] = set()

    async def acquire(self, requirements):
        self.acquired.append(requirements)
        if self._acquire_exc is not None:
            raise self._acquire_exc
        lease_id = f"envlease-{len(self.acquired)}"
        self._active.add(lease_id)
        return lease_id

    def env_id_for_lease(self, lease_id):
        if lease_id not in self._active:
            raise AdapterUnavailableError(f"未知租约: {lease_id}")
        return self.env_id

    async def health_check(self, env_id):
        return True

    async def reset(self, env_id):
        return None

    async def release(self, lease_id):
        self.released.append(lease_id)
        self._active.discard(lease_id)
        if self._release_exc is not None:
            raise self._release_exc

    @property
    def active_count(self):
        return len(self._active)


class RecordingRunner:
    """记下收到的 request（验证 labels 透传），默认产出 passed。"""

    def __init__(self, *, explode=None):
        self._explode = explode
        self.submit_calls = 0
        self.canceled: list[str] = []
        self.requests: list[TestExecutionRequest] = []

    async def submit(self, request):
        self.submit_calls += 1
        if self._explode is not None:
            raise self._explode
        self.requests.append(request)
        return "adapter-internal"

    async def get_run(self, run_id):
        request = self.requests[-1]
        return TestRun(
            run_id="adapter-internal",
            idempotency_key=request.idempotency_key,
            project=request.project,
            commit_sha=request.commit_sha,
            status=RunStatus.PASSED,
            environment=EnvironmentInfo(
                provider="ios-simulator",
                env_id=request.environment.labels.get("udid", "")),
            case_results=[CaseResult(
                external_case_id="T/t1", status=CaseStatus.PASSED, duration_ms=5)],
        )

    async def cancel(self, run_id):
        self.canceled.append(run_id)


class SlowRunner:
    def __init__(self, sleep_s):
        self.sleep_s = sleep_s
        self.canceled: list[str] = []

    async def submit(self, request):
        return "slow-1"

    async def get_run(self, run_id):
        await asyncio.sleep(self.sleep_s)
        raise AssertionError("超时路径不应再返回结果")

    async def cancel(self, run_id):
        self.canceled.append(run_id)


def _req(*, caps=("ios.simulator",), labels=None) -> TestExecutionRequest:
    return TestExecutionRequest(
        request_id="req-1",
        idempotency_key="idem-req-1",
        project="demo",
        selector=CaseSelector(test_ids=["AlarmKitSpikeTests/t1"]),
        commit_sha="deadbeef",
        environment=EnvironmentRequirements(
            capabilities=list(caps), labels=dict(labels or {})),
    )


async def test_happy_path_acquire_enriches_labels_and_releases(tmp_path):
    env = FakeEnvironment(env_id="UDID-A")
    runner = RecordingRunner()
    c = Shim(tmp_path, runner, env)
    try:
        await svc.submit_execution(
            c, _req(labels={"team": "qa"}))  # 既有 label 必须保留
        summary = await svc.run_once(c)
        assert summary["outcome"] == RunStatus.PASSED.value

        # acquire 收到的 requirements 已注入 request_id，能力要求原样透传
        assert len(env.acquired) == 1
        reqs = env.acquired[0]
        assert reqs.capabilities == ["ios.simulator"]
        assert reqs.labels["request_id"] == "req-1"
        assert reqs.labels["team"] == "qa"

        # runner 拿到的 request 带 udid（无租约不裸跑的唯一凭据）
        sent = runner.requests[0]
        assert sent.environment.labels["udid"] == "UDID-A"
        assert sent.environment.labels["request_id"] == "req-1"
        assert sent.environment.labels["team"] == "qa"

        # 收尾必释放且只释放一次，无残留租约
        assert env.released == ["envlease-1"] and env.active_count == 0
    finally:
        c.close()


async def test_unsupported_capability_marks_error_and_never_hard_runs(tmp_path):
    env = FakeEnvironment(acquire_exc=UnsupportedCapabilityError(
        "能力 background_audio 属真机独占层，ios-simulator 未覆盖层"))
    runner = RecordingRunner()
    c = Shim(tmp_path, runner, env)
    try:
        await svc.submit_execution(c, _req(caps=("background_audio",)))
        summary = await svc.run_once(c)
        assert summary["outcome"] == RunStatus.ERROR.value
        row = c.get("store").get_test_run_row(summary["run_id"])
        assert "UnsupportedCapabilityError" in row["error"]
        assert "未覆盖层" in row["error"]
        # 硬跑防线：runner 零调用，acquire 失败不留租约、不调 release
        assert runner.submit_calls == 0
        assert env.released == [] and env.active_count == 0
        assert c.get("store").conn.execute(
            "SELECT status FROM jobs").fetchone()["status"] == "failed"
    finally:
        c.close()


async def test_all_devices_busy_marks_error_without_submit(tmp_path):
    env = FakeEnvironment(acquire_exc=AdapterUnavailableError(
        "没有空闲模拟器（全部被占用），请稍后重试或扩容"))
    runner = RecordingRunner()
    c = Shim(tmp_path, runner, env)
    try:
        await svc.submit_execution(c, _req())
        summary = await svc.run_once(c)
        assert summary["outcome"] == RunStatus.ERROR.value
        assert "没有空闲模拟器" in c.get("store").get_test_run_row(
            summary["run_id"])["error"]
        assert runner.submit_calls == 0 and env.released == []
    finally:
        c.close()


async def test_adapter_unavailable_error_carries_runbook_hint(tmp_path):
    """E5 AC④：环境/适配器不可用时 run 与 job 错误都必须指向排障 runbook。"""
    env = FakeEnvironment(acquire_exc=AdapterUnavailableError(
        "没有空闲模拟器（全部被占用），请稍后重试或扩容"))
    runner = RecordingRunner()
    c = Shim(tmp_path, runner, env)
    try:
        await svc.submit_execution(c, _req())
        summary = await svc.run_once(c)
        row = c.get("store").get_test_run_row(summary["run_id"])
        assert "没有空闲模拟器" in row["error"]
        assert "docs/03 §3.6" in row["error"]
        # 队列死信原因同样带指引，巡检 jobs 表就能定位 runbook
        job = c.get("store").conn.execute(
            "SELECT error FROM jobs WHERE id=?", (summary["job_id"],)).fetchone()
        assert "docs/03 §3.6" in job["error"]
        assert runner.submit_calls == 0
    finally:
        c.close()


async def test_non_adapter_error_does_not_carry_runbook_hint(tmp_path):
    """负对照：普通业务异常不挂环境 runbook 指引，防止信号稀释。"""
    env = FakeEnvironment()
    runner = RecordingRunner(explode=RuntimeError("boom: 业务逻辑炸了"))
    c = Shim(tmp_path, runner, env)
    try:
        await svc.submit_execution(c, _req())
        summary = await svc.run_once(c)
        row = c.get("store").get_test_run_row(summary["run_id"])
        assert "RuntimeError: boom" in row["error"]
        assert "docs/03 §3.6" not in row["error"]
        # 适配器异常路径的租约释放纪律不受影响
        assert env.released == ["envlease-1"]
    finally:
        c.close()


async def test_timeout_still_releases_lease_and_cancels_runner(tmp_path):
    env = FakeEnvironment()
    c = Shim(tmp_path, SlowRunner(sleep_s=0.3), env)
    c.config.limits.request_timeout_s = 0.05
    try:
        await svc.submit_execution(c, _req())
        summary = await svc.run_once(c)
        assert summary["outcome"] == RunStatus.TIMEOUT.value
        assert c.get("runner").canceled == ["slow-1"]
        # 超时出口也必须还设备：租约不留，泄漏防线
        assert env.released == ["envlease-1"] and env.active_count == 0
    finally:
        c.close()


async def test_runner_exception_still_releases_lease(tmp_path):
    env = FakeEnvironment()
    runner = RecordingRunner(explode=RuntimeError("xcodebuild boom"))
    c = Shim(tmp_path, runner, env)
    try:
        await svc.submit_execution(c, _req())
        summary = await svc.run_once(c)
        assert summary["outcome"] == RunStatus.ERROR.value
        assert "xcodebuild boom" in c.get("store").get_test_run_row(
            summary["run_id"])["error"]
        assert env.released == ["envlease-1"] and env.active_count == 0
    finally:
        c.close()


async def test_release_failure_does_not_mask_success(tmp_path):
    # 还租约失败是回收侧问题，不能把一条已通过的 run 改写成 error
    env = FakeEnvironment(release_exc=RuntimeError("lease table locked"))
    runner = RecordingRunner()
    c = Shim(tmp_path, runner, env)
    try:
        await svc.submit_execution(c, _req())
        summary = await svc.run_once(c)
        assert summary["outcome"] == RunStatus.PASSED.value
        row = c.get("store").get_test_run_row(summary["run_id"])
        # error 列在账本里默认空串（非 NULL）：成功路径必须保持干净
        assert row["status"] == RunStatus.PASSED.value and row["error"] == ""
        assert runner.submit_calls == 1
    finally:
        c.close()


async def test_without_environment_adapter_request_flows_through_unchanged(tmp_path):
    # environment=stub 剖面：不 acquire、不注入 udid，S3.1 行为一字不动
    runner = RecordingRunner()
    c = Shim(tmp_path, runner, None)
    try:
        await svc.submit_execution(c, _req())
        summary = await svc.run_once(c)
        assert summary["outcome"] == RunStatus.PASSED.value
        sent = runner.requests[0]
        assert "udid" not in sent.environment.labels
        assert sent.environment.capabilities == ["ios.simulator"]
    finally:
        c.close()
