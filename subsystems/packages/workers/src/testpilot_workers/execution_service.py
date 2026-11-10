"""测试执行服务（docs/08 FR-EX-002，S3.1）。

独立 runner-worker 的全部编排语义都在这一层，core 管纯规则、adapters 管 IO：

- **submit**：先持久化配额账本与 run 账本，再 enqueue（外部副作用前先记账，
  崩溃只留可幂等重放的幻影记录）；同 idempotency_key 重放返回同一 run_id。
- **run_once / run_worker**：认领（租约）→ 取消检查点 → 状态机推进 →
  超时/异常隔离 → TestRun 落账 → ack/fail。orchestrator 进程永不直接拉
  xcodebuild，只经队列与本 worker 交互。
- **recover_stale**：worker 启动时回收过期租约，run 账本与 jobs 状态对齐。
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timezone
from typing import Any

from testpilot_core.enums import FailureCategory, RunStatus
from testpilot_core.errors import AdapterUnavailableError, NotFoundError, PolicyDeniedError
from testpilot_core.execution import (
    JOB_TYPE_TEST_EXECUTION,
    BudgetEntry,
    evaluate_rerun,
    is_rerun,
    run_id_for,
    transition_run_status,
)
from testpilot_core.models import TestExecutionRequest, TestRun


class RerunQuotaExceededError(PolicyDeniedError):
    """超每日重跑配额（FR-EX-002 AC2：拒绝并记账，HTTP/CLI 层据此分型）。"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today_utc() -> str:
    return datetime.now(timezone.utc).date().isoformat()


# 适配器返回的 run 状态 → worker 状态机事件
_FINISH_EVENTS = {
    RunStatus.PASSED: "finish_passed",
    RunStatus.FAILED: "finish_failed",
    RunStatus.ERROR: "finish_error",
    RunStatus.TIMEOUT: "timeout",
    RunStatus.CANCELED: "cancel",
}


# ── 提交侧（可被 API/CLI 调用；不直接执行任何测试）────────────────────────────


async def submit_execution(container, request: TestExecutionRequest) -> dict[str, Any]:
    """登记执行请求并入队；幂等重放返回同一 run_id。"""
    store, queue = container.get("store"), container.get("queue")
    run_id = run_id_for(request.idempotency_key)

    existing = store.get_test_run_by_idempotency(request.idempotency_key)
    if existing is not None:
        return {"run_id": existing["run_id"], "status": existing["status"],
                "replayed": True}

    # 1) 重跑配额：先判定，再把账本行落盘（先记账后副作用）
    if is_rerun(request):
        ledger = store.list_budget_entries(request.daily_budget_key, _today_utc())
        decision = evaluate_rerun(
            request_id=request.request_id,
            budget_key=request.daily_budget_key,
            date=_today_utc(),
            ledger=ledger,
            quota=container.config.limits.daily_rerun_quota,
        )
        if not decision.allowed:
            raise RerunQuotaExceededError(
                f"每日重跑配额已耗尽：key={request.daily_budget_key} "
                f"quota={container.config.limits.daily_rerun_quota}"
            )
        if not decision.already_counted:
            store.insert_budget_entry(
                BudgetEntry(
                    budget_key=request.daily_budget_key,
                    date=_today_utc(),
                    request_id=request.request_id,
                    run_id=run_id,
                )
            )

    # 2) run 账本（queued 幻影记录：此时还没派发，崩溃也能凭幂等键重放）
    store.insert_test_run_ledger(
        request=request, run_id=run_id, status=RunStatus.QUEUED.value
    )

    # 3) 最后才入队（队列是派发副作用的边界）
    await queue.enqueue(
        JOB_TYPE_TEST_EXECUTION,
        request.model_dump(mode="json"),
        request.idempotency_key,
    )
    return {"run_id": run_id, "status": RunStatus.QUEUED.value, "replayed": False}


async def request_cancel(container, run_id: str) -> dict[str, Any]:
    """打协作式取消标记；只对 queued/running 的 job 生效。"""
    store, queue = container.get("store"), container.get("queue")
    row = store.get_test_run_row(run_id)
    if row is None:
        raise NotFoundError(f"未知 run_id: {run_id}")
    marked = await queue.request_cancel(row["idempotency_key"])
    return {"run_id": run_id, "cancel_requested": marked, "status": row["status"]}


def _verdict_summary(store, row) -> dict[str, int]:
    """从 raw_json 读归因计数；queued/running 行尚无 TestRun，计 0。"""
    run = store.row_to_test_run(row)
    if run is None:
        return {"verdict_total": 0, "needs_human": 0}
    return {
        "verdict_total": len(run.attributions),
        "needs_human": sum(
            1 for v in run.attributions
            if v.category == FailureCategory.NEEDS_HUMAN
        ),
    }


