"""S2.6b FR-ING-004 关系构建 worker 集成测试（tmp 库 hermetic，不起端口）。

覆盖：四类输入 → 闭集关系全链路、指纹增量/--full、空库 fail-closed、
整组替换防幽灵、摄入关系隔离、取消用例计数与诊断字段。
"""

from __future__ import annotations

import pytest

from testpilot_adapters.store import bootstrap_store
from testpilot_core.case_relations import RELATION_SCOPE_URN
from testpilot_core.enums import OracleType
from testpilot_core.models import TestCase, TestPoint
from testpilot_core.oracle_mining import MinedOracle
from testpilot_core.requirement import (
    RequirementFeature,
    RequirementParseResult,
)
from testpilot_workers.relation_building import run_relation_building

_CASE_DOC = "urn:testpilot:fs-cases:document:cases/c.md"
_REQ_DOC = "urn:testpilot:fs-reqs:document:req.md"
_CASE1 = "urn:testpilot:fs-cases:test_case:cases/c.md#TC-1"
_CASE2 = "urn:testpilot:fs-cases:test_case:cases/c.md#TC-2"
_FR1 = "urn:testpilot:fs-reqs:requirement:req.md#FR-1"
_SYM1 = "urn:testpilot:fs-docs:symbol:a.swift#AlarmStore.arm"


class _ShimContainer:
    def __init__(self, store):
        self._store = store

    def get(self, name):
        return self._store if name == "store" else None


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
    # 指纹已落 meta
    assert store.get_meta("case-relations-hash:v1")


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
