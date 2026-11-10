"""S2.6b FR-ING-004 用例关系存储与证据解析测试（复用 relations 表独立 scope）。

hermetic tmp 库，不起端口、不碰语料仓。覆盖：
- scope 整组替换/防幽灵/摄入关系隔离/坏 scope 回滚；
- 低置信默认隐藏与 kind 过滤；
- get_case_relation 的 scope 前缀守卫；
- get_evidence_rows 六来源解析 + 缺失 URN 不返回（dossier 标 unresolved）。
"""

from __future__ import annotations

import pytest

from testpilot_adapters.store import bootstrap_store
from testpilot_core.case_relations import (
    RELATION_SCOPE_URN,
    CaseRelation,
    CaseRelationError,
    case_relation_urn,
)
from testpilot_core.enums import OracleType, StaleStatus
from testpilot_core.models import (
    Entity,
    KnowledgeChunk,
    SourceDocument,
    TestCase,
    TestPoint,
)
from testpilot_core.oracle_mining import MinedOracle
from testpilot_core.requirement import (
    RequirementAcceptance,
    RequirementFeature,
    RequirementParseResult,
    RequirementRule,
)
from testpilot_core.testpoint import acceptance_urn, point_urn

_CASE = "urn:testpilot:fs-cases:test_case:cases/c.md#TC-1"
_FR1 = "urn:testpilot:fs-reqs:requirement:req.md#FR-1"
_SYM1 = "urn:testpilot:fs-docs:symbol:a.swift#AlarmStore.arm"
_SUG1 = "urn:testpilot:fs-cases:oracle_suggestion:cases/c.md#abc1234567"
_POINT1 = "urn:testpilot:fs-reqs:test_point:req.md#FR-1-aaaa1111bb"
_CHUNK1 = "urn:testpilot:fs-docs:chunk:docs/incident.md#i-0"
_DOC1 = "urn:testpilot:fs-docs:document:docs/incident.md"


def _rel(target: str, *, kind: str = "case_validates_requirement",
         confidence: float = 0.9, evidence=None, provenance=None) -> CaseRelation:
    return CaseRelation(
        urn=case_relation_urn(_CASE, kind, target),
        source_urn=_CASE, target_urn=target, kind=kind,  # type: ignore[arg-type]
        confidence=confidence,
        evidence_urns=evidence or [_CASE],
        provenance=provenance or ["fr_tag"],  # type: ignore[arg-type]
    )


@pytest.fixture
def store(tmp_path):
    s = bootstrap_store(tmp_path)
    try:
        yield s
    finally:
        s.close()


# ── 整组替换与隔离 ───────────────────────────────────────────────────────────

def test_replace_and_read_back_round_trip(store):
    n = store.replace_case_relations(RELATION_SCOPE_URN, [_rel(_FR1)])
    assert n == 1 and store.count_case_relations() == 1
    got = store.list_case_relations()[0]
    assert got.target_urn == _FR1 and got.confidence == 0.9
    assert got.evidence_urns == [_CASE] and got.provenance == ["fr_tag"]


def test_scope_replacement_never_touches_ingestion_relations(store):
    # 摄入管线的关系：doc_urn 是真实文档 URN
    store.conn.execute(
        """INSERT INTO relations
           (urn, source_urn, target_urn, kind, confidence, evidence_json, doc_urn)
           VALUES (?,?,?,?,?, '{"lines": [], "urns": []}', ?)""",
        ("urn:testpilot:fs-docs:rel:doc#a->b", _SYM1,
         "urn:testpilot:fs-docs:symbol:a.swift#Other.arm",
         "calls", 0.9, _DOC1),
    )
    store.conn.commit()

    store.replace_case_relations(RELATION_SCOPE_URN, [_rel(_FR1)])
    store.replace_case_relations(  # 第二次替换：清扫本 scope 旧行
        RELATION_SCOPE_URN, [_rel(_SYM1, kind="case_references_symbol",
                                  confidence=0.7, evidence=[_SUG1, _SYM1])])

    ingestion = store.conn.execute(
        "SELECT kind FROM relations WHERE doc_urn=?", (_DOC1,)).fetchall()
    assert [r["kind"] for r in ingestion] == ["calls"]  # 摄入关系零误伤
    scoped = store.list_case_relations(include_low=True)
    assert len(scoped) == 1 and scoped[0].kind == "case_references_symbol"


