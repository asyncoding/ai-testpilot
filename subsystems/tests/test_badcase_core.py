"""S2.6c FR-EV-005 bad case 闭环纯逻辑测试（core 零 IO，hostless）。

覆盖：内容寻址幂等、建账校验、状态机合法/非法迁移、分诊两步事件、
转 golden 溯源、回归关联、反馈投影、闭环周期统计与不可变入参纪律。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from testpilot_core.badcase import (
    GOLDEN_LOCKED_VERSION,
    BadCase,
    BadCaseError,
    BadCaseSnapshot,
    apply_transition,
    associate_regression,
    bad_case_id,
    cycle_time_stats,
    golden_case_id_for,
    ingest_feedback_as_bad_case,
    new_bad_case,
    promote_to_eval_case,
    triage,
)
from testpilot_core.enums import FeedbackKind

_T0 = datetime(2026, 11, 10, 9, 0, tzinfo=timezone.utc)
_T1 = _T0 + timedelta(hours=1)
_T2 = _T0 + timedelta(hours=2)
_T3 = _T0 + timedelta(hours=5)


def _case(**over) -> BadCase:
    kwargs = dict(
        source="manual_review", source_ref="INC-42#comment-1",
        title="闹钟响铃未覆盖前台场景", at=_T0)
    kwargs.update(over)
    return new_bad_case(**kwargs)


# ── 建账与内容寻址 ──────────────────────────────────────────────────────────

def test_bad_case_id_deterministic_and_prefixed():
    a = bad_case_id("qa_feedback", "run-1#feedback-7")
    b = bad_case_id("qa_feedback", "run-1#feedback-7")
    assert a == b and a.startswith("bc-") and len(a) == len("bc-") + 12
    assert a != bad_case_id("qa_feedback", "run-1#feedback-8")


def test_new_bad_case_genesis_state_and_event():
    case = _case()
    assert case.status == "open" and not case.is_closed
    assert case.bad_case_id == bad_case_id("manual_review", "INC-42#comment-1")
    assert case.created_at == case.updated_at == _T0
    assert case.closed_at is None
    assert len(case.history) == 1
    assert case.history[0].to_status == "open" and case.history[0].actor == "system"


@pytest.mark.parametrize("field,value", [
    ("source_ref", ""), ("source_ref", "   "), ("title", ""), ("title", "  "),
])
def test_new_bad_case_rejects_blank_required(field, value):
    with pytest.raises(BadCaseError):
        _case(**{field: value})


def test_qa_feedback_requires_feedback_id():
    with pytest.raises(BadCaseError):
        new_bad_case(source="qa_feedback", source_ref="r#f", title="t", at=_T0)
    ok = new_bad_case(source="qa_feedback", source_ref="r#f", title="t",
                      feedback_id="9", at=_T0)
    assert ok.feedback_id == "9"


# ── 状态机迁移闭集 ──────────────────────────────────────────────────────────

def test_full_lifecycle_open_to_fixed():
    case = _case()
    case = apply_transition(case, "triaged", actor="qa-li", at=_T1)
    case = apply_transition(case, "accepted", actor="qa-li", at=_T1)
    assert case.status == "accepted"
    case = apply_transition(case, "promoted", actor="qa-li", at=_T2)
    assert case.status == "promoted" and case.golden_case_id == ""
    case = associate_regression(
        case, eval_run_id="eval-run-1", outcome="pass", actor="ci-bot", at=_T3)
    assert case.status == "fixed" and case.is_closed
    assert case.closed_at == _T3
    assert case.regression_eval_run_id == "eval-run-1"
    assert case.cycle_hours == 5.0
    # 审计链完整：创世 + 四次迁移
    assert [e.to_status for e in case.history] == [
        "open", "triaged", "accepted", "promoted", "fixed"]


def test_illegal_transitions_fail_closed():
    case = _case()
    with pytest.raises(BadCaseError, match="非法状态迁移"):
        apply_transition(case, "accepted", actor="qa-li")
    with pytest.raises(BadCaseError):
        apply_transition(case, "fixed", actor="qa-li")
    # 终态不可迁移
    rejected = triage(case, decision="reject", actor="qa-li", at=_T1)
    assert rejected.status == "rejected" and rejected.is_closed
    with pytest.raises(BadCaseError):
        apply_transition(rejected, "triaged", actor="qa-li")


def test_transition_requires_actor_and_does_not_mutate_input():
    case = _case()
    with pytest.raises(BadCaseError, match="actor"):
        apply_transition(case, "triaged", actor="   ")
    before = case.model_copy(deep=True)
    apply_transition(case, "triaged", actor="qa-li", at=_T1)
    assert case == before  # 入参不可变，函数返回新副本


def test_triage_from_open_emits_two_events_and_records_auditor():
    case = _case()
    accepted = triage(case, decision="accept", actor="qa-wang",
                      reason="factual_error", note="复现成立", at=_T2)
    assert accepted.status == "accepted"
    assert accepted.triaged_by == "qa-wang" and accepted.triaged_at == _T2
    assert accepted.reason == "factual_error"
    assert accepted.decision_note == "复现成立"
    assert [e.to_status for e in accepted.history][-2:] == ["triaged", "accepted"]


def test_triage_illegal_state_rejected():
    promoted = apply_transition(
        apply_transition(apply_transition(
            _case(), "triaged", actor="a"), "accepted", actor="a"),
        "promoted", actor="a")
    with pytest.raises(BadCaseError, match="分诊"):
        triage(promoted, decision="accept", actor="a")


# ── 转 golden 评测题 ────────────────────────────────────────────────────────

def test_promote_requires_accepted():
    with pytest.raises(BadCaseError, match="accepted"):
        promote_to_eval_case(_case())


def test_promote_to_eval_case_carries_full_provenance():
    case = new_bad_case(
        source="qa_feedback", source_ref="run-9#feedback-3",
        title="负反馈 run-9：stale_reference", feedback_id="3",
        reason="stale_reference", detail="引用了已删除文档",
        question="如何停止闹钟？",
        snapshot=BadCaseSnapshot(data_version="d1", model_version="m1",
                                 prompt_version="p1", params_version="x1"),
        at=_T0)
    accepted = triage(case, decision="accept", actor="qa-li", at=_T1)
    eval_case = promote_to_eval_case(accepted)
    assert eval_case.kind == "bad_case"
    assert eval_case.case_id == golden_case_id_for(case.bad_case_id)
    assert eval_case.case_id.startswith("bad-case:bc-")
    assert eval_case.question == "如何停止闹钟？"
    prov = eval_case.ground_truth["provenance"]
    assert prov == {
        "bad_case_id": case.bad_case_id, "source": "qa_feedback",
        "source_ref": "run-9#feedback-3", "feedback_id": "3"}
    assert eval_case.ground_truth["snapshot"] == {
        "data_version": "d1", "model_version": "m1",
        "prompt_version": "p1", "params_version": "x1"}
    assert GOLDEN_LOCKED_VERSION == "bad-case-promoted"
    # promote 只映射不改状态
    assert accepted.status == "accepted" and accepted.golden_case_id == ""


# ── 回归关联 ────────────────────────────────────────────────────────────────

def test_regression_fail_keeps_loop_open_then_pass_closes():
    accepted = triage(_case(), decision="accept", actor="qa-li", at=_T1)
    promoted = apply_transition(accepted, "promoted", actor="qa-li", at=_T2)
    failed = associate_regression(
        promoted, eval_run_id="eval-run-2", outcome="fail",
        actor="ci-bot", note="场景复现", at=_T3)
    assert failed.status == "regression_failed" and not failed.is_closed
    assert failed.closed_at is None and failed.cycle_hours is None
    fixed = associate_regression(
        failed, eval_run_id="eval-run-3", outcome="pass",
        actor="ci-bot", at=_T3 + timedelta(hours=1))
    assert fixed.status == "fixed" and fixed.is_closed
    assert fixed.regression_eval_run_id == "eval-run-3"


def test_regression_requires_run_id_and_legal_state():
    accepted = triage(_case(), decision="accept", actor="qa-li", at=_T1)
    with pytest.raises(BadCaseError, match="eval_run_id"):
        associate_regression(accepted, eval_run_id=" ", outcome="pass", actor="ci")
    with pytest.raises(BadCaseError, match="非法状态迁移"):
        associate_regression(accepted, eval_run_id="e1", outcome="pass", actor="ci")


# ── 反馈投影 ────────────────────────────────────────────────────────────────

def test_ingest_feedback_useful_rejected():
    with pytest.raises(BadCaseError, match="useful"):
        ingest_feedback_as_bad_case(
            feedback_row_id=1, run_id="run-1", kind=FeedbackKind.USEFUL)


def test_ingest_not_useful_feedback_fields_and_idempotent_id():
    snap = BadCaseSnapshot(data_version="d9", model_version="",
                           prompt_version="p", params_version="x")
    case = ingest_feedback_as_bad_case(
        feedback_row_id=7, run_id="run-7", kind=FeedbackKind.NOT_USEFUL,
        reason="off_topic", detail="答非所问", question="Q7",
        snapshot=snap, at=_T0)
    assert case.source == "qa_feedback"
    assert case.source_ref == "run-7#feedback-7"
    assert case.feedback_id == "7"
    assert "run-7" in case.title and "off_topic" in case.title
    assert case.snapshot.data_version == "d9"
    # 同反馈重放：内容寻址恒等，store UNIQUE 兜底幂等
    again = ingest_feedback_as_bad_case(
        feedback_row_id=7, run_id="run-7", kind=FeedbackKind.NOT_USEFUL,
        reason="off_topic", snapshot=snap, at=_T0)
    assert again.bad_case_id == case.bad_case_id


def test_ingest_feedback_model_version_fills_snapshot():
    case = ingest_feedback_as_bad_case(
        feedback_row_id=2, run_id="run-2", kind=FeedbackKind.NOT_USEFUL,
        model_version="qwen2.5:7b", at=_T0)
    assert case.snapshot.model_version == "qwen2.5:7b"


# ── 闭环周期统计 ────────────────────────────────────────────────────────────

def test_cycle_stats_empty_sample_no_division():
    stats = cycle_time_stats([])
    assert stats.total == 0 and stats.closure_rate == 0.0
    assert stats.median_cycle_hours is None
    assert set(stats.by_status) == {
        "open", "triaged", "accepted", "rejected",
        "promoted", "fixed", "regression_failed"}


def test_cycle_stats_mixed_population():
    open_case = _case()
    rejected = triage(_case(source_ref="r2"), decision="reject",
                      actor="a", at=_T0 + timedelta(hours=2))
    fixed = associate_regression(
        apply_transition(
            triage(_case(source_ref="r3"), decision="accept",
                   actor="a", at=_T0 + timedelta(hours=1)),
            "promoted", actor="a", at=_T0 + timedelta(hours=2)),
        eval_run_id="e1", outcome="pass", actor="ci",
        at=_T0 + timedelta(hours=6))
    stats = cycle_time_stats([open_case, rejected, fixed])
    assert stats.total == 3 and stats.open_count == 1 and stats.closed_count == 2
    assert stats.closure_rate == round(2 / 3, 6)
    assert stats.by_status["fixed"] == 1
    assert stats.by_status["rejected"] == 1
    assert stats.by_status["open"] == 1
    # 闭环周期：rejected 2h、fixed 6h，中位数 4h
    assert stats.median_cycle_hours == 4.0
