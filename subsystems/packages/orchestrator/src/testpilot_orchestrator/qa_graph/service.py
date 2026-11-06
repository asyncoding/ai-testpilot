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

    async def list_runs(self, *, status: str | None = None,
                        limit: int = 50) -> list[dict[str, Any]]:
        return await asyncio.to_thread(
            self._store.list_qa_runs, status=status, limit=limit)

    # ── S1.5：原文代理与 trace 时间线（router 的薄数据面）──────────────────────

    async def get_run_source(self, run_id: str, urn: str) -> dict[str, Any]:
        """取某次 run 证据白名单内的原文（FR-UI-001：引用可点开原文与 hash）。

        白名单 = 该 run 证据块 URN ∪ 证据所属文档 URN ∪ 模型实际引用 URN。
        白名单外（含路径穿越形态、他库文档 URN）一律 NotFoundError——本方法
        不接收任何文件路径参数，文档级请求只经 URN 反查 store，无穿越面。
        """
        run = await self.get_run(run_id)
        if run is None:
            raise NotFoundError(f"qa_run 不存在: {run_id}")
        evidence: list[dict[str, Any]] = run.get("evidence") or []
        citations = (run.get("answer_payload") or {}).get("citations") or []
        whitelist = {e["urn"] for e in evidence}
        whitelist.update(e.get("doc_urn") for e in evidence if e.get("doc_urn"))
        whitelist.update(c.get("urn") for c in citations if c.get("urn"))
        if urn not in whitelist:
            # 与 run 不存在同形态 404：不向调用方透露白名单边界外的资源存在性
            raise NotFoundError(f"URN 不在该 run 的证据白名单内: {urn}")

        chunk = next((e for e in evidence if e["urn"] == urn), None)
        if chunk is not None:
            return {
                "urn": urn,
                "kind": chunk.get("kind", ""),
                "text": chunk.get("text", ""),
                "content_hash": chunk.get("content_hash", ""),
                "source_uri": chunk.get("source_uri", ""),
                "doc_urn": chunk.get("doc_urn", ""),
                "authority_level": chunk.get("authority_level", ""),
                "branch": chunk.get("branch", ""),
                "stale": chunk.get("stale", ""),
            }

        # 文档级 URN：聚合该文档当前索引的全部切块（白名单已证明文档属于本 run）
        def _load_document() -> dict[str, Any]:
            doc = self._store.get_document(urn)
            if doc is None:
                raise NotFoundError(f"白名单文档在索引中已不存在: {urn}")
            chunk_urns = self._store.list_chunk_urns(urn)
            rows = self._store.load_retrieval_chunks(chunk_urns)
            return {
                "urn": urn,
                "kind": "document",
                "text": "\n\n".join(str(r.get("text") or "") for r in rows),
                "content_hash": doc.content_hash,
                "source_uri": doc.uri,
                "doc_urn": urn,
                "authority_level": doc.authority_level.value
                if hasattr(doc.authority_level, "value") else str(doc.authority_level),
                "branch": "",
                "stale": "",
            }

        return await asyncio.to_thread(_load_document)

    async def get_run_trace(self, run_id: str) -> dict[str, Any]:
        """组装本地可观测时间线（FR-EV-004 AC1：UI 可反查一次问答的完整链路）。

        verification_events 表无 run_id 列无法 join，时间线直接用 qa_runs 里
        图落盘的 verify_results/evidence 投影；Langfuse 深链由 router 按 profile
        注入，服务层不读配置。FTS 通道 retrieval_event_id 为 null 时如实呈现。
        """
        run = await self.get_run(run_id)
        if run is None:
            raise NotFoundError(f"qa_run 不存在: {run_id}")
        evidence = [
            {
                "urn": e.get("urn", ""),
                "doc_urn": e.get("doc_urn", ""),
                "kind": e.get("kind", ""),
                "score": e.get("score", 0.0),
                "authority_level": e.get("authority_level", ""),
            }
            for e in (run.get("evidence") or [])
        ]
        citations = [
            {
                "urn": c.get("urn", ""),
                "version_state": c.get("version_state", ""),
                "version_ref": c.get("version_ref", ""),
            }
            for c in (run.get("citations") or [])
        ]
        return {
            "run_id": run["run_id"],
            "question": run.get("question", ""),
            "intent": run.get("intent", ""),
            "status": run.get("status", ""),
            "unknown_reason": run.get("unknown_reason", ""),
            "attempts": run.get("attempts", 0),
            "regens": run.get("regens", 0),
            "tokens": {"in": run.get("tokens_in", 0), "out": run.get("tokens_out", 0)},
            "latency_ms": run.get("latency_ms", 0),
            "degraded": run.get("degraded") or [],
            "retrieval_event_id": run.get("retrieval_event_id"),
            "evidence": evidence,
            "verify_results": run.get("verify_results") or [],
            "citations": citations,
            "versions": {
                "model": run.get("model_version", ""),
                "prompt": run.get("prompt_version", ""),
                "params": run.get("params_version", ""),
                "data": run.get("data_version", ""),
            },
            "thread_id": run.get("thread_id", ""),
            "created_at": run.get("created_at", ""),
        }

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
