"""S1.6 评测裁判纯函数测试（hostless，零 IO）。

每个错误标签都有独立坏 case 守着（反假绿：回退裁判逻辑对应测试必须变红）。
"""

from __future__ import annotations

import pytest

from testpilot_core.evaluation import (
    GENERATION_SIDE_LABELS,
    RECALL_SIDE_LABELS,
    CaseResult,
    aggregate_metrics,
    detect_holdout_leakage,
    judge_case,
)
from testpilot_core.models import EvalCase

CHUNK_URN = "urn:testpilot:fs-docs:chunk:Sources/App.swift#AlertListViewModel.foo()"
DOC_URN = "urn:testpilot:fs-docs:document:Sources/App.swift"


def _case(**over) -> EvalCase:
    base = {
        "case_id": "G01",
        "kind": "qa",
        "question": "AlertListViewModel.foo() 的实现逻辑是什么？",
        "ground_truth": {
            "expected_intent": "code",
            "expected_status": "answered",
            "expected_unknown_reason": None,
        },
        "required_evidence": ["App.swift#AlertListViewModel.foo()"],
    }
    base.update(over)
    return EvalCase(**base)


def _result(**over) -> CaseResult:
    base = {
        "status": "answered",
        "intent": "code",
        "evidence_urns": [CHUNK_URN],
        "citation_urns": [CHUNK_URN],
        "whitelist_urns": [CHUNK_URN, DOC_URN],
        "latency_ms": 1000,
        "tokens_in": 100,
        "tokens_out": 50,
        "attempts": 1,
    }
    base.update(over)
    return CaseResult(**base)


def test_happy_path_passes_without_labels():
    verdict = judge_case(_case(), _result())
    assert verdict.passed is True
    assert verdict.labels == []


def test_intent_mismatch_is_recall_side():
    verdict = judge_case(_case(), _result(intent="concept"))
    assert "intent_mismatch" in verdict.labels
    assert verdict.recall_labels == ["intent_mismatch"]
    assert verdict.passed is False


def test_empty_retrieval_labeled_when_no_evidence():
    result = _result(evidence_urns=[], citation_urns=[])
    verdict = judge_case(_case(), result)
    assert "empty_retrieval" in verdict.recall_labels


def test_recall_miss_uses_substring_match_on_required_evidence():
    result = _result(evidence_urns=["urn:testpilot:fs-docs:chunk:Sources/Other.swift#bar()"])
    verdict = judge_case(_case(), result)
    assert "recall_miss" in verdict.labels
    assert verdict.detail["missing_evidence"] == [
        "App.swift#AlertListViewModel.foo()"
    ]


def test_unexpected_unknown_carries_unknown_reason():
    result = _result(
        status="unknown", unknown_reason="citation_failed_exhausted",
        evidence_urns=[], citation_urns=[],
    )
    verdict = judge_case(_case(), result)
    assert "unexpected_unknown" in verdict.generation_labels
    assert verdict.detail["unknown_reason"] == "citation_failed_exhausted"


def test_answered_without_citations_is_no_citations():
    verdict = judge_case(_case(), _result(citation_urns=[]))
    assert "no_citations" in verdict.generation_labels


def test_citation_outside_whitelist_is_forgery_guardrail():
    forged = "urn:testpilot:fs-docs:chunk:Sources/Ghost.swift#never.indexed()"
    verdict = judge_case(_case(), _result(citation_urns=[forged]))
    assert "citation_forgery" in verdict.generation_labels
    assert verdict.detail["forged_urns"] == [forged]


def test_doc_level_citation_in_whitelist_is_valid():
    verdict = judge_case(_case(), _result(citation_urns=[DOC_URN]))
    assert "citation_forgery" not in verdict.labels
    assert verdict.passed is True


def test_collects_all_failures_not_just_first():
    result = _result(
        intent="concept",
        evidence_urns=["urn:testpilot:fs-docs:chunk:Other.swift#bar()"],
        citation_urns=[],
    )
    verdict = judge_case(_case(), result)
    # 意图错 + 召回缺失 + 无引用三个标签同时落，报告才能分诊
    assert set(verdict.labels) == {
        "intent_mismatch",
        "recall_miss",
        "no_citations",
    }


def _negative_case(strict_reason=None) -> EvalCase:
    gt = {
        "expected_intent": "concept",
        "expected_status": "unknown",
        "expected_unknown_reason": strict_reason,
    }
    return EvalCase(
        case_id="N01",
        kind="negative",
        question="技术方案文档里的架构怎么划分？",
        ground_truth=gt,
        required_evidence=[],
        holdout=True,
    )


