"""S1.6 golden set 结构与配比校验（FR-EV-001，hostless 纯文件测试）。

泄漏检查（holdout 文档不在索引）需要 store，放在评测 runner 的测试里做；
本文件只锁题集本身的不变量，保证任何机器上无外部依赖可跑。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from testpilot_core.retrieval import parse_symbol_reference

GOLDEN = Path(__file__).parent / "fixtures" / "eval" / "golden_s1.json"
_INTENTS = {"code", "incident", "howto", "concept", "out_of_scope"}
_STATUSES = {"answered", "unknown"}


@pytest.fixture(scope="module")
def golden() -> dict:
    with GOLDEN.open(encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture(scope="module")
def cases(golden) -> list[dict]:
    return golden["cases"]


def test_golden_set_has_30_frozen_cases(golden, cases):
    assert golden["set_id"] == "golden-s1-v1"
    assert len(cases) == 30  # FR-EV-001 AC1：S1 冻结 30 题


def test_case_ids_and_questions_unique(cases):
    ids = [c["case_id"] for c in cases]
    assert len(set(ids)) == len(ids)
    questions = [c["question"] for c in cases]
    assert len(set(questions)) == len(questions)
    assert all(q.strip() for q in questions)


def test_negative_ratio_meets_15_percent_floor(cases):
    negatives = [c for c in cases if c["kind"] == "negative"]
    # FR-EV-001 AC1：≥15% 无答案负例；30 题即至少 5 题，当前冻结 6 题（20%）
    assert len(negatives) >= 5
    assert len(negatives) / len(cases) >= 0.15


def test_ground_truth_enums_valid(cases):
    for case in cases:
        gt = case["ground_truth"]
        assert gt["expected_intent"] in _INTENTS, case["case_id"]
        assert gt["expected_status"] in _STATUSES, case["case_id"]


def test_qa_cases_carry_required_evidence_negatives_carry_none(cases):
    for case in cases:
        if case["kind"] == "qa":
            assert case["required_evidence"], f"{case['case_id']} 正例题必须标注取证 URN 子串"
        else:
            assert case["required_evidence"] == [], f"{case['case_id']} 负例不预置期望证据"


def test_symbol_form_flags_agree_with_core_parser(cases):
    """symbol_form=true 的题面必须真的被 parse_symbol_reference 命中（旁路口径一致）。"""
    for case in cases:
        parsed = parse_symbol_reference(case["question"])
        if case["ground_truth"].get("symbol_form"):
            assert parsed is not None, f"{case['case_id']} 标注符号形态但解析器未命中"
        elif case["ground_truth"].get("expected_intent") != "out_of_scope":
            # 自然语言题面不得被符号解析器误路由（旁路高精度门禁的反向保证）
            assert parsed is None, f"{case['case_id']} 自然语言题被符号解析器误命中"


def test_holdout_cases_flagged_consistently(cases):
    holdouts = [c for c in cases if c.get("holdout")]
    assert len(holdouts) >= 2  # 留出隔离：至少两篇不入库文档各一题
    for case in holdouts:
        assert case["kind"] == "negative"
        assert case["ground_truth"]["expected_status"] == "unknown"
        assert case["ground_truth"].get("holdout_doc"), f"{case['case_id']} 缺 holdout_doc 标注"


def test_negative_expectations_match_guardrail_design(cases):
    negatives = [c for c in cases if c["kind"] == "negative"]
    # 闲聊负例是唯一锁死 unknown_reason 的（ACL 确定性收口）；检索类负例只锁
    # status=unknown，原因随通道行为而定，避免把题集写成对实现的过拟合断言
    chitchat = [c for c in negatives if c["ground_truth"]["expected_intent"] == "out_of_scope"]
    assert len(chitchat) == 1
    assert chitchat[0]["ground_truth"]["expected_unknown_reason"] == "out_of_scope"
    for case in negatives:
        if case["case_id"] != chitchat[0]["case_id"]:
            assert case["ground_truth"]["expected_unknown_reason"] is None


def test_expert_label_slot_reserved_for_blind_review(cases):
    # FR-EV-001 盲标工作流数据结构：每题保留专家标签位，冻结初期待人工填充
    for case in cases:
        assert "expert_label" in case["ground_truth"]
