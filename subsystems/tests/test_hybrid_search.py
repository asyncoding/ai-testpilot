"""HybridSearchAdapter 契约（docs/08 FR-KNO-001，S1.2）。

fake 模型 + fake 向量库 + 真实 sqlite：断言双路 RRF 融合顺序、
过滤条件下推、KNO-001 AC2（URI+content_hash）、retrieval_events 留痕、
向量故障显式降级 FTS-only（错误入库不被吞掉）。
"""

import json


from testpilot_adapters.hybrid_search import HybridSearchAdapter
from testpilot_adapters.store import bootstrap_store
from testpilot_core.enums import AuthorityLevel
from testpilot_core.models import KnowledgeChunk, SourceDocument, VectorHit
from testpilot_core.retrieval import FTS, VECTOR


def _doc(urn="urn:testpilot:fs-docs:document:d1@h1", uri="file:///tmp/d1.md"):
    return SourceDocument(
        urn=urn, source_system="fs-docs", uri=uri, title="d1", version="h1",
        content_hash="hash1", authority_level=AuthorityLevel.L2_EXPERIENCE,
    )


def _chunk(text, *, urn="urn:testpilot:fs-docs:chunk:d1#c0", doc_urn=None,
           index=0, kind="text", quality=0.7):
    return KnowledgeChunk(
        urn=urn, doc_urn=doc_urn or "urn:testpilot:fs-docs:document:d1@h1",
        index=index, kind=kind, text=text, quality_score=quality,
        branch="main", content_hash=f"ch-{index}",
    )


class FakeModel:
    embed_model = "fake-embed-1"

    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.embed_calls = 0

    async def embed(self, texts):
        self.embed_calls += 1
        if self.fail:
            raise RuntimeError("ollama down")
        return [[0.1, 0.2, 0.3] for _ in texts]


class FakeVectors:
    def __init__(self, hit_ids):
        self.hit_ids = hit_ids
        self.wheres = []

    async def query(self, vector, top_k, where=None):
        self.wheres.append(where)
        return [VectorHit(id=u, distance=0.1 * (i + 1)) for i, u in enumerate(self.hit_ids)]


def _store_with_two_chunks(tmp_path):
    store = bootstrap_store(tmp_path)
    store.upsert_document(_doc())
    store.upsert_chunk(_chunk("全屏闹钟响时按物理键的日志证据", quality=0.9))
    store.upsert_chunk(
        _chunk("无关内容：默认闹钟铃声配置", urn="urn:testpilot:fs-docs:chunk:d1#c1",
               index=1, quality=0.5)
    )
    return store


async def test_hybrid_fuses_two_channels_overlap_ranks_first(tmp_path):
    store = _store_with_two_chunks(tmp_path)
    vectors = FakeVectors([
        "urn:testpilot:fs-docs:chunk:d1#c1",  # 向量通道 c1 第 1
        "urn:testpilot:fs-docs:chunk:d1#c0",  # c0 第 2（但 FTS 也命中 → 双通道）
    ])
    adapter = HybridSearchAdapter(
        store, FakeModel(), vectors, project="ios-local-poc"
    )
    result = await adapter.search("物理键日志", limit=5)

    urns = [c.urn for c in result.chunks]
    assert urns[0].endswith("#c0") and urns[1].endswith("#c1")
    top = result.scores[0]
    assert top.channels == (FTS, VECTOR)
    # KNO-001 AC2：切块带源 URI 与 content_hash
    assert result.chunks[0].metadata_["doc_uri"] == "file:///tmp/d1.md"
    assert result.chunks[0].metadata_["doc_content_hash"] == "hash1"
    # 留痕
    row = store.conn.execute("SELECT * FROM retrieval_events WHERE id=?", (result.event_id,)).fetchone()
    assert row["degraded"] == 0 and row["project"] == "ios-local-poc"
    assert row["model_version"] == "fake-embed-1"
    fused = json.loads(row["fused_urns"])
    assert fused[0].endswith("#c0")
    assert json.loads(row["fts_urns"])[0].endswith("#c0")
    assert json.loads(row["vector_urns"])[0].endswith("#c1")


async def test_kinds_filter_pushed_to_both_channels(tmp_path):
    store = _store_with_two_chunks(tmp_path)
    vectors = FakeVectors(["urn:testpilot:fs-docs:chunk:d1#c0"])
    adapter = HybridSearchAdapter(store, FakeModel(), vectors)
    await adapter.search("物理键", kinds=("code_symbol",), limit=5)
    where = vectors.wheres[-1]
    assert where == {
        "$and": [
            {"branch": "main"},
            {"acl_scope": "default"},
            {"kind": {"$in": ["code_symbol"]}},
        ]
    }
    # FTS 侧真实过滤：text 切块在 code_symbol 过滤下 0 命中
    row = store.conn.execute("SELECT fts_urns FROM retrieval_events ORDER BY id DESC LIMIT 1").fetchone()
    assert json.loads(row["fts_urns"]) == []


async def test_vector_failure_degrades_to_fts_only_without_swallowing(tmp_path):
    store = _store_with_two_chunks(tmp_path)
    adapter = HybridSearchAdapter(
        store, FakeModel(fail=True), FakeVectors([]), project="p"
    )
    result = await adapter.search("物理键日志", limit=5)

    assert result.trace.degraded is True
    assert result.trace.vector_available is False and result.trace.fts_available is True
    # 错误不被吞：随留痕 raw_json 落库（下面对账）
    # FTS-only 仍有召回
    assert [c.urn for c in result.chunks][0].endswith("#c0")
    row = store.conn.execute("SELECT * FROM retrieval_events ORDER BY id DESC LIMIT 1").fetchone()
    assert row["degraded"] == 1 and json.loads(row["vector_urns"]) == []
    assert "ollama down" in json.loads(row["raw_json"])["vector_error"]


async def test_no_hits_anywhere_returns_empty_not_degraded(tmp_path):
    store = _store_with_two_chunks(tmp_path)
    adapter = HybridSearchAdapter(store, FakeModel(), FakeVectors([]))
    result = await adapter.search("完全不相关的一串话XYZ", limit=5)
    assert result.chunks == [] and result.scores == []
    assert result.degraded is False  # 真空 ≠ 降级
