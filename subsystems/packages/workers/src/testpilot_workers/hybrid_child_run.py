"""ChildRunPort 生产实现：混合 xcodebuild 段接入 execution_service 队列（H4，docs/15 §6.2）。

H3 的混合编排器只定义了 :class:`~testpilot_workers.hybrid_runner.ChildRunPort` 协议并用
假件测试，本模块给真实接线，语义纪律：

- **一套队列不造第二套**：子 run 就是普通 :class:`TestExecutionRequest`，复用
  execution_service 的先账本后入队、幂等重放、五态状态机、环境租约与重跑配额；
  幂等键加 ``hybrid:`` 命名空间前缀，与 impact-rerun 的 ``impact:`` 键物理隔离。
- **提交后必须等到终态**：docs/15 §8 定案「s1 子 run 结束才启动后续采集/人工步」，
  只入队就返回会让会话在测试还没跑完时越过该段。轮询 run 账本（不 sleep 死等，
  间隔可注入），崩溃续跑时 submit_execution 重放同一 run_id，等待循环自然接住。
- **失败分型不污染归因**：子 run ``failed`` 是产品失败，抛 :class:`ChildRunFailedError`
  交步骤 failure_policy 裁决；``error/timeout/canceled`` 与等待超时是基础设施未就位，
  抛 :class:`ChildRunUnavailableError`（AdapterUnavailableError 子类），会话转 blocked。
- **launch_arguments 只追溯不穿越**：xcodebuild CLI 无启动参数旗标（``xcodebuild -help``
  实证），探针产出的 ``--ak-*`` 参数由生成的 Swift 测试经
  ``XCUIApplication.launchArguments`` 落地（docs/12 §3.2）；本端口把参数原样写进
  TestExecutionRequest 落 request_json 留痕，xcode_runner 的 argv 不消费它。
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

from testpilot_core.enums import RunStatus
from testpilot_core.hybrid_plan import HybridPlanError
from testpilot_core.models import CaseSelector, TestExecutionRequest

from testpilot_workers import execution_service as svc
from testpilot_workers.hybrid_runner import (
    ChildRunFailedError,
    ChildRunRequest,
    ChildRunUnavailableError,
)

# 子 run 非终态轮询间隔：账本在同机 SQLite，2s 足够及时且不烧轮询。
_DEFAULT_POLL_INTERVAL_S = 2.0
# 等待超时的 runbook 提示：最常见原因是 runner worker 进程没起，队列无人排空。
_DRAIN_HINT = (
    "混合子 run 等待终态超时；若 run 停在 queued，检查 runner worker 是否在跑"
    "（testpilot-runner worker），排障见 docs/03 §3.6 与 docs/15 §8"
)
_TERMINAL_FAIL = frozenset({
    RunStatus.ERROR, RunStatus.TIMEOUT, RunStatus.CANCELED,
})
Sleeper = Callable[[float], Awaitable[None]]


class ExecutionServiceChildRun:
    """把混合段 ChildRunRequest 翻译成执行队列请求并守候到终态。"""

    def __init__(
        self,
        container,
        *,
        poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
        max_wait_s: float | None = None,
        sleeper: Sleeper | None = None,
    ) -> None:
        self._container = container
        self._poll_interval_s = poll_interval_s
        # None = 每次提交时读 profile 的 request_timeout_s（真机 profile 分钟级）
        self._max_wait_s = max_wait_s
        self._sleep: Sleeper = sleeper or (lambda delay: asyncio.sleep(delay))

    async def submit(self, request: ChildRunRequest) -> str:
        container = self._container
        scm = container.get("scm")
        if scm is None:
            # 没有 SCM 就拿不到 HEAD commit 对账，stub profile 不许混跑真实设备段
            raise HybridPlanError(
                "scm 适配器未装配（scm=stub）：混合子 run 需要 HEAD commit 对账，拒绝裸跑")
        store = container.get("store")
        if store is None:
            raise HybridPlanError(
                "store 未装配：混合子 run 无法登记执行账本，拒绝裸跑")

        commit_sha = await scm.head_sha()
        # 命名空间隔离：同一混合步骤崩溃重放得同 run（step 幂等键本身含用例 id），
        # 又不会与 impact-rerun 等其他来源的幂等键撞车。
        namespaced_key = f"hybrid:{request.idempotency_key}"
        exec_request = TestExecutionRequest(
            request_id=f"hybrid-req:{request.idempotency_key}",
            idempotency_key=namespaced_key,
            project=container.config.project.name,
            selector=CaseSelector(test_ids=list(request.only_testing)),
            commit_sha=commit_sha,
            triggered_by="agent",
            # 混合段是计划内首跑，不是每日重跑配额口径；重放由幂等键兜底。
            daily_budget_key=None,
            launch_arguments=list(request.launch_arguments),
        )
        submission = await svc.submit_execution(container, exec_request)
        run_id = submission["run_id"]
        await self._await_terminal(store, run_id)
        return run_id

    async def _await_terminal(self, store, run_id: str) -> None:
        """轮询账本到五态终态；超时按基础设施不可用收口（blocked，不是产品失败）。"""
        max_wait_s = self._max_wait_s
        if max_wait_s is None:
            max_wait_s = float(self._container.config.limits.request_timeout_s)
        elapsed = 0.0
        while True:
            row = store.get_test_run_row(run_id)
            if row is None:
                # submit_execution 先写账本后入队，账本行丢失=接线/库损坏，fail-loud
                raise ChildRunUnavailableError(
                    f"子 run {run_id} 已提交但账本行消失，拒绝在无对账下继续")
            status = RunStatus(row["status"])
            if status is RunStatus.PASSED:
                return
            if status is RunStatus.FAILED:
                raise ChildRunFailedError(
                    f"子 run {run_id} 产品失败（status=failed）：{row['error'] or '见 TestRun 明细'}")
            if status in _TERMINAL_FAIL:
                raise ChildRunUnavailableError(
                    f"子 run {run_id} 基础设施收口（status={status.value}）："
                    f"{row['error'] or '无错误明细'}")
            if elapsed >= max_wait_s:
                raise ChildRunUnavailableError(
                    f"子 run {run_id} {max_wait_s:.0f}s 未到终态（当前 {status.value}）；{_DRAIN_HINT}")
            await self._sleep(self._poll_interval_s)
            elapsed += self._poll_interval_s