def test_negative_correctly_refused_passes():
    result = _result(status="unknown", intent="concept",
                     evidence_urns=[], citation_urns=[], unknown_reason="empty_retrieval")
    verdict = judge_case(_negative_case(), result)
    assert verdict.passed is True


def test_negative_answered_is_wrong_answer_generation_side():
    verdict = judge_case(_negative_case(), _result(intent="concept"))
    assert "wrong_answer_on_negative" in verdict.generation_labels
    assert verdict.passed is False


def test_strict_unknown_reason_enforced_only_when_labeled():
    result = _result(status="unknown", intent="concept",
                     evidence_urns=[], citation_urns=[], unknown_reason="weak_retrieval")
    # 无严格原因标注：任何 unknown 原因都算通过
    assert judge_case(_negative_case(), result).passed is True
    # 锁死原因（如闲聊 out_of_scope）：原因不符即失败
    strict = _negative_case(strict_reason="out_of_scope")
    verdict = judge_case(strict, result)
    assert "strict_reason_mismatch" in verdict.generation_labels
    assert verdict.detail["reason_expected"] == "out_of_scope"


def test_label_side_partition_is_exhaustive():
    # 所有标签必须归在召回侧或生成侧之一，新增标签漏注册会被这个测试抓到
    from testpilot_core.evaluation import (
        GENERATION_SIDE_LABELS as GEN,
        RECALL_SIDE_LABELS as REC,
    )
    known = {
        "intent_mismatch", "empty_retrieval", "recall_miss",
        "unexpected_unknown", "no_citations", "citation_forgery",
        "wrong_answer_on_negative", "strict_reason_mismatch",
    }
    assert known == REC | GEN


def test_aggregate_metrics_rates_and_guardrail():
    good_qa = (_case(case_id="G1"), judge_case(_case(case_id="G1"), _result()), _result())
    bad_qa_case = _case(case_id="G2")
    # answered 但召回证据不含 required_evidence：召回侧失败（recall_miss）
    bad_qa_result = _result(
        evidence_urns=["urn:testpilot:fs-docs:chunk:Other.swift#bar()"],
        citation_urns=["urn:testpilot:fs-docs:chunk:Other.swift#bar()"],
        whitelist_urns=["urn:testpilot:fs-docs:chunk:Other.swift#bar()"],
    )
    bad_qa = (bad_qa_case, judge_case(bad_qa_case, bad_qa_result), bad_qa_result)
    neg_case = _negative_case()
    neg_result = _result(status="unknown", intent="concept",
                         evidence_urns=[], citation_urns=[])
    neg = (neg_case, judge_case(neg_case, neg_result), neg_result)

    metrics = aggregate_metrics([good_qa, bad_qa, neg])
    assert metrics["cases_total"] == 3
    assert metrics["golden_passed"] == 2
    assert metrics["golden_pass_rate"] == pytest.approx(2 / 3)
    assert metrics["recall_at_k"] == pytest.approx(0.5)
    assert metrics["refusal_accuracy"] == 1.0
    assert metrics["citation_forgery_count"] == 0
    assert metrics["label_recall_miss"] == 1
    assert metrics["latency_median_ms"] == 1000


def test_aggregate_flags_single_forgery():
    forged = "urn:testpilot:fs-docs:chunk:Ghost.swift#x()"
    result = _result(citation_urns=[forged])
    metrics = aggregate_metrics([(_case(), judge_case(_case(), result), result)])
    assert metrics["citation_forgery_count"] == 1
    assert metrics["citation_accuracy"] == 0.0


def test_holdout_leakage_detection():
    markers = ["docs/技术方案文档.md", "docs/功能设计文档.md"]
    indexed = [
        "urn:testpilot:fs-docs:document:app-calendar-alarm/docs/V1需求文档-强提醒闹钟.md",
    ]
    assert detect_holdout_leakage(markers, indexed) == []
    polluted = indexed + [
        "urn:testpilot:fs-docs:document:app-calendar-alarm/docs/技术方案文档.md"
    ]
    assert detect_holdout_leakage(markers, polluted) == ["docs/技术方案文档.md"]


def test_side_label_sets_disjoint():
    assert RECALL_SIDE_LABELS.isdisjoint(GENERATION_SIDE_LABELS)
