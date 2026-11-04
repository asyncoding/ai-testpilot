"""schema v3 测试：unicode61→trigram 老库迁移、trigram FTS+LIKE 检索、
检索留痕（docs/08 FR-KNO-001）。"""

from testpilot_adapters.store import bootstrap_store
from testpilot_core.enums import AuthorityLevel
from testpilot_core.models import KnowledgeChunk, SourceDocument


def _doc(urn: str = "urn:testpilot:fs-docs:document:d1@h1") -> SourceDocument:
    return SourceDocument(
        urn=urn,
        source_system="fs-docs",
        uri="file:///tmp/d1.md",
        title="d1",
        version="h1",
        content_hash="hash1",
        authority_level=AuthorityLevel.L2_EXPERIENCE,
    )


def _chunk(text: str, *, urn: str = "urn:testpilot:fs-docs:chunk:d1#c0",
           kind: str = "text", branch: str = "main", index: int = 0) -> KnowledgeChunk:
    return KnowledgeChunk(
        urn=urn,
        doc_urn="urn:testpilot:fs-docs:document:d1@h1",
        index=index,
        kind=kind,
        text=text,
        quality_score=0.7,
        branch=branch,
        content_hash="c1",
    )


def test_v2_unicode61_fts_is_migrated_and_rebuilt_from_chunks(tmp_path):
    # 先在 v3 下写一条切块
    store = bootstrap_store(tmp_path)
    store.upsert_document(_doc())
    store.upsert_chunk(_chunk("锁屏下按物理键会打出什么日志"))
    # 模拟 v2 老库：FTS 表是 unicode61
    store.conn.execute("DROP TABLE chunks_fts")
    store.conn.execute(
        "CREATE VIRTUAL TABLE chunks_fts USING fts5(text, chunk_urn UNINDEXED, "
        "tokenize='unicode61')"
    )
    store.conn.execute("INSERT INTO chunks_fts(text, chunk_urn) SELECT text, urn FROM chunks")
    store.conn.execute("UPDATE meta SET value='2' WHERE key='schema_version'")
    store.close()

    # 重新打开：迁移应自动 DROP 重建为 trigram 并回填
    store = bootstrap_store(tmp_path)
    try:
        sql = store.conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='chunks_fts'"
        ).fetchone()["sql"]
        assert "trigram" in sql
        # trigram 下自然语言三元串可命中（unicode61 下整段成词，必 0 命中）
        rows = store.search_fts_matches("物理键", [], limit=5)
        assert [r["urn"] for r in rows] == ["urn:testpilot:fs-docs:chunk:d1#c0"]
        assert store.get_meta("schema_version") == "3"
    finally:
        store.close()


def test_fts_matches_trigram_and_like_fallback(tmp_path):
    store = bootstrap_store(tmp_path)
    store.upsert_document(_doc())
    store.upsert_chunk(_chunk("锁屏 LA 卡片：按物理键的日志证据"))
    try:
        # 三元串 MATCH
        assert store.search_fts_matches("物理键", [])[0]["urn"].endswith("#c0")
        # 2 字符短词 trigram 不索引 → LIKE 兜底
        rows = store.search_fts_matches("", ["LA"])
        assert rows and rows[0]["urn"].endswith("#c0")
        # LIKE 的 % 转义：查百分号本身不应当通配
        assert store.search_fts_matches("", ["%"]) == []
        # 两路 OR
        rows = store.search_fts_matches("物理键", ["LA"])
        assert len(rows) == 1
    finally:
        store.close()


def test_fts_matches_filters_branch_and_kind(tmp_path):
    store = bootstrap_store(tmp_path)
    store.upsert_document(_doc())
    store.upsert_chunk(_chunk("物理键日志", kind="text"))
    store.upsert_chunk(
        _chunk("物理键符号", urn="urn:x#s1", kind="code_symbol", index=1)
    )
    try:
        assert len(store.search_fts_matches("物理键", [], kinds=("code_symbol",))) == 1
        # 分支不匹配：查不到（切块在 main）
        assert store.search_fts_matches("物理键", [], branch="feature-x") == []
    finally:
        store.close()


def test_load_retrieval_chunks_preserves_order_and_doc_meta(tmp_path):
    store = bootstrap_store(tmp_path)
    store.upsert_document(_doc())
    store.upsert_chunk(_chunk("第一条物理键"))
    store.upsert_chunk(_chunk("第二条", urn="urn:x#c2", index=1))
    try:
        out = store.load_retrieval_chunks(["urn:x#c2", "urn:testpilot:fs-docs:chunk:d1#c0"])
        assert [c["urn"] for c in out] == ["urn:x#c2", "urn:testpilot:fs-docs:chunk:d1#c0"]
        assert out[0]["doc_uri"] == "file:///tmp/d1.md"
        assert out[0]["doc_content_hash"] == "hash1"
    finally:
        store.close()


def test_record_retrieval_event_roundtrip(tmp_path):
    store = bootstrap_store(tmp_path)
    try:
        event_id = store.record_retrieval_event(
            query="物理键会怎么样", query_sha="abc", project="AlarmKitSpike",
            branch="main", acl_scope="default", kinds=(),
            model_version="dmeta-embedding-zh",
            fts_available=True, vector_available=False, degraded=True,
            fts_urns=["u1"], vector_urns=[], fused_urns=["u1"],
            fts_latency_ms=3, vector_latency_ms=0,
        )
        row = store.conn.execute(
            "SELECT * FROM retrieval_events WHERE id=?", (event_id,)
        ).fetchone()
        assert row["degraded"] == 1 and row["vector_available"] == 0
        assert row["fts_urns"] == '["u1"]'
        assert row["model_version"] == "dmeta-embedding-zh"
    finally:
        store.close()
