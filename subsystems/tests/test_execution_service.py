"""S3.1 FR-EX-002 执行服务测试（tmp 库 hermetic，hostless 假 runner）。

覆盖：提交幂等/先账本后队列、重跑配额（拒入不留幻影/崩溃重放不双扣）、
取消检查点、超时终止+cancel 回调、适配器异常收口、崩溃租约回收（重领/死信）、
stub runner 拒绝、worker 并发循环统计、未收尾状态契约守卫。
"""

from __future__ import annotations

import asyncio
import types
from datetime import datetime, timedelta, timezone

import pytest

from testpilot_adapters.recorded_runner import RecordedTestRunner
from testpilot_adapters.store import SqliteQueue, bootstrap_store
from testpilot_core.enums import CaseStatus, RunStatus
from testpilot_core.errors import AdapterUnavailableError, NotFoundError
from testpilot_core.execution import run_id_for
from testpilot_core.models import (
    CaseResult,
    CaseSelector,
    EnvironmentInfo,
    TestExecutionRequest,
    TestRun,
)
from testpilot_workers import execution_service as svc
from testpilot_workers.execution_service import RerunQuotaExceededError

FIXTURES = __import__("pathlib").Path(__file__).parent / "fixtures"


def _expired_lease_iso() -> str:
    # 必须基于真实挂钟偏移：dequeue/回收都按真实 now 判期，冻结时钟会混入未来时间
    return (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()


class _Limits:
    daily_rerun_quota = 2
    request_timeout_s = 1
    max_concurrency = 1
    execution_max_attempts = 3
    execution_lease_seconds = 300


class Shim:
    """最小容器：真实 Store+SqliteQueue（tmp 库）+ 可替换 runner。"""

    def __init__(self, tmp_path, runner):
        store = bootstrap_store(tmp_path / "data")
        self._adapters = {
            "store": store,
            "queue": SqliteQueue(store.conn),
            "runner": runner,
        }
        self.config = types.SimpleNamespace(limits=_Limits())

    def get(self, name):
        return self._adapters.get(name)

    def close(self):
        self._adapters["store"].close()


def _req(request_id: str = "req-1", *, rerun_key: str | None = None,
         hint: str | None = None, idem: str | None = None) -> TestExecutionRequest:
    return TestExecutionRequest(
        request_id=request_id,
        idempotency_key=idem or f"idem-{request_id}",
        project="demo",
        selector=CaseSelector(test_ids=["T/t1"], fixture_hint=hint),
        commit_sha="deadbeef",
        daily_budget_key=rerun_key,
    )


def _passed_run(request: TestExecutionRequest) -> TestRun:
    return TestRun(
        run_id="adapter-internal",
        idempotency_key=request.idempotency_key,
        project=request.project,
        commit_sha=request.commit_sha,
        status=RunStatus.PASSED,
        environment=EnvironmentInfo(provider="junit-local", env_id="jdk21"),
        case_results=[CaseResult(
            external_case_id="T/t1", status=CaseStatus.PASSED, duration_ms=5)],
    )


class ScriptedRunner:
    def __init__(self, run_factory=_passed_run):
        self.run_factory = run_factory
        self.canceled: list[str] = []
        self.submit_calls = 0

    async def submit(self, request):
        self.submit_calls += 1
        self._last = request
        return "adapter-internal"

    async def get_run(self, run_id):
        return self.run_factory(self._last)

    async def cancel(self, run_id):
        self.canceled.append(run_id)


class SlowRunner:
    """submit 立即拿到外部句柄、get_run 轮询卡住——对齐 xcodebuild 子进程模型。"""

    def __init__(self, sleep_s: float):
        self.sleep_s = sleep_s
        self.canceled: list[str] = []

    async def submit(self, request):
        return "slow-1"

    async def get_run(self, run_id):
        await asyncio.sleep(self.sleep_s)
        raise AssertionError("超时路径不应再返回结果")

    async def cancel(self, run_id):
        self.canceled.append(run_id)


class ExplodingRunner:
    def __init__(self, exc: Exception):
        self.exc = exc

    async def submit(self, request):
        raise self.exc

    async def get_run(self, run_id):
        raise self.exc

    async def cancel(self, run_id):
        pass


# ── 提交：账本/幂等/配额 ─────────────────────────────────────────────────────


async def test_submit_persists_ledger_before_queue_and_is_idempotent(tmp_path):
    c = Shim(tmp_path, ScriptedRunner())
    try:
        result = await svc.submit_execution(c, _req())
        run_id = run_id_for("idem-req-1")
        assert result == {"run_id": run_id, "status": "queued", "replayed": False}
        row = c.get("store").get_test_run_row(run_id)
        assert row["status"] == "queued" and row["request_json"]
        jobs = c.get("store").conn.execute(
            "SELECT COUNT(*) n FROM jobs").fetchone()["n"]
        assert jobs == 1
        # 同幂等键重放：不新增 job/账本，返回 replayed
        replay = await svc.submit_execution(c, _req())
        assert replay["replayed"] is True and replay["run_id"] == run_id
        assert c.get("store").conn.execute(
            "SELECT COUNT(*) n FROM jobs").fetchone()["n"] == 1
    finally:
        c.close()


async def test_rerun_quota_denied_leaves_no_phantom_and_accounts(tmp_path):
    c = Shim(tmp_path, ScriptedRunner())
    try:
        key = "demo:2026-11-10"
        # 首次执行（无 rerun key）不占配额
        await svc.submit_execution(c, _req("first"))
        for i in (1, 2):
            await svc.submit_execution(c, _req(f"rerun-{i}", rerun_key=key))
        assert c.get("store").count_budget_today(key, "2026-11-10") == 2
        # 第三笔重跑被拒
        with pytest.raises(RerunQuotaExceededError):
            await svc.submit_execution(c, _req("rerun-3", rerun_key=key))
        # 拒入不留幻影：无 run 账本、无 job、预算不增
        assert c.get("store").count_budget_today(key, "2026-11-10") == 2
        assert c.get("store").get_test_run_row(run_id_for("idem-rerun-3")) is None
        assert c.get("store").conn.execute(
            "SELECT COUNT(*) n FROM jobs").fetchone()["n"] == 3
    finally:
        c.close()


async def test_budget_replay_after_crash_does_not_double_count(tmp_path):
    # 模拟崩溃在「预算已记账、run 账本未建」之间：同请求重提应放行且不双扣
    c = Shim(tmp_path, ScriptedRunner())
    try:
        from testpilot_core.execution import BudgetEntry

        key = "demo:2026-11-10"
        c.get("store").insert_budget_entry(BudgetEntry(
            budget_key=key, date="2026-11-10",
            request_id="rerun-1", run_id=run_id_for("idem-rerun-1")))
        await svc.submit_execution(c, _req("rerun-1", rerun_key=key))
        assert c.get("store").count_budget_today(key, "2026-11-10") == 1
    finally:
        c.close()


# ── worker 单轮：成功/取消/超时/异常 ─────────────────────────────────────────


async def test_run_once_recorded_adapter_full_ledger(tmp_path):
    runner = RecordedTestRunner(FIXTURES / "runs")
    c = Shim(tmp_path, runner)
    try:
        await svc.submit_execution(c, _req(hint="xcode_hostless"))
        summary = await svc.run_once(c)
        assert summary["outcome"] == RunStatus.FAILED.value  # 夹具内含失败用例
        row = c.get("store").get_test_run_row(summary["run_id"])
        assert row["status"] == "failed" and row["raw_json"]
        restored = c.get("store").row_to_test_run(row)
        assert restored.environment.provider == "ios-simulator"
        # 账本身份覆盖适配器内部 run_id
        assert restored.run_id == summary["run_id"]
        job = c.get("store").conn.execute(
            "SELECT status FROM jobs").fetchone()
        assert job["status"] == "done"
        assert await svc.run_once(c) is None  # 队列已空
    finally:
        c.close()


async def test_cancel_before_pickup_marks_run_canceled(tmp_path):
    c = Shim(tmp_path, ScriptedRunner())
    try:
        submitted = await svc.submit_execution(c, _req())
        ack = await svc.request_cancel(c, submitted["run_id"])
        assert ack["cancel_requested"] is True
        summary = await svc.run_once(c)
        assert summary["outcome"] == RunStatus.CANCELED.value
        row = c.get("store").get_test_run_row(submitted["run_id"])
        assert row["status"] == "canceled"
        assert c.get("runner").submit_calls == 0  # 没碰适配器
    finally:
        c.close()


async def test_cancel_unknown_run_is_not_found(tmp_path):
    c = Shim(tmp_path, ScriptedRunner())
    try:
        with pytest.raises(NotFoundError):
            await svc.request_cancel(c, "run-ghost")
    finally:
        c.close()


async def test_timeout_terminates_and_marks_timeout(tmp_path):
    c = Shim(tmp_path, SlowRunner(sleep_s=0.3))
    c.config.limits.request_timeout_s = 0.05
    try:
        await svc.submit_execution(c, _req())
        summary = await svc.run_once(c)
        assert summary["outcome"] == RunStatus.TIMEOUT.value
        assert c.get("runner").canceled == ["slow-1"]  # 尽力终止回调到位
        row = c.get("store").get_test_run_row(summary["run_id"])
        assert row["status"] == "timeout" and "timeout" in row["error"]
        job = c.get("store").conn.execute("SELECT * FROM jobs").fetchone()
        assert job["status"] == "failed" and "timeout" in job["error"]
    finally:
        c.close()


async def test_adapter_exception_marks_run_error_and_fails_job(tmp_path):
    c = Shim(tmp_path, ExplodingRunner(RuntimeError("xcodebuild not found")))
    try:
        await svc.submit_execution(c, _req())
        summary = await svc.run_once(c)
        assert summary["outcome"] == RunStatus.ERROR.value
        row = c.get("store").get_test_run_row(summary["run_id"])
        assert "xcodebuild not found" in row["error"]
        assert c.get("store").conn.execute(
            "SELECT status FROM jobs").fetchone()["status"] == "failed"
    finally:
        c.close()


async def test_stub_runner_rejected_then_recovered_after_reconfig(tmp_path):
    c = Shim(tmp_path, None)
    try:
        await svc.submit_execution(c, _req())
        with pytest.raises(AdapterUnavailableError):
            await svc.run_once(c)
        # job 仍挂 running 租约：worker 修好配置重启，先回收过期租约再重领
        c.get("store").conn.execute(
            "UPDATE jobs SET lease_expires_at=? WHERE status='running'",
            (_expired_lease_iso(),))
        c.get("store").conn.commit()
        decisions = await svc.recover_stale(c)
        assert decisions[0]["action"] == "requeue"
        c._adapters["runner"] = ScriptedRunner()
        summary = await svc.run_once(c)
        assert summary["outcome"] == RunStatus.PASSED.value
    finally:
        c.close()


async def test_unfinished_adapter_status_is_contract_error(tmp_path):
    c = Shim(tmp_path, ScriptedRunner(
        run_factory=lambda req: _passed_run(req).model_copy(
            update={"status": RunStatus.RUNNING})))
    try:
        await svc.submit_execution(c, _req())
        summary = await svc.run_once(c)
        assert summary["outcome"] == RunStatus.ERROR.value
        assert "未收尾状态" in c.get("store").get_test_run_row(
            summary["run_id"])["error"]
    finally:
        c.close()


# ── 崩溃回收与 worker 循环 ───────────────────────────────────────────────────


async def test_recover_requeues_expired_lease_and_run_ledger_resets(tmp_path):
    c = Shim(tmp_path, ScriptedRunner())
    try:
        submitted = await svc.submit_execution(c, _req())
        job = await c.get("queue").dequeue(["test_execution"])
        # worker 认领后把 run 置 running，随即被杀
        c.get("store").update_test_run(
            submitted["run_id"], status=RunStatus.RUNNING.value)
        c.get("store").conn.execute(
            "UPDATE jobs SET lease_expires_at=? WHERE id=?",
            (_expired_lease_iso(), job["job_id"]))
        c.get("store").conn.commit()

        decisions = await svc.recover_stale(c)
        assert decisions[0]["action"] == "requeue"
        assert c.get("store").get_test_run_row(
            submitted["run_id"])["status"] == "queued"
        # 重领后正常跑完
        summary = await svc.run_once(c)
        assert summary["outcome"] == RunStatus.PASSED.value
    finally:
        c.close()


async def test_recover_dead_letter_marks_run_error(tmp_path):
    c = Shim(tmp_path, ScriptedRunner())
    try:
        submitted = await svc.submit_execution(c, _req())
        job = await c.get("queue").dequeue(["test_execution"])
        c.get("store").update_test_run(
            submitted["run_id"], status=RunStatus.RUNNING.value)
        c.get("store").conn.execute(
            "UPDATE jobs SET attempts=3, lease_expires_at=? WHERE id=?",
            (_expired_lease_iso(), job["job_id"]))
        c.get("store").conn.commit()
        decisions = await svc.recover_stale(c)
        assert decisions[0]["action"] == "dead_letter"
        row = c.get("store").get_test_run_row(submitted["run_id"])
        assert row["status"] == "error" and "max_attempts" in row["error"]
    finally:
        c.close()


async def test_worker_loop_processes_three_jobs_with_stats(tmp_path):
    c = Shim(tmp_path, ScriptedRunner())
    try:
        for i in range(3):
            await svc.submit_execution(c, _req(f"req-{i}"))
        stats = await svc.run_worker(c, max_iterations=6, poll_interval=0)
        assert stats["processed"] == 3
        assert stats["by_outcome"] == {"passed": 3}
        assert c.get("store").conn.execute(
            "SELECT COUNT(*) n FROM jobs WHERE status='done'").fetchone()["n"] == 3
    finally:
        c.close()


async def test_worker_loop_empty_queue_without_jobs(tmp_path):
    c = Shim(tmp_path, ScriptedRunner())
    try:
        stats = await svc.run_worker(c, max_iterations=2, poll_interval=0)
        assert stats["processed"] == 0
    finally:
        c.close()
