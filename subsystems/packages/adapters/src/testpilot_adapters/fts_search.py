"""基于 SQLite FTS5 的检索适配器（KnowledgeSearchPort 的 S0/S1 实现）。

S1 接入向量混合检索后，本类退化为 BM25 一路；S0 用它跑通 FTS5 连通。
offline-demo profile 用它，live-verify 仍走与 hybrid 同一 LiveVerifier
（录制 scm），保证两个检索适配器的版本校验语义一致（FR-QA-002）。
"""

from __future__ import annotations

import json
from typing import Any

from testpilot_core.models import KnowledgeChunk, RepoRef

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
        rows = self.store.search_fts(query, limit=limit)
        chunks: list[KnowledgeChunk] = []
        for row in rows:
            full = self.store.conn.execute(
                "SELECT raw_json FROM chunks WHERE urn=?", (row["urn"],)
            ).fetchone()
            if full:
                chunks.append(KnowledgeChunk.model_validate(json.loads(full["raw_json"])))
        return chunks

    async def live_verify(self, urn: str, commit: str = "") -> dict[str, Any]:
        result = await self._verifier.verify(urn, commit)
        return result.model_dump()
