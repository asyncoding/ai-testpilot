"""问答应用服务（S1.4）：图执行收口 + qa_runs 持久化 + 反馈服务。

职责边界：
- 节点保持无副作用，qa_runs 的唯一写入点在本服务（一次 ask 一行，run_id UNIQUE
  兜底双写，重复即 IntegrityError，不静默 upsert）；
- ask 不经手 langgraph：框架异常归一化在 graph.run_qa_graph，本文件零框架导入，
  换手写状态机时服务层零改动；
- feedback 表无 DB FK（现网未开 foreign_keys），孤儿反馈的唯一守卫是 record_feedback
  里的 run 存在性校验，target_urn 走 core 的 qa_run_urn 约定。

生命周期：测试/默认注 InMemorySaver（构造即就绪，无需关闭）；生产传 data_dir，
构造只存路径不建连接（AsyncSqliteSaver 驱动取 running loop，同步装配期起不了），
由 await astart() 建 checkpoint 库并编译图，aclose() 收口连接。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path
from typing import Any

from testpilot_core.answering import qa_run_urn
from testpilot_core.enums import FeedbackKind, FeedbackReason, QaIntent, QaStatus
from testpilot_core.errors import NotFoundError
from testpilot_core.models import (
    AskRequest,
    AskResponse,
    AssembledAnswer,
    Feedback,
)

from .graph import build_qa_graph, recursion_limit, run_qa_graph, sqlite_saver_ctx
from .nodes import QaDeps

# checkpoint 库在数据目录下的固定相对位置（探针/运维可直接定位对账）
CHECKPOINT_REL = Path("checkpoints") / "qa.sqlite"


class QaService:
    def __init__(self, deps: QaDeps, store, *, checkpointer: Any | None = None,
                 graph: Any | None = None, data_dir: str | Path | None = None) -> None:
        """graph 注入点仅供测试（模拟超步/异常）；生产走 build_qa_graph。

        三种就绪形态：① graph 注入（测试）② data_dir=None（默认 InMemorySaver，
        构造即就绪）③ data_dir 给定：延迟到 astart() 起 AsyncSqliteSaver 后编译图。
        """
        self._deps = deps
        self._store = store
        self._data_dir = Path(data_dir) if data_dir is not None else None
        self._saver_ctx: Any | None = None
        if graph is not None:
            self._graph: Any = graph
        elif self._data_dir is None:
            self._graph = build_qa_graph(deps, checkpointer=checkpointer)
        else:
            self._graph = None  # 等 astart()：saver 连接只能在 running loop 里建
        self._step_limit = recursion_limit(deps.limits)

    async def astart(self) -> "QaService":
        """生产生命周期入口：建 data_dir/checkpoints/qa.sqlite 并以 saver 编译图。

        InMemory/注入路径构造即就绪，本调用幂等直通，调用方可以无脑 await。
        """
        if self._graph is not None:
            return self
        if self._data_dir is None:  # 防御：理论上构造分支已保证不可达
            raise RuntimeError("data_dir 未配置，异步启动无 checkpoint 路径可建")
        self._saver_ctx = sqlite_saver_ctx(self._data_dir / CHECKPOINT_REL)
        saver = await self._saver_ctx.__aenter__()
        # enter 不建表（首次写入才懒建）；显式 setup 让「启动即就绪、空库可对账」
        # 成为确定契约。setup 内部 is_setup + IF NOT EXISTS，重复调用幂等。
        await saver.setup()
        self._graph = build_qa_graph(self._deps, checkpointer=saver)
        return self

    async def aclose(self) -> None:
        """收口 saver 连接（InMemory/注入路径无资源，幂等无副作用）。"""
        if self._saver_ctx is not None:
            ctx, self._saver_ctx = self._saver_ctx, None
            await ctx.__aexit__(None, None, None)

    # ── 问答主流程 ──────────────────────────────────────────────────────────

    async def ask(self, request: AskRequest, *, case_key: str = "",
                  run_id: str | None = None) -> AskResponse:
        run_id = run_id or uuid.uuid4().hex
        if self._graph is None:
            raise RuntimeError("QaService 尚未 astart()：sqlite saver 路径需先异步启动")
        t0 = time.monotonic()
        initial = {
            "run_id": run_id,
            "case_key": case_key,
            "question": request.question,
            "project": request.project,
            "branch": request.branch,
            "acl_scope": request.acl_scope,
        }
        final = await run_qa_graph(self._graph, initial, limit=self._step_limit)
        latency_ms = int((time.monotonic() - t0) * 1000)

        response = self._to_response(run_id, final)
        # 先持久化账本再返回：调用方拿到 run_id 时库里必有对应行（反馈校验依赖）
        await asyncio.to_thread(self._persist, request, final, latency_ms, run_id)
        return response

    async def get_run(self, run_id: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._store.get_qa_run, run_id)

    # ── FR-QA-004 反馈 ──────────────────────────────────────────────────────

    async def record_feedback(
        self,
        run_id: str,
        kind: FeedbackKind,
        *,
        reason: FeedbackReason | None = None,
        detail: str = "",
        model_version: str = "",
    ) -> int:
        """对一次问答 run 写 👍/👎；run 不存在直接拒，不留孤儿行。"""
        existing = await self.get_run(run_id)
        if existing is None:
            raise NotFoundError(f"qa_run 不存在，拒绝孤儿反馈: {run_id}")
        feedback = Feedback(
            run_id=run_id,
            target_urn=qa_run_urn(run_id),
            kind=kind,
            reason=reason,
            detail=detail,
            model_version=model_version,
        )
        return await asyncio.to_thread(self._store.add_feedback, feedback)

    async def feedback_summary(self) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._store.feedback_summary)

    async def aclose(self) -> None:
        """C5 无自有资源（InMemorySaver 无需关闭）；C6 接 sqlite saver 后在此收口。"""

    # ── 内部映射 ────────────────────────────────────────────────────────────

    def _to_response(self, run_id: str, final: dict) -> AskResponse:
        answer = None
        payload = final.get("final_answer")
        if payload:
            answer = AssembledAnswer.model_validate(payload)
        intent_value = final.get("intent")
        return AskResponse(
            run_id=run_id,
            status=QaStatus(final.get("status", QaStatus.ERROR.value)),
            answer=answer,
            unknown_reason=final.get("unknown_reason", ""),
            degraded=list(final.get("degraded_flags") or []),
            intent=QaIntent(intent_value) if intent_value else None,
            model_version=final.get("model_version", ""),
            prompt_version=final.get("prompt_version", ""),
            params_version=final.get("params_version", ""),
            data_version=final.get("data_version", ""),
        )

    def _persist(self, request: AskRequest, final: dict,
                 latency_ms: int, run_id: str) -> None:
        tokens = final.get("tokens_total") or {"in": 0, "out": 0}
        attempts = int(final.get("attempts") or 0)
        payload = final.get("final_answer") or {}
        record = {
            "run_id": run_id,
            "project": request.project,
            "branch": request.branch,
            "acl_scope": request.acl_scope,
            "question": request.question,
            "intent": final.get("intent", ""),
            "status": final.get("status", QaStatus.ERROR.value),
            "answer_payload": payload,
            "citations": payload.get("citations", []),
            "evidence": final.get("evidence") or [],
            "verify_results": final.get("verify_results") or [],
            "retrieval_event_id": final.get("retrieval_event_id"),
            "unknown_reason": final.get("unknown_reason", ""),
            "degraded": final.get("degraded_flags") or [],
            "attempts": attempts,
            # regens = 实际重生成次数：首次生成不计，attempt=2 即重生成 1 次
            "regens": max(attempts - 1, 0),
            "model_version": final.get("model_version", ""),
            "prompt_version": final.get("prompt_version", ""),
            "params_version": final.get("params_version", ""),
            "data_version": final.get("data_version", ""),
            "tokens_in": int(tokens.get("in", 0)),
            "tokens_out": int(tokens.get("out", 0)),
            "latency_ms": latency_ms,
            "thread_id": run_id,
        }
        self._store.insert_qa_run(record=record)


async def build_qa_service(container, *, use_sqlite: bool = True) -> QaService:
    """从同步 Container 装配问答服务（factory 保持 LangGraph 无感知的异步边界）。

    use_sqlite=True：data_dir/checkpoints/qa.sqlite 持久化 checkpoint，调用方负责
    在关停时 await service.aclose()；False：InMemorySaver，进程退出即清空（测试用）。
    """
    deps = QaDeps(
        model=container.get("model"),
        search=container.get("search"),
        scm=container.get("scm"),
        store=container.get("store"),
        repo=container.get("repo"),
        tracer=container.get("tracer"),
    )
    if use_sqlite:
        service = QaService(deps, container.get("store"), data_dir=container.data_dir)
        return await service.astart()
    return QaService(deps, container.get("store"))
