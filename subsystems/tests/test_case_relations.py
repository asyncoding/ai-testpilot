"""S2.6b FR-ING-004(部分) 用例关系派生纯逻辑测试（零 IO、零三方）。

钉死四条派生路径的置信度、合并语义、低置信阈值与 fail-closed 行为；
store/worker/API 的 IO 集成在各自测试文件。
"""

from __future__ import annotations

import pytest

from testpilot_core.case_relations import (
    LOW_CONFIDENCE_THRESHOLD,
    RELATION_SCOPE_URN,
    CaseRelation,
    CaseRelationError,
    EvidenceRow,
    PointRef,
    SuggestionRef,
    assemble_evidence_dossier,
    build_case_relations,
    build_fr_index,
    case_relation_urn,
    classify_resource,
    ensure_relation_scope,
)
from testpilot_core.models import TestCase

_CASE = "urn:testpilot:fs-cases:test_case:cases/c.md#TC-1"
_CASE2 = "urn:testpilot:fs-cases:test_case:cases/c.md#TC-2"
_CANCELLED = "urn:testpilot:fs-cases:test_case:cases/c.md#TC-X"
_FR1 = "urn:testpilot:fs-reqs:requirement:req.md#FR-1"
_AC1 = "urn:testpilot:fs-reqs:requirement:req.md#AC-1"
_RULE1 = "urn:testpilot:fs-reqs:requirement_rule:req.md#NFR-1"
_SYM1 = "urn:testpilot:fs-docs:symbol:a.swift#MissionCoordinator.schedulePrimary"
_SYM2 = "urn:testpilot:fs-docs:symbol:a.swift#AlarmStore.arm"
_SUG1 = "urn:testpilot:fs-cases:oracle_suggestion:cases/c.md#abc1234567"
_POINT1 = "urn:testpilot:fs-reqs:test_point:req.md#FR-1-aaaa1111bb"


def _case(urn: str = _CASE, *, tags=("FR-1",), lifecycle: str = "active") -> TestCase:
    return TestCase(
        urn=urn, external_id="c.md#TC-1", title="t", steps=["s"],
        tags=list(tags), lifecycle=lifecycle,  # type: ignore[arg-type]
    )


def _index() -> dict[str, str]:
    return {"FR-1": _FR1}


# ── URN / 工具 ──────────────────────────────────────────────────────────────

def test_relation_urn_is_content_addressed_and_stable():
    u1 = case_relation_urn(_CASE, "case_references_symbol", _SYM1)
    u2 = case_relation_urn(_CASE, "case_references_symbol", _SYM1)
    u3 = case_relation_urn(_CASE, "case_references_symbol", _SYM2)
    assert u1 == u2 and u1.startswith("urn:testpilot:case-relations:rel:")
    assert u1 != u3  # 目标不同 URN 必须可区分


def test_classify_resource_and_scope_guard():
    assert classify_resource(_SYM1) == "symbol"
    assert classify_resource(_FR1) == "requirement"
    assert classify_resource("not-a-urn") is None
    ensure_relation_scope(RELATION_SCOPE_URN)
    with pytest.raises(CaseRelationError):
        ensure_relation_scope("urn:testpilot:fs-docs:document:req.md")


def test_build_fr_index_detects_cross_doc_collision():
    index, ambiguous = build_fr_index([
        ("req-a.md", [("FR-1", "urn:testpilot:fs-reqs:requirement:req-a.md#FR-1")]),
        ("req-b.md", [("FR-1", "urn:testpilot:fs-reqs:requirement:req-b.md#FR-1")]),
        ("req-b.md", [("FR-2", "urn:testpilot:fs-reqs:requirement:req-b.md#FR-2")]),
    ])
    assert "FR-1" not in index  # 裸 tag 消歧不了 → fail-closed
    assert ambiguous == ["FR-1"]
    assert index["FR-2"].endswith("FR-2")


# ── 路径①：FR tag ───────────────────────────────────────────────────────────

