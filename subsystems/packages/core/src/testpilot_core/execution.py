"""测试执行的纯领域逻辑：状态机、重跑配额、租约回收（docs/08 FR-EX-002，S3.1）。

零 IO、零三方：worker 怎么领任务、什么情况下拒绝重跑、崩溃后任务怎么重领，
规则全部在本模块算完，worker/service 只负责取数、落账与调适配器。

三块职责：

1. **run 状态机**：queued/running 与五个终态（passed/failed/error/timeout/canceled）
   之间只允许迁移闭集中的边，非法迁移 fail-closed 抛 :class:`ExecutionError`，
   终态不可变（防后写覆盖前写）。
2. **每日重跑配额**：配额判定是纯函数，账本（今天已记账的重跑）由调用方传入；
   同一 request_id 重放幂等不重复扣减；跨日靠调用方传入的 date 自然重置，
   纯函数不碰时钟。
3. **队列租约回收**：worker 领任务即写租约到期时间；进程被杀后，过期 running
   任务由回收判定决定重领还是死信，attempts 达上限不再重领（防毒消息无限循环）。
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Literal, Sequence

from pydantic import BaseModel, Field

from .enums import CaseStatus, RunStatus
from .models import CaseResult, TestExecutionRequest

# worker 队列里执行类任务的统一 job_type（service 入队/worker 认领以此为准）
JOB_TYPE_TEST_EXECUTION = "test_execution"

# 五个终态：进了终态的 run 不允许任何后续迁移
TERMINAL_RUN_STATUSES = frozenset(
    {
        RunStatus.PASSED,
        RunStatus.FAILED,
        RunStatus.ERROR,
        RunStatus.TIMEOUT,
        RunStatus.CANCELED,
    }
)

# 状态机事件（人话：worker 生命周期里能发生的事）
RunEvent = Literal[
    "pick_up",        # worker 从队列领到任务
    "cancel",         # 用户/系统请求取消
    "finish_passed",  # 执行结束且用例全过
    "finish_failed",  # 执行结束但有用例失败
    "finish_error",   # 基础设施级失败（构建失败/适配器异常/零结果）
    "timeout",        # 超过执行超时被终止
    "requeue",        # 租约回收器确认 worker 已死，回队重领（仅回收路径合法）
]

# 合法迁移闭集：表外迁移一律拒绝，绝不静默落非法状态
_RUN_TRANSITIONS: dict[RunStatus, dict[str, RunStatus]] = {
    RunStatus.QUEUED: {
        "pick_up": RunStatus.RUNNING,
        "cancel": RunStatus.CANCELED,
    },
    RunStatus.RUNNING: {
        "cancel": RunStatus.CANCELED,
        "finish_passed": RunStatus.PASSED,
        "finish_failed": RunStatus.FAILED,
        "finish_error": RunStatus.ERROR,
        "timeout": RunStatus.TIMEOUT,
        # 唯一的"回头路"：必须有过期租约作证（decide_reclaim 判定），
        # 正常执行路径不允许调用，防后写覆盖前写。
        "requeue": RunStatus.QUEUED,
    },
    RunStatus.PASSED: {},
    RunStatus.FAILED: {},
    RunStatus.ERROR: {},
    RunStatus.TIMEOUT: {},
    RunStatus.CANCELED: {},
}

# 配额判定原因码（worker 落账/API 错误映射都用这几个字面量）
BUDGET_OK = "ok"
BUDGET_REPLAY = "replay"
BUDGET_DENIED = "daily_rerun_quota_exceeded"

# 租约回收动作
RECLAIM_NONE = "none"
RECLAIM_REQUEUE = "requeue"
RECLAIM_DEAD_LETTER = "dead_letter"

_RUN_ID_PREFIX = "run-"


class ExecutionError(ValueError):
    """执行域输入/迁移非法（非法状态迁移、配额账本矛盾等），服务层映射 422。"""


# ── 1. run 状态机 ────────────────────────────────────────────────────────────


def transition_run_status(current: RunStatus, event: RunEvent) -> RunStatus:
    """按迁移闭集求下一状态；终态或非法事件抛 :class:`ExecutionError`。"""
    try:
        return _RUN_TRANSITIONS[current][event]
    except KeyError:
        raise ExecutionError(
            f"非法执行状态迁移：{current.value} --{event}-->（终态不可变/事件不合法）"
        ) from None


def run_outcome(case_results: Sequence[CaseResult]) -> RunStatus:
    """从用例级结果聚合 run 级结论。

    - 零结果：基础设施级 ``error``（如构建失败，xcodebuild 没产出任何用例）；
    - 任一 failed/error：run 级 ``failed``（用例失败与基础设施错误在 run 级都算失败，
      具体区分看 CaseResult.failure_type 与 message）；
    - 其余（含全 skipped）：``passed``。
    """
    if not case_results:
        return RunStatus.ERROR
    if any(c.status in (CaseStatus.FAILED, CaseStatus.ERROR) for c in case_results):
        return RunStatus.FAILED
    return RunStatus.PASSED


def run_id_for(idempotency_key: str) -> str:
    """内容寻址 run_id：同幂等键重放恒得同一 run_id（配合账本 UNIQUE 实现幂等）。"""
    digest = hashlib.sha1(idempotency_key.encode("utf-8")).hexdigest()[:12]
    return f"{_RUN_ID_PREFIX}{digest}"


def is_rerun(request: TestExecutionRequest) -> bool:
    """带每日配额账本 key 的请求才算重跑；首次执行不占重跑预算。"""
    return bool(request.daily_budget_key)


# ── 2. 每日重跑配额 ──────────────────────────────────────────────────────────


class BudgetEntry(BaseModel):
    """一次已记账的重跑（账本行的纯数据投影；持久化在 rerun_budget 表）。"""

    budget_key: str
    date: str  # UTC YYYY-MM-DD；日期由调用方给，纯函数不读时钟，测试可冻结
    request_id: str
    run_id: str


class BudgetDecision(BaseModel):
    allowed: bool
    reason: str
    already_counted: bool = False  # True=同 request_id 重放，不新占预算槽位


def evaluate_rerun(
    *,
    request_id: str,
    budget_key: str,
    date: str,
    ledger: Sequence[BudgetEntry],
    quota: int,
) -> BudgetDecision:
    """判定一次重跑请求是否放行（docs/08 FR-EX-002 AC2：超日配额拒绝并记账）。

    顺序：先查重放（同 request_id 已入账→放行但不重复扣减），再数当日槽位。
    quota<=0 表示配额关死，重放之外一律拒绝。
    """
    same_request = [e for e in ledger if e.request_id == request_id]
    if same_request:
        # 幂等重放：同一请求绝不允许换 budget_key 复现（账本矛盾，fail-closed）
        if any(e.budget_key != budget_key for e in same_request):
            raise ExecutionError(
                f"重放请求的 budget_key 与账本不一致：request_id={request_id}"
            )
        return BudgetDecision(allowed=True, reason=BUDGET_REPLAY, already_counted=True)
    today = [e for e in ledger if e.budget_key == budget_key and e.date == date]
    if len(today) >= max(quota, 0):
        return BudgetDecision(allowed=False, reason=BUDGET_DENIED)
    return BudgetDecision(allowed=True, reason=BUDGET_OK)


# ── 3. 队列租约回收 ──────────────────────────────────────────────────────────


class JobLease(BaseModel):
    """jobs 表中与租约相关字段的纯数据投影（回收判定不直接吃 sqlite row）。"""

    job_id: str
    status: str  # queued / running / done / failed
    attempts: int
    lease_expires_at: datetime | None = None


class ReclaimDecision(BaseModel):
    action: str = RECLAIM_NONE
    reason: str = ""


def decide_reclaim(job: JobLease, *, now: datetime, max_attempts: int) -> ReclaimDecision:
    """崩溃恢复判定（docs/08 FR-EX-002 AC1：worker 重启后任务可被重新领取）。

    只认 ``running`` 且租约已过期的任务；未过期说明可能仍有 worker 在跑，
    绝不能抢（防同一任务两个 worker 并发执行）。attempts 已达上限的过期任务
    转死信而不是无限重领。
    """
    if job.status != "running" or job.lease_expires_at is None:
        return ReclaimDecision(action=RECLAIM_NONE)
    if job.lease_expires_at > now:
        return ReclaimDecision(action=RECLAIM_NONE)
    if job.attempts >= max_attempts:
        return ReclaimDecision(
            action=RECLAIM_DEAD_LETTER, reason="max_attempts_exceeded"
        )
    return ReclaimDecision(action=RECLAIM_REQUEUE, reason="lease_expired")
