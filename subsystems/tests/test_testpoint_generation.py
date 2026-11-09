"""FR-TD-002 测试点生成纯逻辑测试（S2.3，core 零三方）。

确定性脊柱 + 模型白名单补点的全部规则都在这里钉死：
优先级强制、取消态不产点、幻觉逐行拒入、去重、URN 内容寻址。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from testpilot_core.models import KnowledgeChunk, TestPoint
from testpilot_core.requirement import apply_linking, parse_requirement_markdown
from testpilot_core.testpoint import (
    MODEL_POINT_KINDS,
    ImpactEvidence,
    TestPointValidationError,
    apply_model_points,
    build_impact_query,
    build_source_whitelist,
    build_testpoint_messages,
    deterministic_result,
    deterministic_test_points,
    partition_impact_chunks,
    point_urn,
    testpoint_json_schema,
)

FIXTURE = Path(__file__).parent / "fixtures" / "requirements" / "synthetic-requirements.md"
RECORDED = Path(__file__).parent / "fixtures" / "model" / "responses.json"
EXTERNAL_ID = "requirements/synthetic-requirements.md"


@pytest.fixture
def parsed():
    return parse_requirement_markdown(
        FIXTURE.read_text(encoding="utf-8"), doc_external_id=EXTERNAL_ID
    )


@pytest.fixture
def linked(parsed):
    payload = json.loads(RECORDED.read_text(encoding="utf-8"))[
        "req_link:requirements/synthetic-requirements.md"
    ]["structured"]
    return apply_linking(parsed, payload)


# ── 确定性脊柱 ──────────────────────────────────────────────────────────────

def test_spine_counts_and_kinds_on_plain_parse(parsed):
    points = deterministic_test_points(parsed)
    kinds = [p.kind for p in points]
    # 6 活跃 FR（FR-4 取消不产点）+ 2 NFR 规则 + 3 确定性链接 AC 点
    assert kinds.count("functional") == 6
    assert kinds.count("rule") == 2
    assert kinds.count("acceptance") == 3
    assert all(p.provenance == "deterministic" for p in points)


def test_spine_expands_ac_points_after_model_linking(linked):
    points = deterministic_test_points(linked)
    # 录制链接把 AC0 挂到 FR-1/FR-2/FR-2a、AC1 挂到 FR-5/FR-6 → 5 验收点
    acceptance = [p for p in points if p.kind == "acceptance"]
    assert {p.fr_id for p in acceptance} == {"FR-1", "FR-2", "FR-2a", "FR-5", "FR-6"}
    assert len(acceptance) == 5


def test_cancelled_feature_produces_no_point(parsed):
    points = deterministic_test_points(parsed)
    assert all(p.fr_id != "FR-4" for p in points)


def test_priority_forced_from_feature_and_rule_defaults_to_p1(parsed):
    points = deterministic_test_points(parsed)
    by_fr = {p.fr_id: p.priority for p in points if p.kind == "functional"}
    assert by_fr["FR-1"] == "P0" and by_fr["FR-2a"] == "P1"
    assert by_fr["FR-3"] == "P2"  # 需求缺优先级 → 兜底 P2，不编造
    assert all(p.priority == "P1" for p in points if p.kind == "rule")


def test_spine_source_refs_are_requirement_urns_only(parsed):
    points = deterministic_test_points(parsed)
    for p in points:
        assert p.source_refs
        assert all(ref.startswith("urn:testpilot:fs-reqs:") for ref in p.source_refs)


def test_point_urn_is_content_addressable_and_stable(parsed):
    points = deterministic_test_points(parsed)
    urns = [point_urn(parsed.external_id, p) for p in points]
    assert len(urns) == len(set(urns))  # 同文档内 URN 唯一
    again = [point_urn(parsed.external_id, p) for p in deterministic_test_points(parsed)]
    assert urns == again  # 重跑幂等


def test_legacy_testpoint_construction_keeps_s0_defaults():
    # S0 冻结契约：只传 text/priority/source_refs 的构造方式必须仍然合法
    point = TestPoint(text="老构造", priority="P0", source_refs=["urn:x"])
    assert point.fr_id == "" and point.kind == "functional"
    assert point.provenance == "deterministic"


# ── 白名单与影响面 ─────────────────────────────────────────────────────────

def test_source_whitelist_covers_requirements_and_evidence(parsed):
    evidence = [ImpactEvidence(urn="urn:testpilot:fs-cases:test_case:x#TC-1",
                               kind="test_case", title="t", text="c")]
    whitelist = build_source_whitelist(parsed, evidence)
    assert "urn:testpilot:fs-cases:test_case:x#TC-1" in whitelist
    assert parsed.features[0].urn in whitelist
    assert parsed.rules[0].urn in whitelist


def test_partition_impact_chunks_splits_symbols_from_incidents():
    chunks = [
        KnowledgeChunk(urn="urn:c1", doc_urn="d", index=0, kind="code_symbol",
                       text="func", content_hash="h", symbol_urn="s1"),
        KnowledgeChunk(urn="urn:c2", doc_urn="d", index=1, kind="text",
                       text="事故复盘", content_hash="h"),
    ]
    incidents, symbols = partition_impact_chunks(chunks)
    assert [e.urn for e in symbols] == ["urn:c1"]
    assert [e.kind for e in incidents] == ["incident"]


def test_impact_query_is_bounded(parsed):
    query = build_impact_query(parsed, max_features=2, max_chars=20)
    assert len(query) <= 20


# ── 模型补点校验 ───────────────────────────────────────────────────────────

def test_apply_model_points_accepts_whitelisted_enrichment(parsed):
    spine = deterministic_test_points(parsed)
    feature_urn = parsed.features[0].urn
    payload = {"points": [
        {"fr_id": "FR-1", "text": "磁盘满时创建备忘报错且不留半截记录",
         "kind": "boundary", "source_refs": [feature_urn]},
    ]}
    out = apply_model_points(parsed, spine, payload, [])
    assert len(out.model_points) == 1
    point = out.model_points[0]
    assert point.kind == "boundary" and point.provenance == "model"
    assert point.priority == "P0"  # 优先级强制取 FR，模型无法影响
    assert out.generation_source == "model" and out.rejected == []


def test_unknown_fr_id_is_rejected_as_hallucination(parsed):
    spine = deterministic_test_points(parsed)
    payload = {"points": [
        {"fr_id": "FR-999", "text": "幻觉功能点", "kind": "negative",
         "source_refs": [parsed.features[0].urn]},
    ]}
    out = apply_model_points(parsed, spine, payload, [])
    assert out.model_points == [] and len(out.rejected) == 1
    assert out.rejected[0].reason == "unknown_fr"
    assert out.hallucination_rate == 1.0


def test_unknown_source_urn_is_rejected(parsed):
    spine = deterministic_test_points(parsed)
    payload = {"points": [
        {"fr_id": "FR-1", "text": "伪造来源", "kind": "negative",
         "source_refs": ["urn:testpilot:made-up:requirement:x#FR-1"]},
    ]}
    out = apply_model_points(parsed, spine, payload, [])
    assert len(out.rejected) == 1 and out.rejected[0].reason == "unknown_source"


def test_evidence_urn_in_whitelist_is_accepted(parsed):
    evidence = [ImpactEvidence(urn="urn:testpilot:fs-cases:test_case:x#TC-N1",
                               kind="test_case", title="TC-N1", text="响铃")]
    spine = deterministic_test_points(parsed)
    payload = {"points": [
        {"fr_id": "FR-5", "text": "参照历史用例补锁屏响铃边界",
         "kind": "boundary", "source_refs": [evidence[0].urn]},
    ]}
    out = apply_model_points(parsed, spine, payload, evidence)
    assert len(out.model_points) == 1 and out.rejected == []
    assert evidence[0].urn in out.model_points[0].source_refs


def test_spine_reserved_kinds_rejected(parsed):
    spine = deterministic_test_points(parsed)
    payload = {"points": [
        {"fr_id": "FR-1", "text": "模型伪装脊柱类别", "kind": "functional",
         "source_refs": [parsed.features[0].urn]},
    ]}
    out = apply_model_points(parsed, spine, payload, [])
    assert out.rejected[0].reason == "bad_kind"


def test_empty_text_and_bad_row_shape_rejected(parsed):
    spine = deterministic_test_points(parsed)
    payload = {"points": [
        {"fr_id": "FR-1", "text": "   ", "kind": "negative",
         "source_refs": [parsed.features[0].urn]},
        {"fr_id": "FR-1", "kind": "negative", "source_refs": []},
    ]}
    out = apply_model_points(parsed, spine, payload, [])
    reasons = {r.reason for r in out.rejected}
    assert reasons == {"empty_text", "bad_shape"}


def test_duplicate_model_point_deduped_not_hallucination(parsed):
    spine = deterministic_test_points(parsed)
    spine_text = next(p for p in spine if p.fr_id == "FR-1").text
    payload = {"points": [
        {"fr_id": "FR-1", "text": spine_text, "kind": "boundary",
         "source_refs": [parsed.features[0].urn]},
    ]}
    out = apply_model_points(parsed, spine, payload, [])
    assert out.model_points == [] and out.rejected == [] and out.deduped == 1
    assert out.hallucination_rate == 0.0  # 重复行不进幻觉率分母


def test_malformed_payload_raises_for_caller_degradation(parsed):
    spine = deterministic_test_points(parsed)
    with pytest.raises(TestPointValidationError):
        apply_model_points(parsed, spine, {"nope": []}, [])
    with pytest.raises(TestPointValidationError):
        apply_model_points(parsed, spine, {"points": "x"}, [])


def test_recorded_fixture_end_to_end_numbers(linked):
    """录制夹具（含 2 投毒行 + 1 重复行）的全量数字钉死。"""
    recorded = json.loads(RECORDED.read_text(encoding="utf-8"))
    payload = recorded["tp_gen:requirements/synthetic-requirements.md"]["structured"]
    spine = deterministic_test_points(linked)
    out = apply_model_points(linked, spine, payload, [])
    assert len(out.points) == 16 and out.spine_count == 13
    assert len(out.model_points) == 3
    assert {r.reason for r in out.rejected} == {"unknown_fr", "unknown_source"}
    assert out.deduped == 1
    assert out.hallucination_rate == 0.4  # 2 / (3 有效 + 2 拒入)


def test_deterministic_result_is_spine_only(parsed):
    result = deterministic_result(parsed)
    assert result.generation_source == "deterministic"
    assert result.spine_count == len(result.points)
    TestPoint.model_validate(result.points[0].model_dump())  # 结构合法率


def test_prompt_and_schema_are_self_contained(parsed):
    messages = build_testpoint_messages(parsed, [], deterministic_test_points(parsed))
    assert messages[0]["role"] == "system"
    content = messages[1]["content"]
    assert "FR-1" in content  # 白名单 ID 逐字进 prompt
    # 回归守卫（真模型探针实证）：完整 URN 必须可逐字抄写，否则模型只会回显裸 ID
    assert parsed.features[0].urn in content
    assert parsed.rules[0].urn in content
    schema = testpoint_json_schema()
    item = schema["properties"]["points"]["items"]
    assert set(item["required"]) == {"fr_id", "text", "kind", "source_refs"}
    assert item["properties"]["kind"]["enum"] == list(MODEL_POINT_KINDS)
    assert "priority" not in item["properties"]  # 模型不给优先级
