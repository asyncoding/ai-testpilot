"""S2.6b FR-ING-004 关系构建 worker 集成测试（tmp 库 hermetic，不起端口）。

覆盖：四类输入 → 闭集关系全链路、指纹增量/--full、空库 fail-closed、
整组替换防幽灵、摄入关系隔离、取消用例计数与诊断字段；以及 2026-11-21
切片 A+B：路径⑤检索挂链（async 采集 → 0.50 低置信）、路径⑥ calls 传播
（relations 表 calls 边 → BFS 两跳 0.45/0.40）、指纹 v2 对新输入敏感。
"""

from __future__ import annotations

import pytest

from qa_fakes import FakeHybridSearch
from testpilot_adapters.store import bootstrap_store
from testpilot_core.case_relations import (
    RELATION_SCOPE_URN,
    RetrievalSymbolRef,
)
from testpilot_core.enums import OracleType
from testpilot_core.models import KnowledgeChunk, TestCase, TestPoint
from testpilot_core.oracle_mining import MinedOracle
from testpilot_core.requirement import (
    RequirementFeature,
    RequirementParseResult,
)
from testpilot_workers.relation_building import (
    collect_relation_inputs,
    run_relation_building,
)

_CASE_DOC = "urn:testpilot:fs-cases:document:cases/c.md"
_REQ_DOC = "urn:testpilot:fs-reqs:document:req.md"
_CASE1 = "urn:testpilot:fs-cases:test_case:cases/c.md#TC-1"
_CASE2 = "urn:testpilot:fs-cases:test_case:cases/c.md#TC-2"
_FR1 = "urn:testpilot:fs-reqs:requirement:req.md#FR-1"
_SYM1 = "urn:testpilot:fs-docs:symbol:a.swift#AlarmStore.arm"
_SYM2 = "urn:testpilot:fs-docs:symbol:a.swift#AlarmStore.disarm"
_SYM3 = "urn:testpilot:fs-docs:symbol:a.swift#Scheduler.tick"
_DOC1 = "urn:testpilot:fs-docs:document:a.swift"
_CHUNK1 = "urn:testpilot:fs-docs:chunk:a.swift#sym-1"


class _ShimContainer:
    def __init__(self, store):
        self._store = store

    def get(self, name):
        return self._store if name == "store" else None


class _Project:
    name = "demo"
    default_branch = "main"


class _Config:
    project = _Project()


class _IoContainer:
    """带 config 与可选 search 适配器的容器桩（对齐 orchestrator Container）。"""

    def __init__(self, store, search=None):
        self.config = _Config()
        self._adapters = {"store": store, "search": search}

    def get(self, name):
        return self._adapters.get(name)


def _symbol_chunk(urn: str = _CHUNK1, symbol_urn: str = _SYM1) -> KnowledgeChunk:
    return KnowledgeChunk(
        urn=urn, doc_urn=_DOC1, index=0, kind="code_symbol",
        text="func arm() 响铃调度实现", symbol_urn=symbol_urn,
        branch="main", content_hash=f"hc-{symbol_urn[-4:]}")


def _insert_calls_edge(store, source: str, target: str, tag: str) -> None:
    store.conn.execute(
        """INSERT INTO relations
           (urn, source_urn, target_urn, kind, confidence, evidence_json, doc_urn)
           VALUES (?,?,?,?,?, '{"lines": [], "urns": []}', ?)""",
        (f"urn:testpilot:fs-docs:rel:a.swift#{tag}", source, target,
         "calls", 0.9, _DOC1))
    store.conn.commit()


@pytest.fixture
def store(tmp_path):
    s = bootstrap_store(tmp_path)
    try:
        yield s
    finally:
        s.close()


def _seed_skeleton(store, *, fr_id="FR-1", urn=_FR1):
    store.upsert_requirement_skeleton(
        _REQ_DOC,
        RequirementParseResult(
            doc_urn=_REQ_DOC, external_id="req.md",
            features=[RequirementFeature(urn=urn, fr_id=fr_id, text="前台响铃")]),
        content_hash="h-req")


