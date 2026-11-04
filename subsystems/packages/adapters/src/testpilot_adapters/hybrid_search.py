"""混合检索适配器（docs/08 FR-KNO-001，S1.2）。

trigram-BM25 与向量两路并行召回 → core 纯函数 RRF×质量×新鲜度终排 →
批量加载切块（带 source URI + content_hash，KNO-001 AC2）→ retrieval_events 留痕。

降级语义（显式、不吞错）：
- 向量通道异常（embed 失败 / Chroma 不可达）→ FTS-only，trace.degraded=True，
  错误记入 retrieval_events.raw_json.vector_error；
- FTS 通道异常属于本地存储故障，直接抛出，不静默；
- 两通道都空 → 返回空结果（非降级，是真没召回，不编造）。
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from datetime import datetime, timezone
from typing import Any

from testpilot_core.models import KnowledgeChunk
from testpilot_core.retrieval import (
    FTS,
    VECTOR,
    ChannelRanking,
    HybridResult,
    build_stage_trace,
    fuse_and_rank,
    shape_trigram_query,
)


class HybridSearchAdapter:
    def __init__(
        self,
        store,  # noqa: ANN001
        model_gateway,  # noqa: ANN001  ModelGatewayPort
        vector_store,  # noqa: ANN001  VectorStorePort
        *,
        project: str = "",
        channel_top_k: int = 20,
        rrf_k: int = 60,
        quality_power: float = 1.0,
        half_life_days: float | None = None,
    ) -> None:
        self.store = store
        self.model = model_gateway
        self.vectors = vector_store
        self.project = project
        self.channel_top_k = channel_top_k
        self.rrf_k = rrf_k
        self.quality_power = quality_power
        self.half_life_days = half_life_days

    async def search(
        self,
        query: str,
        *,
        project: str | None = None,
        branch: str = "main",
        acl_scope: str = "default",
        kinds: tuple[str, ...] = (),
        limit: int = 8,
    ) -> HybridResult:
        project = project if project is not None else self.project
        match_expr, like_terms = shape_trigram_query(query)

        # 两通道并发（sqlite 调用放线程池，避免阻塞事件循环）
        fts_task = asyncio.ensure_future(self._fts_recall(match_expr, like_terms, branch, acl_scope, kinds))
        vec_task = asyncio.ensure_future(
            self._vector_recall(query, branch, acl_scope, kinds)
        )
        fts_ranking, vec_ranking = await asyncio.gather(fts_task, vec_task)

        trace = build_stage_trace(fts_ranking, vec_ranking)
        union_urns = list(dict.fromkeys([*fts_ranking.urns, *vec_ranking.urns]))
        loaded = self.store.load_retrieval_chunks(union_urns)
        by_urn = {row["urn"]: row for row in loaded}

        quality = {urn: float(by_urn.get(urn, {}).get("quality_score", 0.5)) for urn in union_urns}
        ages: dict[str, float] = {}
        for urn in union_urns:
            age = _age_days(by_urn.get(urn, {}).get("doc_mtime"))
            if age is not None:
                ages[urn] = age

        ranked = fuse_and_rank(
            [fts_ranking, vec_ranking],
            quality_by_urn=quality,
            age_days_by_urn=ages,
            rrf_k=self.rrf_k,
            quality_power=self.quality_power,
            half_life_days=self.half_life_days,
        )[:limit]

        chunks: list[KnowledgeChunk] = []
        scores = []
        for fused in ranked:
            row = by_urn.get(fused.urn)
            if row is None:
                continue  # 两通道召回后、融合前被删的极端情况：跳过
            # doc_* 不在 KnowledgeChunk 字段里（直接 validate 会被忽略），
            # 并入 metadata 供答案引用溯源（KNO-001 AC2）。
            row = dict(row)
            doc_meta = {
                k: row.pop(k)
                for k in ("doc_uri", "doc_mtime", "doc_authority", "doc_content_hash")
                if k in row
            }
            row["metadata"] = {**(row.get("metadata") or {}), **doc_meta}
            chunks.append(KnowledgeChunk.model_validate(row))
            scores.append(fused)

        model_version = getattr(self.model, "embed_model", None) or getattr(
            self.model, "model_version", ""
        )
        vector_error = getattr(vec_ranking, "error", "")
        event_id = self.store.record_retrieval_event(
            query=query,
            query_sha=hashlib.sha256(query.encode("utf-8")).hexdigest()[:16],
            project=project,
            branch=branch,
            acl_scope=acl_scope,
            kinds=kinds,
            model_version=str(model_version),
            fts_available=trace.fts_available,
            vector_available=trace.vector_available,
            degraded=trace.degraded,
            fts_urns=trace.fts_urns,
            vector_urns=trace.vector_urns,
            fused_urns=[f.urn for f in scores],
            fts_latency_ms=trace.fts_latency_ms,
            vector_latency_ms=trace.vector_latency_ms,
            extra={"vector_error": vector_error} if vector_error else None,
        )
        return HybridResult(chunks=chunks, scores=scores, trace=trace, event_id=event_id)

    async def _fts_recall(
        self,
        match_expr: str,
        like_terms: list[str],
        branch: str,
        acl_scope: str,
        kinds: tuple[str, ...],
    ) -> ChannelRanking:
        t0 = time.monotonic()
        rows = await asyncio.to_thread(
            self.store.search_fts_matches,
            match_expr,
            like_terms,
            branch=branch,
            acl_scope=acl_scope,
            kinds=kinds,
            limit=self.channel_top_k,
        )
        return ChannelRanking(
            FTS, [r["urn"] for r in rows], latency_ms=int((time.monotonic() - t0) * 1000)
        )

    async def _vector_recall(
        self,
        query: str,
        branch: str,
        acl_scope: str,
        kinds: tuple[str, ...],
    ) -> ChannelRanking:
        t0 = time.monotonic()
        try:
            vectors = await self.model.embed([query])
            if not vectors:
                raise RuntimeError("embed 返回空向量")
            where = _chroma_where(branch=branch, acl_scope=acl_scope, kinds=kinds)
            hits = await self.vectors.query(
                vectors[0], self.channel_top_k, where=where
            )
            return ChannelRanking(
                VECTOR,
                [h.id for h in hits],
                latency_ms=int((time.monotonic() - t0) * 1000),
            )
        except Exception as exc:  # 降级：不吞，错误随留痕落库
            return ChannelRanking(
                VECTOR,
                [],
                available=False,
                degraded=True,
                latency_ms=int((time.monotonic() - t0) * 1000),
                error=f"{type(exc).__name__}: {exc}"[:300],
            )

    async def live_verify(self, urn: str, commit: str) -> dict[str, Any]:
        # S1.3 接 CodeRepositoryPort；与 FtsSearchAdapter 保持同一占位
        return {"verified": False, "reason": "live-verify 在 S1.3 接入 git 适配器后启用", "urn": urn}


def _chroma_where(
    *, branch: str, acl_scope: str, kinds: tuple[str, ...]
) -> dict[str, Any]:
    """Chroma 多条件必须显式 $and（0.5.x 不再隐式 AND 多个顶层键，实测于联调）。"""
    clauses: list[dict[str, Any]] = [{"branch": branch}, {"acl_scope": acl_scope}]
    if kinds:
        clauses.append({"kind": {"$in": list(kinds)}})
    if len(clauses) == 1:
        return clauses[0]
    return {"$and": clauses}


def _age_days(mtime: str | None) -> float | None:
    if not mtime:
        return None
    try:
        dt = datetime.fromisoformat(mtime.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0)
