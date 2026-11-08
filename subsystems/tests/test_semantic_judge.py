"""E5 语义裁判 hostless 纯逻辑测试（docs/06 §6/§7，FR-EV-003）。

守的是：裁判信封 fail-closed 解析（畸形输出绝不放行）、忠实度陈述级口径、
相关性逐题判定、聚合（无分母不造数）、人工抽检的 Cohen's kappa。
模型调用（deepseek-r1:7b，与生成模型不同体）在 workers 适配层，不进本文件。
"""

from __future__ import annotations

import pytest

from testpilot_core.semantic_judge import (
    Adjudication,
    SemanticVerdict,
    aggregate_semantic,
    cohen_kappa,
    parse_semantic_judgement,
)


def _payload(*, supported: int = 2, unsupported: int = 0, relevance: bool = True):
    return {
        "claims": [
            {"claim": f"陈述{i}", "supported": i < supported,
             "evidence_urn": "urn:testpilot:fs-docs:chunk:docs/x.md#s1",
             "reason": "证据原文可支撑" if i < supported else "证据中找不到该断言"}
            for i in range(supported + unsupported)
        ],
        "relevance": {"passed": relevance, "reason": "答案直接回应问题"},
    }


def test_parse_valid_envelope_returns_claims_and_verdict():
    v = parse_semantic_judgement("G01", _payload(supported=2, unsupported=1))
    assert isinstance(v, SemanticVerdict)
    assert v.case_id == "G01" and v.parse_error == ""
    assert len(v.claims) == 3 and len(v.unsupported_claims) == 1
    # 陈述级忠实度：3 条陈述 2 条被支撑 → 2/3，未达逐题全支撑即不通过
    assert v.faithfulness_ratio == pytest.approx(2 / 3)
    assert v.faithfulness_passed is False
    assert v.relevance_passed is True


def test_all_supported_claims_pass_faithfulness():
    v = parse_semantic_judgement("G02", _payload(supported=3))
    assert v.faithfulness_ratio == 1.0 and v.faithfulness_passed is True


def test_malformed_envelope_is_fail_closed_not_raised():
    # 批量评测纪律（G22 教训）：单题裁判输出畸形不能炸掉整批评测，
    # 归为 parse_error 且两个语义维都不放行
    for bad in (
        {},
        {"claims": "not-a-list"},
        {"claims": [], "relevance": {"passed": True}},  # 已回答题零陈述=无法证忠实
        {"claims": [{"claim": "x"}], "relevance": {}},  # 缺 supported/相关性判定
        {"claims": _payload()["claims"], "relevance": {"passed": "yes"}},  # 非布尔
    ):
        v = parse_semantic_judgement("G03", bad)
        assert v.parse_error
        assert v.faithfulness_passed is False and v.relevance_passed is False
        assert v.claims == ()


def test_claim_must_carry_evidence_urn_when_supported():
    # 被支撑的陈述必须指向证据 URN，否则裁判无法核验，按不支撑处理
    payload = _payload(supported=1)
    payload["claims"][0]["evidence_urn"] = ""
    v = parse_semantic_judgement("G04", payload)
    assert v.faithfulness_ratio == 0.0 and v.faithfulness_passed is False


def test_aggregate_semantic_micro_average_and_no_fabrication():
    verdicts = [
        parse_semantic_judgement("G01", _payload(supported=3)),
        parse_semantic_judgement("G02", _payload(supported=1, unsupported=1, relevance=False)),
    ]
    m = aggregate_semantic(verdicts)
    # 陈述级微平均：5 条陈述 4 条支撑（docs/06：能被上下文支撑的陈述占比）
    assert m["faithfulness"] == pytest.approx(0.8)
    assert m["faithfulness_case_pass_rate"] == 0.5
    assert m["answer_relevance"] == 0.5
    assert m["cases_judged"] == 2 and m["parse_error_count"] == 0
    assert m["faithfulness_gate"] == 0.9 and m["answer_relevance_gate"] == 0.85

    empty = aggregate_semantic([])  # 无分母不造数，与 FR-EV-003 AC2 同纪律
    assert empty == {}


def test_aggregate_counts_parse_errors_separately():
    m = aggregate_semantic([
        parse_semantic_judgement("G01", _payload(supported=1)),
        parse_semantic_judgement("G02", {}),
    ])
    assert m["parse_error_count"] == 1
    assert m["cases_judged"] == 2  # 畸形题仍在分母里，不能悄悄剔除


def test_cohen_kappa_perfect_and_chance_agreement():
    # 完全一致 → 1.0；期望一致（边际相同）导致分母为 0 → None 而非除零造数
    assert cohen_kappa([True, True, False, False], [True, True, False, False]) == 1.0
    assert cohen_kappa([True, False], [False, True]) == -1.0
    assert cohen_kappa([True, True], [True, True]) is None
    with pytest.raises(ValueError):
        cohen_kappa([True], [True, False])  # 长度不一致是调用方缺陷


def test_kappa_against_manual_adjudication_subset():
    # docs/06 §6：≥10% 人工抽检，裁判与专家的一致性用 kappa 量化
    judge = [True, True, False, True, False, True, False, True, True, False]
    expert = [True, False, False, True, False, True, True, True, True, False]
    verdicts = [
        SemanticVerdict(
            case_id=f"G{i:02d}", faithfulness_passed=j, relevance_passed=True,
            claims=(), unsupported_claims=(), faithfulness_ratio=1.0 if j else 0.0,
            relevance_reason="", parse_error="",
        )
        for i, j in enumerate(judge)
    ]
    adj = [
        Adjudication(case_id=f"G{i:02d}", faithfulness_passed=e, relevance_passed=True)
        for i, e in enumerate(expert)
    ]
    k = cohen_kappa(
        [v.faithfulness_passed for v in verdicts],
        [a.faithfulness_passed for a in adj],
    )
    # 对角一致 8/10，手工可复算：Po=.8, Pe=.6²+.4²=.52 → κ=.28/.48≈.5833
    assert k == pytest.approx(0.5833, abs=1e-3)