def _seed_case(store, urn=_CASE1, *, tags=("FR-1",), lifecycle="active"):
    store.upsert_test_case(
        _CASE_DOC,
        TestCase(urn=urn, external_id=urn.split("#")[-1], title="响铃用例",
                 steps=["步骤1"], tags=list(tags), lifecycle=lifecycle),
        content_hash="h-case")


def _seed_symbol_suggestion(store, case_urn=_CASE1, *, provenance="deterministic"):
    oracle = MinedOracle(
        type=OracleType.LOG_ASSERTION, expectation="日志含 arm() 调用",
        source_refs=[_SYM1], source_kind="code_symbol", provenance=provenance)
    store.upsert_oracle_suggestion(
        _CASE_DOC, case_urn, "c.md", oracle, content_hash="h-sug")


def test_full_pipeline_report_and_scope_rows(store):
    _seed_skeleton(store)
    _seed_case(store)
    _seed_symbol_suggestion(store)
    store.upsert_test_point(
        _REQ_DOC, "req.md",
        TestPoint(text="验证主流程", fr_id="FR-1", source_refs=[_FR1],
                  kind="functional"),
        content_hash="h-point")

    report = run_relation_building(_ShimContainer(store))
    assert report["status"] == "succeeded"
    assert report["cases_seen"] == 1 and report["cases_cancelled"] == 0
    assert report["relations_total"] == 2  # validates_requirement + references_symbol
    assert report["relations_by_kind"] == {
        "case_validates_requirement": 1, "case_references_symbol": 1}
    assert report["replaced"] == 2 and report["skipped_unchanged"] == 0
    assert len(report["input_hash"]) == 16

    rows = store.list_case_relations(include_low=True)
    assert {(r.kind, r.target_urn) for r in rows} == {
        ("case_validates_requirement", _FR1),
        ("case_references_symbol", _SYM1)}
    # 确定性符号关系 0.70 面向用户；低置信默认浏览口也能看到 FR 直连
    visible = store.list_case_relations()
    assert {r.target_urn for r in visible} == {_FR1, _SYM1}
    # 指纹已落 meta（v2：含检索/calls 输入与规则版本位）
    assert store.get_meta("case-relations-hash:v2")


def test_incremental_skip_then_full_rebuild(store):
    _seed_skeleton(store)
    _seed_case(store)
    first = run_relation_building(_ShimContainer(store))
    assert first["status"] == "succeeded"

    second = run_relation_building(_ShimContainer(store))
    assert second["status"] == "skipped_unchanged"
    assert second["replaced"] == 0 and second["skipped_unchanged"] == 1
    assert second["relations_total"] == first["relations_total"]

    third = run_relation_building(_ShimContainer(store), full=True)
    assert third["status"] == "succeeded" and third["replaced"] == 1


def test_empty_case_library_rejected(store):
    with pytest.raises(ValueError, match="用例库为空"):
        run_relation_building(_ShimContainer(store))


