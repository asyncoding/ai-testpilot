"""基于 SQLite FTS5 的检索适配器（KnowledgeSearchPort 的 S0/S1 实现）。

S1 接入向量混合检索后，本类退化为 BM25 一路；S0 用它跑通 FTS5 连通。
"""

from __future__ import annotations

import json
from typing import Any

from testpilot_core.models import KnowledgeChunk


class FtsSearchAdapter:
    def __init__(self, store) -> None:  # noqa: ANN001
        self.store = store

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

    async def live_verify(self, urn: str, commit: str) -> dict[str, Any]:
        # S0 无 SCM 适配器；S1 由混合检索实现接 CodeRepositoryPort 做真实校验
        return {"verified": False, "reason": "live-verify 在 S1 接入 git 适配器后启用", "urn": urn}