def test_bad_scope_raises_and_rolls_back(store):
    store.replace_case_relations(RELATION_SCOPE_URN, [_rel(_FR1)])
    with pytest.raises(CaseRelationError):
        store.replace_case_relations(_DOC1, [_rel(_SYM1)])
    # 回滚后原 scope 数据无损
    assert store.count_case_relations() == 1


def test_ghost_cleanup_on_rebuild(store):
    store.replace_case_relations(RELATION_SCOPE_URN, [
        _rel(_FR1),
        _rel(_SYM1, kind="case_references_symbol", confidence=0.7),
    ])
    assert store.count_case_relations() == 2
    store.replace_case_relations(RELATION_SCOPE_URN, [_rel(_FR1)])
    remaining = store.list_case_relations(include_low=True)
    assert [r.target_urn for r in remaining] == [_FR1]


# ── 浏览口过滤 ───────────────────────────────────────────────────────────────

def test_low_confidence_hidden_by_default_and_kind_filter(store):
    high = _rel(_FR1, confidence=0.9)
    low = _rel(_SYM1, kind="case_references_symbol", confidence=0.5,
               evidence=[_SYM1], provenance=["test_point_fr"])
    store.replace_case_relations(RELATION_SCOPE_URN, [high, low])

    assert len(store.list_case_relations()) == 1
    assert store.list_case_relations()[0].target_urn == _FR1
    assert len(store.list_case_relations(include_low=True)) == 2
    only_symbols = store.list_case_relations(
        kind="case_references_symbol", include_low=True)
    assert [r.kind for r in only_symbols] == ["case_references_symbol"]


def test_get_relation_scope_guard(store):
    store.replace_case_relations(RELATION_SCOPE_URN, [_rel(_FR1)])
    got = store.get_case_relation(case_relation_urn(
        _CASE, "case_validates_requirement", _FR1))
    assert got is not None and got.target_urn == _FR1
    assert store.get_case_relation("urn:testpilot:fs-docs:rel:doc#a->b") is None
    assert store.get_case_relation("urn:testpilot:case-relations:rel:missing") is None


# ── 证据解析六来源 ─────────────────────────────────────────────────────────