def test_rebuild_ghost_cleanup_and_ingestion_isolation(store):
    _seed_skeleton(store)
    _seed_case(store)
    _seed_symbol_suggestion(store)
    run_relation_building(_ShimContainer(store))
    assert store.count_case_relations() == 2

    # 摄入管线的 calls 关系（真实文档 doc_urn），整组替换不可误伤
    store.conn.execute(
        """INSERT INTO relations
           (urn, source_urn, target_urn, kind, confidence, evidence_json, doc_urn)
           VALUES (?,?,?,?,?, '{"lines": [], "urns": []}', ?)""",
        ("urn:testpilot:fs-docs:rel:doc#a->b", _SYM1,
         "urn:testpilot:fs-docs:symbol:a.swift#Other.arm",
         "calls", 0.9,
         "urn:testpilot:fs-docs:document:a.swift"))
    store.conn.commit()

    # 输入收缩：删掉用例的符号来源（替换为只有 FR tag 的新用例库状态）
    store.conn.execute("DELETE FROM oracle_suggestions").fetchall()
    store.conn.commit()
    report = run_relation_building(_ShimContainer(store), full=True)
    assert report["relations_total"] == 1
    scoped_kinds = {r["kind"] for r in store.conn.execute(
        "SELECT kind FROM relations WHERE doc_urn=?", (RELATION_SCOPE_URN,)).fetchall()}
    assert scoped_kinds == {"case_validates_requirement"}  # 符号关系幽灵被清扫
    assert store.conn.execute(
        "SELECT COUNT(*) AS n FROM relations WHERE kind='calls'").fetchone()["n"] == 1


def test_cancelled_case_skipped_and_model_symbol_low_confidence(store):
    _seed_skeleton(store)
    _seed_case(store, _CASE2, lifecycle="cancelled")
    _seed_symbol_suggestion(store, _CASE2, provenance="model")
    report = run_relation_building(_ShimContainer(store))
    assert report["cases_seen"] == 1 and report["cases_cancelled"] == 1
    assert report["relations_total"] == 0  # 取消用例的草稿不派生任何关系
    visible = store.list_case_relations()
    assert visible == []


def test_unknown_fr_tag_diagnostic_not_fabricated(store):
    _seed_skeleton(store)
    _seed_case(store, tags=("FR-404",))
    report = run_relation_building(_ShimContainer(store))
    assert report["unknown_fr_tags"] == ["FR-404"]
    assert report["relations_total"] == 0


# ── 切片 A：路径⑤检索挂链（async 采集 → 低置信关系） ──────────────────────────


async def test_collect_retrieval_refs_projects_symbol_hits(store):
    _seed_case(store)
    search = FakeHybridSearch([_symbol_chunk()])
    container = _IoContainer(store, search)

    refs, edges = await collect_relation_inputs(container, store.list_test_cases())

    assert edges == []  # 无 calls 关系时路径⑥输入为空
    assert refs == [RetrievalSymbolRef(
        case_urn=_CASE1, symbol_urn=_SYM1, chunk_urn=_CHUNK1)]
    # 采集参数契约：只召回 code_symbol、top-8、查询串以用例标题开头
    call = search.search_calls[0]
    assert call["kinds"] == ("code_symbol",) and call["limit"] == 8
    assert call["query"].startswith("响铃用例")


async def test_collect_without_search_adapter_returns_empty_refs(store):
    _seed_case(store)
    # stub profile：容器无 search 键，不报错，路径⑤空输入
    refs, edges = await collect_relation_inputs(
        _IoContainer(store), store.list_test_cases())
    assert refs == [] and edges == []


async def test_collect_skips_cancelled_case_and_dedupes_symbols(store):
    _seed_case(store, _CASE1)  # 活跃
    _seed_case(store, _CASE2, lifecycle="cancelled")
    # 同符号两个切块 + 一个无 symbol_urn 的 text 切块
    search = FakeHybridSearch([
        _symbol_chunk(),
        _symbol_chunk(urn=f"{_CHUNK1}-dup", symbol_urn=_SYM1),
        KnowledgeChunk(
            urn="urn:testpilot:fs-docs:chunk:a.swift#t1", doc_urn=_DOC1,
            index=2, kind="text", text="纯文本无符号",
            branch="main", content_hash="hc-t"),
    ])
    container = _IoContainer(store, search)

    refs, _ = await collect_relation_inputs(container, store.list_test_cases())

    assert len(search.search_calls) == 1  # 取消用例不发起检索
    assert refs == [RetrievalSymbolRef(
        case_urn=_CASE1, symbol_urn=_SYM1, chunk_urn=_CHUNK1)]