def list_runs(
    container, *, status: str | None = None, project: str | None = None
) -> list[dict[str, Any]]:
    store = container.get("store")
    rows = store.list_test_runs(status=status, project=project)
    return [
        {
            "run_id": r["run_id"],
            "idempotency_key": r["idempotency_key"],
            "project": r["project"],
            "commit_sha": r["commit_sha"],
            "status": r["status"],
            "error": r["error"],
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
            **_verdict_summary(store, r),
        }
        for r in rows
    ]


def get_run_detail(container, run_id: str) -> dict[str, Any]:
    """单 run 全量详情：账本投影 + TestRun（用例/制品/归因/未覆盖层）。

    幽灵 run_id → NotFoundError（HTTP 层映射 404）；queued/running 行
    raw_json 为空时 run=None，调用方只看账本投影。
    """
    store = container.get("store")
    row = store.get_test_run_row(run_id)
    if row is None:
        raise NotFoundError(f"未知 run_id: {run_id}")
    run = store.row_to_test_run(row)
    return {
        "run_id": row["run_id"],
        "idempotency_key": row["idempotency_key"],
        "project": row["project"],
        "commit_sha": row["commit_sha"],
        "status": row["status"],
        "error": row["error"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "run": run.model_dump(mode="json") if run is not None else None,
    }


# ── worker 侧（独立进程；orchestrator 不调这里）──────────────────────────────


async def recover_stale(container) -> list[dict[str, str]]:
    """启动恢复：过期租约回收，并把 run 账本状态对齐回队/死信结论。"""
    store, queue = container.get("store"), container.get("queue")
    decisions = await queue.reclaim_stale(
        max_attempts=container.config.limits.execution_max_attempts
    )
    for decision in decisions:
        job = await queue.get_job(decision["job_id"])
        if job is None:
            continue
        row = store.get_test_run_by_idempotency(job["idempotency_key"])
        if row is None or row["status"] != RunStatus.RUNNING.value:
            continue  # 还没来得及置 running 就被杀：账本仍是 queued，无需回退
        if decision["action"] == "requeue":
            store.update_test_run(row["run_id"], status=RunStatus.QUEUED.value)
        else:
            store.update_test_run(
                row["run_id"],
                status=RunStatus.ERROR.value,
                error=f"reclaim: {decision['reason']}",
                finished_at=_now_iso(),
            )
    return decisions


async def _process_job(container, job: dict[str, Any]) -> dict[str, Any]:
    """认领之后的完整处理；任何异常都保证 job 不烂在 running、run 进终态。"""
    store, queue, runner = (
        container.get("store"), container.get("queue"), container.get("runner"))
    if runner is None:
        raise AdapterUnavailableError("runner 适配器未装配（profile runner=stub）")

    request = TestExecutionRequest.model_validate(job["payload"])
    run_id = run_id_for(request.idempotency_key)
    timeout_s = container.config.limits.request_timeout_s

    # 取消检查点（认领瞬间与队列标记双读，防 enqueue 与 cancel 的竞态漏检）
    if job.get("cancel_requested") or await queue.is_cancel_requested(job["job_id"]):
        status = transition_run_status(RunStatus.QUEUED, "cancel")
        store.update_test_run(
            run_id, status=status.value, error="canceled before pickup",
            finished_at=_now_iso())
        await queue.ack(job["job_id"])
        return {"job_id": job["job_id"], "run_id": run_id, "outcome": status.value}

    store.update_test_run(
        run_id, status=RunStatus.RUNNING.value, started_at=_now_iso())

    # S3.2 FR-EX-004：environment 适配器存在时"先租后跑"。acquire 内部先做
    # 能力判定——UnsupportedCapabilityError 与本块其他异常统一落到函数末尾的
    # 外层 except 收口为 run error + job fail，永远到不了 runner.submit（不硬跑）；
    # environment=stub 时本块整体跳过，S3.1 的 recorded/junit 路径行为一字不动。
    env = container.get("environment")
    lease_id = ""
    adapter_run_id = ""
    try:
        enriched_request = request
        if env is not None:
            try:
                requirements = request.environment.model_copy(update={
                    "labels": {
                        **request.environment.labels,
                        "request_id": request.request_id,
                    },
                })
                lease_id = await env.acquire(requirements)
                # 租约→具体设备（udid），runner 只认 labels 不自己挑设备
                env_id = env.env_id_for_lease(lease_id)
                enriched_request = request.model_copy(update={
                    "environment": requirements.model_copy(update={
                        "labels": {**requirements.labels, "udid": env_id},
                    }),
                })
            except BaseException:
                # acquire 成功后、enrichment 前出错（适配器契约违例等）也不留租约幻影
                if lease_id:
                    with contextlib.suppress(Exception):
                        await env.release(lease_id)
                raise

        try:
            try:
                adapter_run_id = await asyncio.wait_for(
                    runner.submit(enriched_request), timeout=timeout_s)
                adapter_run: TestRun = await asyncio.wait_for(
                    runner.get_run(adapter_run_id), timeout=timeout_s)
            finally:
                # 任何出口（成功/超时/适配器异常）都释放租约，防模拟器设备泄漏。
                # release 只是内存登记弹出（真正 shutdown 在 reclaim_expired），
                # 且必须在 cancel 通知前后都安全；释放失败不能掩盖执行结论。
                if lease_id:
                    with contextlib.suppress(Exception):
                        await env.release(lease_id)
        except asyncio.TimeoutError:
            # 尽力通知适配器终止（S3.2 xcode 适配器在此 kill 子进程）
            if adapter_run_id:
                try:
                    await runner.cancel(adapter_run_id)
                except Exception:  # noqa: BLE001 - 终止失败不能掩盖超时结论
                    pass
            status = transition_run_status(RunStatus.RUNNING, "timeout")
            store.update_test_run(
                run_id, status=status.value,
                error=f"execution timeout after {timeout_s}s",
                finished_at=_now_iso())
            await queue.fail(job["job_id"], f"timeout after {timeout_s}s")
            return {"job_id": job["job_id"], "run_id": run_id, "outcome": status.value}

        event = _FINISH_EVENTS.get(adapter_run.status)
        if event is None:
            # 适配器返回了未收尾状态：契约违例，按基础设施错误收口
            raise AdapterUnavailableError(
                f"runner 返回了未收尾状态: {adapter_run.status.value}")
        status = transition_run_status(RunStatus.RUNNING, event)  # type: ignore[arg-type]
        # 账本以 run_id_for(幂等键) 为权威身份，覆盖适配器内部 id
        normalized = adapter_run.model_copy(update={"run_id": run_id})
        # S3.5 FR-EX-007：失败/错误用例归因随 TestRun 落 raw_json（零 schema 变更）。
        # 容器未装配归因服务时跳过（手拼 Shim/老剖面）；默认 Null 证据提供者一律
        # 裁 needs_human——证据不足不硬归因（AC4），全绿 run 得到空结论列表。
        attribution_svc = container.get("attribution")
        if attribution_svc is not None:
            verdicts = await attribution_svc.attribute_run(normalized)
            normalized = normalized.model_copy(
                update={"attributions": verdicts})
        store.update_test_run(
            run_id,
            status=status.value,
            raw_json=normalized.model_dump_json(),
            finished_at=_now_iso(),
        )
        await queue.ack(job["job_id"])
        return {"job_id": job["job_id"], "run_id": run_id, "outcome": status.value}
    except Exception as exc:
        status = transition_run_status(RunStatus.RUNNING, "finish_error")
        store.update_test_run(
            run_id, status=status.value,
            error=f"{type(exc).__name__}: {exc}"[:500], finished_at=_now_iso())
        await queue.fail(job["job_id"], str(exc)[:500])
        return {"job_id": job["job_id"], "run_id": run_id, "outcome": status.value}


async def run_once(container) -> dict[str, Any] | None:
    """单轮认领处理；队列空返回 None（CLI run-once / 探针用）。"""
    queue = container.get("queue")
    job = await queue.dequeue(
        [JOB_TYPE_TEST_EXECUTION],
        lease_seconds=container.config.limits.execution_lease_seconds,
    )
    if job is None:
        return None
    return await _process_job(container, job)


async def run_worker(
    container, *, max_iterations: int | None = None, poll_interval: float = 1.0
) -> dict[str, Any]:
    """长循环 worker：启动先回收，再按并发上限协程化处理，SIGTERM 由 CLI 兜底。"""
    queue = container.get("queue")
    await recover_stale(container)
    max_concurrency = max(container.config.limits.max_concurrency, 1)
    in_flight: set[asyncio.Task] = set()
    stats = {"processed": 0, "by_outcome": {}, "reclaimed": 0}

    def _on_done(task: asyncio.Task) -> None:
        summary = task.result()
        stats["processed"] += 1
        outcome = summary["outcome"]
        stats["by_outcome"][outcome] = stats["by_outcome"].get(outcome, 0) + 1

    iteration = 0
    try:
        while max_iterations is None or iteration < max_iterations:
            iteration += 1
            if len(in_flight) >= max_concurrency:
                done, _ = await asyncio.wait(
                    in_flight, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    in_flight.discard(task)
            job = await queue.dequeue(
                [JOB_TYPE_TEST_EXECUTION],
                lease_seconds=container.config.limits.execution_lease_seconds,
            )
            if job is None:
                if in_flight:  # 等手头任务收尾再轮询
                    done, _ = await asyncio.wait(
                        in_flight, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        in_flight.discard(task)
                    continue
                if max_iterations is not None:
                    break
                await asyncio.sleep(poll_interval)
                continue
            task = asyncio.create_task(_process_job(container, job))
            in_flight.add(task)
            task.add_done_callback(_on_done)
    finally:
        if in_flight:
            await asyncio.gather(*in_flight, return_exceptions=True)
    stats["reclaimed"] = 0  # recover_stale 的返回由调用方/CLI 单独展示
    return stats