def test_path1_fr_tag_direct_relation_high_confidence():
    result = build_case_relations(
        cases=[_case()], fr_index=_index(), suggestions=[], points=[])
    assert len(result.relations) == 1
    rel = result.relations[0]
    assert (rel.source_urn, rel.kind, rel.target_urn) == (
        _CASE, "case_validates_requirement", _FR1)
    assert rel.confidence == 0.90 and not rel.is_low_confidence
    assert rel.provenance == ["fr_tag"] and rel.evidence_urns == [_CASE]


def test_unknown_fr_tag_recorded_not_fabricated():
    result = build_case_relations(
        cases=[_case(tags=("FR-404",))], fr_index=_index(),
        suggestions=[], points=[])
    assert result.relations == []
    assert result.unknown_fr_tags == ["FR-404"]


def test_cancelled_cases_skipped_and_counted():
    result = build_case_relations(
        cases=[_case(), _case(_CANCELLED, lifecycle="cancelled")],
        fr_index=_index(), suggestions=[], points=[])
    assert result.cases_seen == 2 and result.cases_cancelled == 1
    assert {r.source_urn for r in result.relations} == {_CASE}


# ── 路径②：oracle 草稿 ───────────────────────────────────────────────────────

def test_path2_suggestion_requirement_deterministic_vs_model():
    sug_d = SuggestionRef(
        case_urn=_CASE, urn=_SUG1, provenance="deterministic",
        source_kind="requirement_ac", source_refs=[_AC1])
    sug_m = SuggestionRef(
        case_urn=_CASE, urn=_SUG1 + "m", provenance="model",
        source_kind="model_proposed", source_refs=[_RULE1])
    result = build_case_relations(
        cases=[_case(tags=())], fr_index=_index(),
        suggestions=[sug_d, sug_m], points=[])
    by_target = {r.target_urn: r for r in result.relations}
    assert by_target[_AC1].confidence == 0.80
    assert by_target[_AC1].provenance == ["oracle_suggestion"]
    model_rel = by_target[_RULE1]
    assert model_rel.confidence == 0.55 and model_rel.is_low_confidence


def test_path2_suggestion_symbol_confidence_and_evidence():
    sug = SuggestionRef(
        case_urn=_CASE, urn=_SUG1, provenance="deterministic",
        source_kind="code_symbol", source_refs=[_SYM1])
    result = build_case_relations(
        cases=[_case(tags=())], fr_index=_index(), suggestions=[sug], points=[])
    rel = result.relations[0]
    assert rel.kind == "case_references_symbol" and rel.confidence == 0.70
    assert rel.evidence_urns == sorted([_SUG1, _SYM1])

    # 模型源符号引用降为低置信
    sug.provenance = "model"
    result2 = build_case_relations(
        cases=[_case(tags=())], fr_index=_index(), suggestions=[sug], points=[])
    assert result2.relations[0].confidence == 0.50
    assert result2.relations[0].is_low_confidence


def test_suggestion_on_cancelled_case_ignored():
    sug = SuggestionRef(
        case_urn=_CANCELLED, urn=_SUG1, source_refs=[_SYM1])
    result = build_case_relations(
        cases=[_case(_CANCELLED, lifecycle="cancelled")],
        fr_index=_index(), suggestions=[sug], points=[])
    assert result.relations == []


def test_unparseable_ref_recorded_and_skipped():
    sug = SuggestionRef(
        case_urn=_CASE, urn=_SUG1,
        source_refs=["裸ID-FR-1", "https://elsewhere/x", _SYM1])
    result = build_case_relations(
        cases=[_case(tags=())], fr_index=_index(), suggestions=[sug], points=[])
    assert len(result.relations) == 1  # 只有合法符号 URN 成关系
    assert result.relations[0].target_urn == _SYM1
    assert set(result.unparseable_refs) == {"裸ID-FR-1", "https://elsewhere/x"}


# ── 路径③④：测试点 ───────────────────────────────────────────────────────────