async def test_path5_end_to_end_low_confidence_hidden_from_browse(store):
    _seed_skeleton(store)
    _seed_case(store)
    search = FakeHybridSearch([_symbol_chunk()])
    container = _IoContainer(store, search)

    refs, edges = await collect_relation_inputs(container, store.list_test_cases())
    report = run_relation_building(
        container, retrieval_refs=refs, calls_edges=edges)

    assert report["retrieval_link_count"] == 1
    assert report["retrieval_refs_seen"] == 1 and report["calls_edges_seen"] == 0
    # 0.50 低于 0.60 门限：默认浏览口只见 0.90 的 FR 直连
    visible = store.list_case_relations()
    assert [r.kind for r in visible] == ["case_validates_requirement"]
    low_rows = [
        r for r in store.list_case_relations(include_low=True)
        if r.kind == "case_references_symbol"]
    assert len(low_rows) == 1
    low = low_rows[0]
    assert low.confidence == 0.50
    assert low.provenance == ["retrieval_link"]
    assert low.evidence_urns == [_CHUNK1, _SYM1]


# ── 切片 B：路径⑥ calls 传播（BFS 两跳，环安全在 core 单测另锁） ──────────────


async def test_path6_calls_propagation_end_to_end(store):
    _seed_skeleton(store)
    _seed_case(store)
    search = FakeHybridSearch([_symbol_chunk()])  # 种子：CASE1 → _SYM1
    _insert_calls_edge(store, _SYM1, _SYM2, "e1")  # hop1
    _insert_calls_edge(store, _SYM2, _SYM3, "e2")  # hop2
    container = _IoContainer(store, search)

    refs, edges = await collect_relation_inputs(container, store.list_test_cases())
    assert len(edges) == 2
    report = run_relation_building(
        container, retrieval_refs=refs, calls_edges=edges)

    assert report["calls_propagated_count"] == 2
    by_target = {
        r.target_urn: r for r in store.list_case_relations(include_low=True)
        if "calls_propagation" in r.provenance}
    assert set(by_target) == {_SYM2, _SYM3}
    assert by_target[_SYM2].confidence == 0.45  # hop1
    assert by_target[_SYM3].confidence == 0.40  # hop2
    # 传播产物全是低置信：默认浏览口仍然只有 FR 直连
    assert all(r.is_low_confidence for r in by_target.values())
    assert len(store.list_case_relations()) == 1


# ── 指纹 v2：新输入变化必须触发重建，相同输入仍跳过 ───────────────────────────


async def test_fingerprint_v2_reacts_to_retrieval_and_calls_inputs(store):
    _seed_skeleton(store)
    _seed_case(store)
    container = _IoContainer(store, FakeHybridSearch([_symbol_chunk()]))
    refs, edges = await collect_relation_inputs(container, store.list_test_cases())

    first = run_relation_building(
        container, retrieval_refs=refs, calls_edges=edges)
    assert first["status"] == "succeeded"
    second = run_relation_building(
        container, retrieval_refs=refs, calls_edges=edges)
    assert second["status"] == "skipped_unchanged"

    # 检索输入变化（多挂一个符号）→ 指纹失配，整组重建而非跳过
    other_ref = RetrievalSymbolRef(
        case_urn=_CASE1, symbol_urn=_SYM2, chunk_urn=f"{_CHUNK1}-other")
    third = run_relation_building(
        container, retrieval_refs=[*refs, other_ref], calls_edges=edges)
    assert third["status"] == "succeeded" and third["replaced"] > 0

    # calls 边变化同样触发重建
    _insert_calls_edge(store, _SYM1, _SYM3, "e3")
    refs2, edges2 = await collect_relation_inputs(
        container, store.list_test_cases())
    fourth = run_relation_building(
        container, retrieval_refs=refs2, calls_edges=edges2)
    assert fourth["status"] == "succeeded"
    assert fourth["calls_edges_seen"] == 1

