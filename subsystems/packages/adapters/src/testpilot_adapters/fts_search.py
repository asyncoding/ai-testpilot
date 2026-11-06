"""基于 SQLite FTS5 的检索适配器（KnowledgeSearchPort 的 S0/S1 实现）。

S1 接入向量混合检索后，本类退化为 BM25 一路；S0 用它跑通 FTS5 连通。
offline-demo profile 用它，live-verify 仍走与 hybrid 同一 LiveVerifier
（录制 scm），保证两个检索适配器的版本校验语义一致（FR-QA-002）。
"""

from __future__ import annotations

from typing import Any

from testpilot_core.models import KnowledgeChunk, RepoRef
from testpilot_core.retrieval import shape_trigram_query

from .live_verify import LiveVerifier


class FtsSearchAdapter:
    def __init__(
        self,
        store,
        *,
        project: str = "",
        scm=None,  # CodeRepositoryPort
        repo: RepoRef | None = None,
        tracer=None,
        default_branch: str = "main",
    ) -> None:  # noqa: ANN001
        self.store = store
        self.default_branch = default_branch
        self._verifier = LiveVerifier(
            scm, store, repo or RepoRef(project=project, repo_path=""),
            tracer=tracer, project=project,
        )

    async def search(
        self,
        query: str,
        *,
        project: str,
        branch: str = "main",
        acl_scope: str = "default",
        limit: int = 8,
    ) -> list[KnowledgeChunk]:
        # 必须先整形：原始问题里的 . : " * ( ) 是 FTS5 语法字符，直塞 MATCH
        # 会抛 OperationalError 让整条问答链炸在检索节点（S1.4 端到端测试复现）。
        # 与 HybridSearchAdapter 共用 core 的同一整形函数，两通道查询语义保持一致。
        match_expr, like_terms = shape_trigram_query(query)
        if not match_expr and not like_terms:
            return []
        rows = self.store.search_fts_matches(
            match_expr, like_terms,
            branch=branch, acl_scope=acl_scope, kinds=(), limit=limit,
        )
        # 与 HybridSearchAdapter 走同一个水合方法：JOIN source_documents 把
        # doc_authority/doc_uri/doc_content_hash 折进 chunk.metadata。旧实现只取
        # raw_json，FTS-only 离线 profile 上证据块没有 doc_authority，
        # FR-QA-003 的 L2 历史证词识别整链失效（qa_probe incident 双段照出该洞）。
        loaded = self.store.load_retrieval_chunks([r["urn"] for r in rows])
        chunks: list[KnowledgeChunk] = []
        for row in loaded:
            row = dict(row)
            doc_meta = {
                k: row.pop(k)
                for k in ("doc_uri", "doc_mtime", "doc_authority", "doc_content_hash")
                if k in row
            }
            # doc_* 不是 KnowledgeChunk 字段（pydantic 直接 validate 会忽略），
            # 并入 metadata 才能随 to_evidence_dict 进入答案图 state
            row["metadata"] = {**(row.get("metadata") or {}), **doc_meta}
            chunks.append(KnowledgeChunk.model_validate(row))
        return chunks

    async def live_verify(self, urn: str, commit: str = "") -> dict[str, Any]:
        result = await self._verifier.verify(urn, commit)
        return result.model_dump()