def test_path3_point_impact_inverse_requirement_relation():
    point = PointRef(urn=_POINT1, fr_id="FR-1", source_refs=[_CASE])
    result = build_case_relations(
        cases=[_case(tags=())], fr_index=_index(),
        suggestions=[], points=[point])
    rel = result.relations[0]
    assert (rel.source_urn, rel.kind, rel.target_urn) == (
        _CASE, "case_validates_requirement", _FR1)
    assert rel.confidence == 0.60 and rel.provenance == ["test_point_impact"]


def test_path4_fr_neighbor_symbol_transitive_is_low():
    point = PointRef(urn=_POINT1, fr_id="FR-1", source_refs=[_SYM1, _SYM2])
    result = build_case_relations(
        cases=[_case()], fr_index=_index(), suggestions=[], points=[point])
    symbol_rels = [r for r in result.relations if r.kind == "case_references_symbol"]
    assert {r.target_urn for r in symbol_rels} == {_SYM1, _SYM2}
    assert all(r.confidence == 0.50 and r.is_low_confidence for r in symbol_rels)
    assert all(r.provenance == ["test_point_fr"] for r in symbol_rels)


# ── 合并语义 ─────────────────────────────────────────────────────────────────

def test_merge_same_triple_union_evidence_max_confidence_union_provenance():
    # 同 case→FR-1：fr_tag(0.9) 与 point impact(0.6) 合并为一条
    point = PointRef(urn=_POINT1, fr_id="FR-1", source_refs=[_CASE])
    result = build_case_relations(
        cases=[_case()], fr_index=_index(), suggestions=[], points=[point])
    fr_rels = [r for r in result.relations
               if r.kind == "case_validates_requirement" and r.target_urn == _FR1]
    assert len(fr_rels) == 1
    rel = fr_rels[0]
    assert rel.confidence == 0.90
    assert set(rel.evidence_urns) == {_CASE, _POINT1}
    assert set(rel.provenance) == {"fr_tag", "test_point_impact"}


def test_deterministic_ordering_and_threshold_constant():
    sug = SuggestionRef(case_urn=_CASE, urn=_SUG1, source_refs=[_SYM1])
    result = build_case_relations(
        cases=[_case(), _case(_CASE2, tags=("FR-1",))],
        fr_index=_index(), suggestions=[sug], points=[])
    keys = [(r.source_urn, r.kind, r.target_urn) for r in result.relations]
    assert keys == sorted(keys)
    assert LOW_CONFIDENCE_THRESHOLD == 0.60
    # 0.60 的 point impact 恰好不低，0.55/0.50 低
    assert all(0.0 <= r.confidence <= 1.0 for r in result.relations)


# ── 证据 dossier fail-closed ─────────────────────────────────────────────────

def test_assemble_dossier_marks_unresolved_without_fabrication():
    rel = CaseRelation(
        urn=case_relation_urn(_CASE, "case_references_symbol", _SYM1),
        source_urn=_CASE, target_urn=_SYM1,
        kind="case_references_symbol", confidence=0.7,
        evidence_urns=[_SUG1, _SYM1])
    dossier = assemble_evidence_dossier(rel, [
        EvidenceRow(urn=_SYM1, status="resolved", kind="code_symbol",
                    label="schedulePrimary", excerpt="func schedulePrimary()")
    ])
    assert dossier.complete is False
    by_status = {item.urn: item.status for item in dossier.items}
    assert by_status[_SYM1] == "resolved" and by_status[_SUG1] == "unresolved"
    unresolved = next(i for i in dossier.items if i.status == "unresolved")
    assert unresolved.label == "" and unresolved.excerpt == ""  # 不编造原文


def test_assemble_dossier_complete_when_all_resolved():
    rel = CaseRelation(
        urn=case_relation_urn(_CASE, "case_validates_requirement", _FR1),
        source_urn=_CASE, target_urn=_FR1,
        kind="case_validates_requirement", confidence=0.9,
        evidence_urns=[_CASE])
    dossier = assemble_evidence_dossier(rel, [
        EvidenceRow(urn=_CASE, status="resolved", kind="test_case",
                    label="TC-1", excerpt="步骤")])
    assert dossier.complete is True and len(dossier.items) == 1
