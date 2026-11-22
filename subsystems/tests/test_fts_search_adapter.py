"""FtsSearchAdapter 契约（S1.4 补齐）：doc_* 折叠、FTS5 裸问题整形、branch/acl 下推。

历史背景：该适配器是 offline-demo profile 的真实检索通道，S1.4 探针首次跑通
生产装配路径时照出两个洞——原始问题直塞 MATCH 炸链（已修，见整形用例）、
水合只取 raw_json 丢 doc_authority 导致 FR-QA-003 L2 证词识别失效（本文件首测守卫）。
"""

from __future__ import annotations

from testpilot_adapters.fts_search import FtsSearchAdapter
from testpilot_adapters.store import bootstrap_store
from testpilot_core.answering import extract_history_testimony, to_evidence_dict
from testpilot_core.enums import AuthorityLevel
from testpilot_core.models import KnowledgeChunk, SourceDocument

DOC = "urn:testpilot:fs-docs:document:docs/postmortem.md"
DOC2 = "urn:testpilot:fs-docs:document:docs/restricted.md"
CHUNK = "urn:testpilot:fs-docs:chunk:docs/postmortem.md#section-one"


def _doc(urn: str = DOC, *, acl: str = "default",
         authority=AuthorityLevel.L2_EXPERIENCE) -> SourceDocument:
    return SourceDocument(
        urn=urn, source_system="fs-docs",
        uri=f"file:///repo/{urn.split(':', 4)[-1]}",
        title="postmortem", version="h1", content_hash="hash1",
        authority_level=authority, acl_scope=acl,
    )


def _chunk(text: str, *, urn: str = CHUNK, doc_urn: str = DOC,
           branch: str = "main", index: int = 0) -> KnowledgeChunk:
    return KnowledgeChunk(
        urn=urn, doc_urn=doc_urn, index=index, kind="text", text=text,
        branch=branch, content_hash=f"ch-{urn[-1]}",
    )


def _adapter(store) -> FtsSearchAdapter:
    return FtsSearchAdapter(store, project="demo")


async def test_doc_authority_uri_hash_folded_into_chunk_metadata(tmp_path):
    store = bootstrap_store(tmp_path / "data")
    store.upsert_document(_doc())
    store.upsert_chunk(_chunk("侧边键停钟的四层兜底机制说明"))
    try:
        chunks = await _adapter(store).search(
            "侧边键停钟的兜底机制是什么", project="demo")
        assert len(chunks) == 1
        meta = chunks[0].metadata_
        assert meta["doc_authority"] == AuthorityLevel.L2_EXPERIENCE.value
        assert meta["doc_uri"].endswith("docs/postmortem.md")
        assert meta["doc_content_hash"] == "hash1"

        # FR-QA-003 闭环：折叠后的证据块能被识别为 L2 历史证词
        history = extract_history_testimony([to_evidence_dict(chunks[0])])
        assert len(history) == 1 and history[0].doc_urn == DOC
    finally:
        store.close()


async def test_raw_punctuation_question_is_shaped_without_raising(tmp_path):
    # . : " * ( ) 是 FTS5 语法字符；旧实现直塞 MATCH 抛 OperationalError 炸断问答链
    store = bootstrap_store(tmp_path / "data")
    store.upsert_document(_doc(authority=AuthorityLevel.L1_AUTHORITATIVE))
    store.upsert_chunk(_chunk("C.keep() 方法的实现逻辑与调度说明"))
    try:
        chunks = await _adapter(store).search(
            'C.keep() 的实现逻辑是什么？: " * ( )', project="demo")
        assert chunks and chunks[0].urn == CHUNK
    finally:
        store.close()


async def test_kinds_filter_is_pushed_down(tmp_path):
    # 路径⑤检索挂链只用 code_symbol 召回：kinds 透传 SQL 层，text 切块不可混入
    store = bootstrap_store(tmp_path / "data")
    store.upsert_document(_doc())
    sym_chunk_urn = "urn:testpilot:fs-docs:chunk:docs/a.swift#sym-1"
    symbol_urn = "urn:testpilot:fs-docs:symbol:docs/a.swift#Scheduler.runReconcile"
    # 水合走 raw_json，kind/symbol_urn 必须随模型写入（不能只改列）
    store.upsert_chunk(KnowledgeChunk(
        urn=sym_chunk_urn, doc_urn=DOC, index=0, kind="code_symbol",
        text="runReconcile 唤醒伴随后台对账调度", symbol_urn=symbol_urn,
        branch="main", content_hash="ch-sym"))
    store.upsert_chunk(_chunk(
        "runReconcile 机制的文字说明与事故复盘",
        urn=f"{CHUNK}-text", index=1))
    try:
        adapter = _adapter(store)
        all_hits = await adapter.search("runReconcile 唤醒伴随", project="demo")
        assert {c.urn for c in all_hits} == {sym_chunk_urn, f"{CHUNK}-text"}

        symbol_only = await adapter.search(
            "runReconcile 唤醒伴随", project="demo",
            kinds=("code_symbol",))
        assert len(symbol_only) == 1
        assert symbol_only[0].kind == "code_symbol"
        assert symbol_only[0].symbol_urn == symbol_urn
    finally:
        store.close()


async def test_branch_and_acl_filters_are_pushed_down(tmp_path):
    store = bootstrap_store(tmp_path / "data")
    store.upsert_document(_doc())
    store.upsert_chunk(_chunk("主分支锁屏按键日志证据"))
    store.upsert_chunk(_chunk(
        "另一分支的锁屏按键日志证据", urn=f"{CHUNK}-b2",
        branch="alarmkit-spike", index=1))
    store.upsert_document(_doc(DOC2, acl="restricted"))
    store.upsert_chunk(_chunk(
        "受限密级的锁屏按键日志证据", urn=f"{CHUNK}-b3", doc_urn=DOC2))
    try:
        hits = await _adapter(store).search(
            "锁屏按键日志证据", project="demo",
            branch="main", acl_scope="default")
        urns = {c.urn for c in hits}
        assert CHUNK in urns
        assert f"{CHUNK}-b2" not in urns  # 分支过滤
        assert f"{CHUNK}-b3" not in urns  # 密级过滤
    finally:
        store.close()
