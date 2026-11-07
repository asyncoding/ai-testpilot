"""问答图测试共用 fake（hostless，不连任何外部服务）。

仿照 git_helpers.py 的 tests/ 同级模块惯例；三个 test_qa_* 图测试共用。
所有 fake 都记录调用序列，供「精确调用计数」类反假绿断言使用。
"""

from __future__ import annotations

from typing import Any

from testpilot_core.errors import AdapterUnavailableError, PathMissingAtCommitError
from testpilot_core.models import KnowledgeChunk, RepoRef
from testpilot_core.retrieval import (
    ChannelRanking,
    FusedScore,
    HybridResult,
    StageTrace,
)

FTS = "fts"
VECTOR = "vector"


def make_chunk(urn: str, *, doc_urn: str = "urn:testpilot:fs-docs:document:d1",
               kind: str = "text", text: str = "证据原文", branch: str = "main",
               content_hash: str | None = None, valid_from: str | None = "abc1234",
               metadata: dict | None = None) -> KnowledgeChunk:
    return KnowledgeChunk(
        urn=urn, doc_urn=doc_urn, index=0, kind=kind, text=text,
        branch=branch, valid_from=valid_from,
        content_hash=content_hash or f"hash-{urn[-6:]}",
        metadata_=metadata or {},
    )


def structured_envelope(data: dict | None = None, *, degraded: bool = False,
                        model: str = "fake-model-1", tokens_in: int = 100,
                        tokens_out: int = 30) -> dict[str, Any]:
    return {
        "data": data, "text": "", "model": model,
        "tokens_in": tokens_in, "tokens_out": tokens_out,
        "latency_ms": 0, "attempts": 1, "degraded": degraded,
    }


class FakeModel:
    """按 key 返回响应；同一 key 给 list 时按调用次序弹出。

    缺 key 或 envelope 含 "raise" 时抛 AdapterUnavailableError，
    与 RecordedModelGateway 的缺 key 行为对齐。
    """

    def __init__(self, responses: dict[str, Any]):
        self.responses = responses
        self.calls: list[tuple[str, str]] = []  # (method, key)
        self.messages: list[list[dict]] = []  # 每次 structured 调用的完整消息

    async def chat(self, messages, **options):
        key = options.get("key", "default")
        self.calls.append(("chat", key))
        return {"text": "fake", "model": "fake-model-1", "tokens_in": 1,
                "tokens_out": 1, "degraded": False}

    async def structured(self, messages, schema, **options):
        key = options.get("key", "default")
        self.calls.append(("structured", key))
        self.messages.append(list(messages))
        spec = self.responses.get(key)
        if spec is None:
            raise AdapterUnavailableError(f"fake 缺少录制 key: {key}")
        env = spec.pop(0) if isinstance(spec, list) else spec
        if isinstance(env, dict) and env.get("raise"):
            raise AdapterUnavailableError(env["raise"])
        return env

    async def embed(self, texts):
        self.calls.append(("embed", ""))
        return [[0.1] * 8 for _ in texts]

    def structured_keys(self) -> list[str]:
        return [k for method, k in self.calls if method == "structured"]

    def count(self, key: str | None = None) -> int:
        return sum(1 for _m, k in self.calls if key is None or k == key)


class _FakeSearchBase:
    """共用留痕与 live_verify 编程能力；search 形态由子类决定。"""

    def __init__(self, chunks: list[KnowledgeChunk], *,
                 verify_results: dict[str, dict] | None = None,
                 degraded: bool = False, event_id: int | None = 11):
        self.chunks = chunks
        self.search_calls: list[dict[str, Any]] = []
        self.verify_calls: list[str] = []
        self._verify = verify_results or {}
        self._degraded = degraded
        self._event_id = event_id

    def _hybrid_result(self) -> HybridResult:
        # rankings 仅用于构造真实 StageTrace 语义（节点只读 trace 字段）
        ChannelRanking(FTS, [c.urn for c in self.chunks])
        ChannelRanking(VECTOR, [c.urn for c in self.chunks],
                       available=not self._degraded, degraded=self._degraded)
        scores = [
            FusedScore(urn=c.urn, total=0.9 - i * 0.1, rrf=0.01,
                       quality=0.5, freshness=1.0, channels=(FTS, VECTOR))
            for i, c in enumerate(self.chunks)
        ]
        trace = StageTrace(
            fts_available=True, vector_available=not self._degraded,
            degraded=self._degraded,
            fts_urns=[c.urn for c in self.chunks],
            vector_urns=[] if self._degraded else [c.urn for c in self.chunks],
        )
        return HybridResult(chunks=list(self.chunks), scores=scores,
                            trace=trace, event_id=self._event_id)

    async def live_verify(self, urn: str, commit: str = "") -> dict[str, Any]:
        self.verify_calls.append(urn)
        default = {
            "urn": urn, "kind": "code_symbol", "exists": True, "verified": True,
            "body_changed": False, "degraded": False, "reason": "unchanged",
            "head_commit": "7936295a700c8542034573e64f40bced3d5f80e5",
            "indexed_commit": "abc1234", "latency_ms": 5, "branch": "main",
        }
        return {**default, **self._verify.get(urn, {})}


class FakeHybridSearch(_FakeSearchBase):
    """对齐 HybridSearchAdapter：search 带显式 kinds 形参，返回 HybridResult。"""

    async def search(self, query, *, project="", branch="main",
                     acl_scope="default", limit=8, kinds=None, symbol_name=None):
        self.search_calls.append({
            "query": query, "project": project, "branch": branch,
            "acl_scope": acl_scope, "limit": limit, "kinds": kinds,
            "symbol_name": symbol_name,
        })
        return self._hybrid_result()


class FakeFtsSearch(_FakeSearchBase):
    """对齐 FtsSearchAdapter：只有 **kwargs（无 kinds 形参），返回裸 list。"""

    async def search(self, query, **kwargs):
        self.search_calls.append({"query": query, **kwargs})
        return list(self.chunks)


def FakeSearch(chunks: list[KnowledgeChunk], *, hybrid: bool = False,
               verify_results: dict[str, dict] | None = None,
               degraded: bool = False, event_id: int | None = 11):
    """测试便利工厂：hybrid=True 给带 kinds 形参的形态，否则裸 list 形态。"""
    cls = FakeHybridSearch if hybrid else FakeFtsSearch
    return cls(chunks, verify_results=verify_results,
               degraded=degraded, event_id=event_id)


class FakeScm:
    """read_file_at_commit 按 (path, commit) 取编程 blob；缺档抛 PathMissing。"""

    def __init__(self, blobs: dict[tuple[str, str], str], *, head: str = "7936295a700c8542034573e64f40bced3d5f80e5"):
        self.blobs = blobs
        self.head = head
        self.reads: list[tuple[str, str]] = []

    async def head_sha(self, repo: RepoRef) -> str:
        return self.head

    async def read_file_at_commit(self, repo: RepoRef, path: str, commit: str) -> str:
        self.reads.append((path, commit))
        if (path, commit) not in self.blobs:
            raise PathMissingAtCommitError(f"fake 仓 {commit} 无 {path}")
        return self.blobs[(path, commit)]


class FakeStore:
    """只实现 assembly 节点用到的 get_document。"""

    def __init__(self, docs: dict | None = None):
        self.docs = docs or {}

    def get_document(self, urn: str):
        return self.docs.get(urn)
