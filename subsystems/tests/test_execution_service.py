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
from testpilot_core.attribution import RerunRecord
from testpilot_core.enums import CaseStatus, FailureCategory, RunStatus
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
from testpilot_workers.attribution_service import (
    AttributionService,
    NullEvidenceProvider,
)
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
    """最小容器：真实 Store+SqliteQueue（tmp 库）+ 可替换 runner/归因服务。"""

    def __init__(self, tmp_path, runner, *, attribution=None):
        store = bootstrap_store(tmp_path / "data")
        self._adapters = {
            "store": store,
            "queue": SqliteQueue(store.conn),
            "runner": runner,
        }
        if attribution is not None:
            self._adapters["attribution"] = attribution
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


async def test_rerun_quota_denied_leaves_no_phantom_and_accounts(tmp_path, monkeypatch):
    # 冻结记账时钟：预算键含 UTC 日期，硬编码日期在跨日后会与服务端真实 today
    # 错位撞 request_id UNIQUE（2026-11-11 UTC 跨日实测变红），测试不得依赖运行当天
    monkeypatch.setattr(svc, "_today_utc", lambda: "2026-11-10")
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


async def test_budget_replay_after_crash_does_not_double_count(tmp_path, monkeypatch):
    # 模拟崩溃在「预算已记账、run 账本未建」之间：同请求重提应放行且不双扣
    # 同样冻结记账时钟，理由同 test_rerun_quota_denied_leaves_no_phantom_and_accounts
    monkeypatch.setattr(svc, "_today_utc", lambda: "2026-11-10")
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


# ── S3.5a FR-EX-007：verdict 随 run 落库（零 schema 变更，走 raw_json）────────


def _failed_run(request: TestExecutionRequest) -> TestRun:
    return TestRun(
        run_id="adapter-internal",
        idempotency_key=request.idempotency_key,
        project=request.project,
        commit_sha=request.commit_sha,
        status=RunStatus.FAILED,
        environment=EnvironmentInfo(provider="junit-local", env_id="jdk21"),
        case_results=[
            CaseResult(external_case_id="T/t1", status=CaseStatus.PASSED,
                       duration_ms=4),
            CaseResult(external_case_id="T/t2", status=CaseStatus.FAILED,
                       duration_ms=9, message="XCTAssertEqual failed"),
        ],
    )


class _FlakyProvider(NullEvidenceProvider):
    """同 commit 历史里 passed+failed 双态：flaky 规则的唯一充分证据。"""

    async def reruns(self, case, run):
        return [
            RerunRecord(run_id="older-pass", commit_sha=run.commit_sha,
                        status=CaseStatus.PASSED),
            RerunRecord(run_id="older-fail", commit_sha=run.commit_sha,
                        status=CaseStatus.FAILED),
        ]


async def test_failed_run_persists_needs_human_verdict_with_null_provider(tmp_path):
    c = Shim(tmp_path, ScriptedRunner(_failed_run),
             attribution=AttributionService())
    try:
        await svc.submit_execution(c, _req())
        summary = await svc.run_once(c)
        assert summary["outcome"] == RunStatus.FAILED.value

        detail = svc.get_run_detail(c, summary["run_id"])
        run = detail["run"]
        assert len(run["attributions"]) == 1  # 只归因失败用例，passed 的 T/t1 不入
        verdict = run["attributions"][0]
        assert verdict["case_id"] == "T/t2"
        assert verdict["category"] == FailureCategory.NEEDS_HUMAN.value
        assert verdict["rule_version"]  # 规则版本戳随结论落库

        # 列表投影带归因计数（S3.5 详情页/API 列表直接消费）
        listing = svc.list_runs(c)
        assert len(listing) == 1
        assert listing[0]["verdict_total"] == 1
        assert listing[0]["needs_human"] == 1
    finally:
        c.close()


async def test_passed_run_persists_empty_attribution_list(tmp_path):
    c = Shim(tmp_path, ScriptedRunner(_passed_run),
             attribution=AttributionService())
    try:
        await svc.submit_execution(c, _req())
        summary = await svc.run_once(c)
        detail = svc.get_run_detail(c, summary["run_id"])
        assert detail["run"]["attributions"] == []
        assert svc.list_runs(c)[0]["verdict_total"] == 0
    finally:
        c.close()


async def test_attribution_service_absent_keeps_legacy_behavior(tmp_path):
    # 手拼 Shim/老剖面不装 attribution：执行链路一字不动，字段回填空列表
    c = Shim(tmp_path, ScriptedRunner(_failed_run))
    try:
        await svc.submit_execution(c, _req())
        summary = await svc.run_once(c)
        row = c.get("store").get_test_run_row(summary["run_id"])
        restored = c.get("store").row_to_test_run(row)
        assert restored.attributions == []
    finally:
        c.close()


async def test_scripted_provider_verdict_flows_through_to_ledger(tmp_path):
    # 非 Null provider 的真实裁决（flaky 0.9）必须原样落库，证明接线不硬编码
    c = Shim(tmp_path, ScriptedRunner(_failed_run),
             attribution=AttributionService(_FlakyProvider()))
    try:
        await svc.submit_execution(c, _req())
        summary = await svc.run_once(c)
        verdict = svc.get_run_detail(c, summary["run_id"])["run"]["attributions"][0]
        assert verdict["category"] == FailureCategory.FLAKY.value
        assert verdict["confidence"] == 0.9
    finally:
        c.close()


async def test_replay_returns_same_run_without_re_attributing(tmp_path):
    c = Shim(tmp_path, ScriptedRunner(_failed_run),
             attribution=AttributionService())
    try:
        await svc.submit_execution(c, _req())
        await svc.run_once(c)
        replay = await svc.submit_execution(c, _req())
        assert replay["replayed"] is True
        # 重放不重新处理：jobs 仍 1 条、归因结论仍 1 条
        assert c.get("store").conn.execute(
            "SELECT COUNT(*) n FROM jobs").fetchone()["n"] == 1
        listing = svc.list_runs(c)
        assert len(listing) == 1 and listing[0]["verdict_total"] == 1
    finally:
        c.close()


async def test_get_run_detail_unknown_and_queued_projection(tmp_path):
    c = Shim(tmp_path, ScriptedRunner())
    try:
        with pytest.raises(NotFoundError):
            svc.get_run_detail(c, "run-ghost")
        submitted = await svc.submit_execution(c, _req())
        detail = svc.get_run_detail(c, submitted["run_id"])
        # queued 行尚无 raw_json：账本投影在、TestRun 为 None
        assert detail["status"] == RunStatus.QUEUED.value
        assert detail["run"] is None
    finally:
        c.close()


async def test_legacy_raw_json_without_attribution_hydrates_empty(tmp_path):
    # v15 老库行（S3.5 前写入的 raw_json 无 attributions 字段）回填兼容
    import json

    c = Shim(tmp_path, ScriptedRunner())
    try:
        await svc.submit_execution(c, _req())
        run_id = run_id_for("idem-req-1")
        legacy_payload = _passed_run(_req()).model_dump(mode="json")
        del legacy_payload["attributions"]  # 模拟 S3.5 之前的历史载荷
        legacy_run = json.dumps(legacy_payload, ensure_ascii=False)
        assert "attributions" not in legacy_run
        c.get("store").update_test_run(
            run_id, status=RunStatus.PASSED.value, raw_json=legacy_run)
        detail = svc.get_run_detail(c, run_id)
        assert detail["run"]["attributions"] == []
    finally:
        c.close()