def _seed_evidence_sources(store):
    # ① 切块 + 所属文档
    store.upsert_document(SourceDocument(
        urn=_DOC1, source_system="fs-docs", uri="file:///docs/incident.md",
        content_hash="h-doc"))
    store.upsert_chunk(KnowledgeChunk(
        urn=_CHUNK1, doc_urn=_DOC1, index=0, kind="incident",
        text="复盘结论：闹钟必须在前台持续响铃。", content_hash="h-chunk",
        stale=StaleStatus.FRESH))
    # ② 符号实体
    entity = Entity(urn=_SYM1, kind="code_symbol", name="AlarmStore.arm",
                    metadata_={"signature": "func arm()", "file": "a.swift"})
    store.conn.execute(
        "INSERT INTO entities(urn, kind, name, branch, doc_urn, raw_json) "
        "VALUES (?,?,?,?,?,?)",
        (_SYM1, "code_symbol", entity.name, "main", _DOC1,
         entity.model_dump_json(by_alias=True)))
    store.conn.commit()
    # ③ 用例
    store.upsert_test_case(
        "urn:testpilot:fs-cases:document:cases/c.md",
        TestCase(urn=_CASE, external_id="c.md#TC-1", title="前台响铃",
                 steps=["打开闹钟", "触发时间到"], tags=["FR-1"]),
        content_hash="h-case")
    # ④ 测试点
    point = TestPoint(text="验证主流程", fr_id="FR-1",
                      source_refs=[_FR1], kind="functional")
    store.upsert_test_point(
        "urn:testpilot:fs-reqs:document:req.md", "req.md", point,
        content_hash="h-point")
    # ⑤ oracle 草稿
    oracle = MinedOracle(
        type=OracleType.UI_ASSERTION, expectation="全屏提醒并持续响铃",
        source_refs=[_FR1], source_kind="requirement_ac")
    store.upsert_oracle_suggestion(
        "urn:testpilot:fs-cases:document:cases/c.md", _CASE, "c.md", oracle,
        content_hash="h-sug")
    # ⑥ 需求骨架（FR/NFR/AC 三类 URN）
    skel = RequirementParseResult(
        doc_urn="urn:testpilot:fs-reqs:document:req.md", external_id="req.md",
        features=[RequirementFeature(urn=_FR1, fr_id="FR-1", text="前台响铃提醒")],
        rules=[RequirementRule(
            urn="urn:testpilot:fs-reqs:requirement_rule:req.md#NFR-1",
            rule_id="NFR-1", text="响铃延迟 <1s")],
        acceptance=[RequirementAcceptance(index=0, text="AC 文本")])
    store.upsert_requirement_skeleton(
        "urn:testpilot:fs-reqs:document:req.md", skel, content_hash="h-req")
    return skel, point, oracle


def test_get_evidence_rows_resolves_all_sources(store):
    skel, point, oracle = _seed_evidence_sources(store)
    point_urn_value = point_urn("req.md", point)
    sug_urn_value = store.conn.execute(
        "SELECT urn FROM oracle_suggestions WHERE case_urn=?", (_CASE,)).fetchone()["urn"]
    ac_urn_value = acceptance_urn("req.md", skel.acceptance[0])
    rule_urn = "urn:testpilot:fs-reqs:requirement_rule:req.md#NFR-1"

    rows = store.get_evidence_rows([
        _CHUNK1, _SYM1, _CASE, point_urn_value, sug_urn_value, _FR1, rule_urn,
        ac_urn_value, "urn:testpilot:fs-docs:chunk:missing#x"])
    by_urn = {r.urn: r for r in rows}
    assert set(by_urn) == {
        _CHUNK1, _SYM1, _CASE, point_urn_value, sug_urn_value,
        _FR1, rule_urn, ac_urn_value}  # 缺失 URN 不返回
    assert by_urn[_CHUNK1].kind == "incident" and by_urn[_CHUNK1].doc_uri
    assert by_urn[_SYM1].label == "AlarmStore.arm"
    assert by_urn[_CASE].kind == "test_case" and by_urn[_CASE].label.startswith("c.md#TC-1")
    assert by_urn[point_urn_value].kind == "test_point"
    assert by_urn[sug_urn_value].kind == "oracle_suggestion"
    assert by_urn[_FR1].kind == "requirement" and "FR-1" in by_urn[_FR1].label
    assert by_urn[rule_urn].kind == "requirement_rule"
    assert by_urn[ac_urn_value].kind == "requirement_ac"
    assert all(r.status == "resolved" for r in rows)


def test_get_evidence_rows_empty_and_dedup(store):
    assert store.get_evidence_rows([]) == []
    _seed_evidence_sources(store)
    rows = store.get_evidence_rows([_CASE, _CASE])
    assert len(rows) == 1 and rows[0].urn == _CASE


def test_list_test_point_rows_carries_external_id(store):
    _seed_evidence_sources(store)
    rows = store.list_test_point_rows()
    assert len(rows) == 1
    assert rows[0]["external_id"] == "req.md"
    assert rows[0]["point"].fr_id == "FR-1"
