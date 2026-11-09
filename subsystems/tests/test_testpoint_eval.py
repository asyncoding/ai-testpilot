"""FR-TD-002 专家盲标评测纯逻辑测试（S2.3，core 零三方）。

钉死四件事：盲标顺序护栏、关键词/Jaccard 双口径匹配、四项指标口径、
P0/P1 漏点与幻觉点单列（AC2）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from testpilot_core.models import TestPoint
from testpilot_core.requirement import apply_linking, parse_requirement_markdown
from testpilot_core.testpoint import (
    TestPointGenerationResult,
    apply_model_points,
    deterministic_test_points,
)
from testpilot_core.testpoint_eval import (
    COVERAGE_GATE,
    HALLUCINATION_GATE,
    PRECISION_GATE,
    BlindOrderError,
    ExpertBlindList,
    ExpertPoint,
    evaluate_blind,
    seal_expert_manifest,
)

FIXTURE = Path(__file__).parent / "fixtures" / "requirements" / "synthetic-requirements.md"
RECORDED = Path(__file__).parent / "fixtures" / "model" / "responses.json"
EXPERT_FIXTURE = Path(__file__).parent / "fixtures" / "testpoint" / "expert-synthetic-requirements.json"
EXTERNAL_ID = "requirements/synthetic-requirements.md"


def _expert(**kw) -> ExpertPoint:
    base = {"expert_id": "E1", "text": "x", "fr_id": "FR-1", "priority": "P1"}
    base.update(kw)
    return ExpertPoint(**base)


def _gen(points, *, external_id="doc-x", rejected=None) -> TestPointGenerationResult:
    return TestPointGenerationResult(
        doc_urn=f"urn:testpilot:fs-reqs:requirement:{external_id}",
        external_id=external_id,
        points=points,
        spine_count=len([p for p in points if p.provenance == "deterministic"]),
        rejected=rejected or [],
        generation_source="model" if rejected is not None or
        any(p.provenance == "model" for p in points) else "deterministic",
    )


# ── 封存与盲标顺序 ──────────────────────────────────────────────────────────

def test_seal_hash_stable_and_sensitive_to_tampering():
    manifest = ExpertBlindList(
        doc_external_id="doc-x", sealed_at="2026-11-09T00:00:00Z",
        points=[_expert()],
    )
    assert seal_expert_manifest(manifest) == seal_expert_manifest(manifest.model_copy())
    tampered = manifest.model_copy(deep=True)
    tampered.points[0].text = "被事后修改"
    assert seal_expert_manifest(tampered) != seal_expert_manifest(manifest)


def test_empty_manifest_and_doc_mismatch_raise_blind_order_error():
    empty = ExpertBlindList(doc_external_id="doc-x", sealed_at="t", points=[])
    gen = _gen([])
    with pytest.raises(BlindOrderError):
        evaluate_blind(gen, empty)

    other = ExpertBlindList(doc_external_id="doc-y", sealed_at="t", points=[_expert()])
    with pytest.raises(BlindOrderError):
        evaluate_blind(gen, other)


# ── 匹配口径 ────────────────────────────────────────────────────────────────

def test_keyword_match_requires_every_keyword():
    manifest = ExpertBlindList(doc_external_id="doc-x", sealed_at="t", points=[
        _expert(text="断网同步", match_keywords=["断网", "同步"]),
    ])
    hit = _gen([TestPoint(text="断网后自动同步队列", priority="P1",
                          source_refs=["urn:r"], fr_id="FR-1")])
    assert evaluate_blind(hit, manifest).expert_covered == 1

    miss = _gen([TestPoint(text="断网后本地缓存", priority="P1",
                           source_refs=["urn:r"], fr_id="FR-1")])
    report = evaluate_blind(miss, manifest)
    assert report.expert_covered == 0 and report.missed[0].expert_id == "E1"


def test_fr_id_mismatch_blocks_match_even_when_keywords_hit():
    manifest = ExpertBlindList(doc_external_id="doc-x", sealed_at="t", points=[
        _expert(fr_id="FR-2", match_keywords=["同步"]),
    ])
    gen = _gen([TestPoint(text="自动同步", priority="P1",
                          source_refs=["urn:r"], fr_id="FR-1")])
    assert evaluate_blind(gen, manifest).expert_covered == 0


def test_jaccard_fallback_without_keywords():
    manifest = ExpertBlindList(doc_external_id="doc-x", sealed_at="t", points=[
        _expert(text="断网编辑自动同步冲突副本", match_keywords=[]),
    ])
    hit = _gen([TestPoint(text="断网编辑自动同步冲突副本不丢数据", priority="P1",
                          source_refs=["urn:r"], fr_id="FR-1")])
    assert evaluate_blind(hit, manifest).expert_covered == 1

    miss = _gen([TestPoint(text="全屏提醒循环响铃直到用户手动确认", priority="P0",
                           source_refs=["urn:r"], fr_id="FR-1")])
    assert evaluate_blind(miss, manifest).expert_covered == 0


def test_non_must_test_point_excluded_from_coverage():
    manifest = ExpertBlindList(doc_external_id="doc-x", sealed_at="t", points=[
        _expert(expert_id="E1", match_keywords=["同步"]),
        _expert(expert_id="E2", text="没人覆盖的点", fr_id="FR-1",
                priority="P2", match_keywords=["不存在的词"], must_test=False),
    ])
    gen = _gen([TestPoint(text="自动同步", priority="P1",
                          source_refs=["urn:r"], fr_id="FR-1")])
    report = evaluate_blind(gen, manifest)
    assert report.expert_total == 2 and report.expert_must_test == 1
    assert report.coverage == 1.0 and report.missed == []


# ── 指标与门禁 ──────────────────────────────────────────────────────────────

def test_clean_generation_passes_all_gates():
    manifest = ExpertBlindList(doc_external_id="doc-x", sealed_at="t", points=[
        _expert(expert_id="E1", match_keywords=["断网", "同步"]),
    ])
    gen = _gen([TestPoint(text="断网后自动同步队列", priority="P1",
                          source_refs=["urn:r"], fr_id="FR-1",
                          kind="negative", provenance="model")])
    report = evaluate_blind(gen, manifest)
    assert report.coverage == 1.0 and report.precision == 1.0
    assert report.hallucination_rate == 0.0 and report.gate.passed is True


def test_p0_miss_listed_separately_and_fails_gate():
    manifest = ExpertBlindList(doc_external_id="doc-x", sealed_at="t", points=[
        _expert(expert_id="E1", fr_id="FR-1", priority="P0",
                match_keywords=["落盘"]),
        _expert(expert_id="E2", fr_id="FR-2", priority="P0",
                match_keywords=["不存在的口径"]),
    ])
    gen = _gen([TestPoint(text="创建后立即落盘", priority="P0",
                          source_refs=["urn:r"], fr_id="FR-1")])
    report = evaluate_blind(gen, manifest)
    assert report.coverage == 0.5 < COVERAGE_GATE
    assert [e.expert_id for e in report.p0p1_misses] == ["E2"]
    assert report.gate.coverage_ok is False
    assert report.gate.p0p1_miss_zero is False
    assert report.gate.passed is False


def test_unrecognized_ai_point_lowers_precision():
    manifest = ExpertBlindList(doc_external_id="doc-x", sealed_at="t", points=[
        _expert(match_keywords=["同步"]),
    ])
    gen = _gen([
        TestPoint(text="自动同步", priority="P1", source_refs=["urn:r"], fr_id="FR-1"),
        TestPoint(text="无人认可的 AI 发挥", priority="P2",
                  source_refs=["urn:r"], fr_id="FR-1"),
    ])
    report = evaluate_blind(gen, manifest)
    assert report.precision == 0.5 < PRECISION_GATE
    assert report.gate.precision_ok is False


def test_hallucination_rate_gate_uses_generation_rejected():
    from testpilot_core.testpoint import RejectedPoint

    manifest = ExpertBlindList(doc_external_id="doc-x", sealed_at="t", points=[
        _expert(match_keywords=["同步"]),
    ])
    rejected = [RejectedPoint(text="幻觉", fr_id="FR-999", kind="negative",
                              source_refs=["urn:fake"], reason="unknown_fr")]
    gen = _gen(
        [TestPoint(text="自动同步", priority="P1", source_refs=["urn:r"], fr_id="FR-1",
                   kind="negative", provenance="model")],
        rejected=rejected,
    )
    report = evaluate_blind(gen, manifest)
    assert report.hallucination_rate == 0.5 > HALLUCINATION_GATE
    assert report.hallucinations[0]["reason"] == "unknown_fr"
    assert report.gate.hallucination_ok is False


# ── 录制夹具全链路口径（含投毒行的 fail-closed 证据） ───────────────────────

def test_recorded_fixture_full_chain_metrics():
    parsed = parse_requirement_markdown(
        FIXTURE.read_text(encoding="utf-8"), doc_external_id=EXTERNAL_ID
    )
    recorded = json.loads(RECORDED.read_text(encoding="utf-8"))
    linked = apply_linking(
        parsed, recorded[f"req_link:{EXTERNAL_ID}"]["structured"]
    )
    spine = deterministic_test_points(linked)
    generation = apply_model_points(
        linked, spine, recorded[f"tp_gen:{EXTERNAL_ID}"]["structured"], []
    )
    manifest = ExpertBlindList.model_validate(
        json.loads(EXPERT_FIXTURE.read_text(encoding="utf-8"))
    )

    report = evaluate_blind(generation, manifest)
    assert report.expert_total == 16 and report.expert_must_test == 16
    assert report.coverage == 0.9375          # 15/16，唯一漏点 E8 是 P2
    assert report.precision == 1.0
    assert [e.expert_id for e in report.missed] == ["E8"]
    assert report.p0p1_misses == []           # AC2：P0/P1 零漏点
    assert report.gate.coverage_ok is True
    assert report.gate.precision_ok is True
    assert report.gate.p0p1_miss_zero is True
    # 录制夹具故意投毒 2 行：幻觉门禁必须 fail-closed，而不是被全绿掩盖
    assert report.hallucination_rate == 0.4
    assert report.gate.hallucination_ok is False
    assert report.gate.passed is False
    assert {h["reason"] for h in report.hallucinations} == {
        "unknown_fr", "unknown_source"
    }
