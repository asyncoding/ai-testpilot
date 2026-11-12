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


# ── L3-1 红证：严格关键词失败后的 Jaccard 回退（W1 E4/E8/E9 冻结原句） ───────

_W1_E4 = _expert(
    expert_id="E4", fr_id="FR-2", priority="P0",
    text="任务完成瞬间停铃并取消全部补发闹钟",
    match_keywords=["停铃", "取消", "补发"],
)
_W1_E4_AI = (
    "验证「任务完成前，接管页不得出现「完成 / 关闭」入口；任务未完成时响铃 + 震动 + "
    "音量锁继续（继承 V1 FR-2 / FR-9a）；任务完成瞬间停铃，并执行 V1 FR-5 的全部取消"
    "（系统闹钟、backup、ladder、companion 全部立即取消）」主流程可用"
)
_W1_E9 = _expert(
    expert_id="E9", fr_id="FR-6", priority="P0",
    text="数学答错重答本题不扣已对题，连错 3 次换同难度新题",
    match_keywords=["答错", "重答", "换题"],
)
_W1_E9_AI = (
    "验证「答题只允许数字键盘输入；答错时标错并可立即重答本题，不扣已答对的题、"
    "不直接放行；同一题连续答错 3 次，换一道同难度新题」主流程可用"
)


def test_l3_1_near_synonym_paraphrase_rescued_after_strict_keyword_fail():
    # 两条 AI 点都缺一个字面关键词（补发/换题），旧口径严格全命中判 miss；
    # L3-1 起严格失败后用专家原句 token Jaccard 兜底（实测 0.688/0.682 ≥ .5）
    for expert, ai_text in ((_W1_E4, _W1_E4_AI), (_W1_E9, _W1_E9_AI)):
        assert not all(kw in ai_text for kw in expert.match_keywords)  # 先证明确实严格失败
        manifest = ExpertBlindList(
            doc_external_id="doc-x", sealed_at="t", points=[expert])
        gen = _gen([TestPoint(text=ai_text, priority="P0",
                              source_refs=["urn:r"], fr_id=expert.fr_id)])
        report = evaluate_blind(gen, manifest)
        assert report.expert_covered == 1 and report.missed == []


def test_l3_1_e8_paraphrase_remains_miss_because_threshold_not_lowered():
    # 诚实挂账：E8 三个关键词全被改写（道数→3 道/5 道），专家句又短，
    # 实测 Jaccard 仅 0.263 < .5，回退也不命中。阈值不许为凑覆盖率下调，
    # 该缺口保留为已知残留，在 W2 post-L3 附录中双列呈现。
    e8 = _expert(
        expert_id="E8", fr_id="FR-5", priority="P0",
        text="数学简单中等困难三档的题型与道数符合定义",
        match_keywords=["数学题", "难度", "道数"],
    )
    e8_ai = (
        "验证「数学题支持加减乘，三档难度：简单为一位数加减、3 道；中等为两位数加减 + "
        "一位数乘法、5 道；困难为两位数乘法 + 混合运算、5 道；每道题在响铃时现场随机"
        "生成，同一响铃会话内不重复出题」主流程可用"
    )
    assert not all(kw in e8_ai for kw in e8.match_keywords)
    manifest = ExpertBlindList(doc_external_id="doc-x", sealed_at="t", points=[e8])
    gen = _gen([TestPoint(text=e8_ai, priority="P0",
                          source_refs=["urn:r"], fr_id="FR-5")])
    report = evaluate_blind(gen, manifest)
    assert report.expert_covered == 0
    assert [m.expert_id for m in report.missed] == ["E8"]


# ── L3-2 红证：分类型精确率视图，冻结 precision 口径不变 ────────────────────

def test_l3_2_precision_by_kind_splits_recognition_without_frozen_field_change():
    manifest = ExpertBlindList(doc_external_id="doc-x", sealed_at="t", points=[
        _expert(expert_id="E1", fr_id="FR-1", match_keywords=["同步"]),
        _expert(expert_id="E2", fr_id="FR-2", priority="P0",
                match_keywords=["落盘"]),
    ])
    gen = _gen([
        TestPoint(text="自动同步队列", priority="P1", source_refs=["urn:r"],
                  fr_id="FR-1", kind="functional"),            # 命中 E1
        TestPoint(text="无人认可的 functional 发挥", priority="P2",
                  source_refs=["urn:r"], fr_id="FR-1", kind="functional"),
        TestPoint(text="创建后立即落盘", priority="P0", source_refs=["urn:r"],
                  fr_id="FR-2", kind="acceptance"),            # 命中 E2
        TestPoint(text="没人认的 AC 发挥", priority="P2",
                  source_refs=["urn:r"], fr_id="FR-2", kind="acceptance"),
    ])
    report = evaluate_blind(gen, manifest)
    assert report.ai_total_by_kind == {"functional": 2, "acceptance": 2}
    assert report.precision_by_kind == {"functional": 0.5, "acceptance": 0.5}
    # 冻结字段：整体 precision 仍是 recognized/ai_total = 2/4，语义不变
    assert report.precision == 0.5 and report.ai_recognized == 2


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
