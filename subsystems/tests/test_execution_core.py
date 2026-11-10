"""S3.1 FR-EX-002 执行域纯逻辑测试（core 零 IO，hostless）。

覆盖：run 状态机合法/非法迁移与终态不可变、用例结果聚合、内容寻址 run_id、
重跑判定、每日配额（槽位/跨日/重放/配额关死/账本矛盾）、租约回收
（未过期不抢/过期重领/达上限死信）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from testpilot_core.enums import CaseStatus, RunStatus
from testpilot_core.execution import (
    BUDGET_DENIED,
    BUDGET_OK,
    BUDGET_REPLAY,
    RECLAIM_DEAD_LETTER,
    RECLAIM_NONE,
    RECLAIM_REQUEUE,
    BudgetEntry,
    ExecutionError,
    JobLease,
    decide_reclaim,
    evaluate_rerun,
    is_rerun,
    run_id_for,
    run_outcome,
    transition_run_status,
)
from testpilot_core.models import CaseResult, CaseSelector, TestExecutionRequest

_T0 = datetime(2026, 11, 10, 9, 0, tzinfo=timezone.utc)


def _req(**over) -> TestExecutionRequest:
    kwargs = dict(
        request_id="req-1",
        idempotency_key="idem-1",
        project="demo",
        selector=CaseSelector(test_ids=["t"]),
        commit_sha="deadbeef",
    )
    kwargs.update(over)
    return TestExecutionRequest(**kwargs)


def _case(status: CaseStatus, external_id: str = "T/t1") -> CaseResult:
    return CaseResult(external_case_id=external_id, status=status)


# ── 状态机 ───────────────────────────────────────────────────────────────────


def test_run_state_machine_happy_path():
    s = transition_run_status(RunStatus.QUEUED, "pick_up")
    assert s == RunStatus.RUNNING
    assert transition_run_status(s, "finish_passed") == RunStatus.PASSED
    assert transition_run_status(RunStatus.QUEUED, "cancel") == RunStatus.CANCELED
    assert transition_run_status(RunStatus.RUNNING, "cancel") == RunStatus.CANCELED
    assert transition_run_status(RunStatus.RUNNING, "finish_failed") == RunStatus.FAILED
    assert transition_run_status(RunStatus.RUNNING, "finish_error") == RunStatus.ERROR
    assert transition_run_status(RunStatus.RUNNING, "timeout") == RunStatus.TIMEOUT


@pytest.mark.parametrize(
    "terminal",
    [RunStatus.PASSED, RunStatus.FAILED, RunStatus.ERROR,
     RunStatus.TIMEOUT, RunStatus.CANCELED],
)
def test_terminal_status_immutable(terminal):
    for event in ("pick_up", "cancel", "finish_passed",
                  "finish_failed", "finish_error", "timeout"):
        with pytest.raises(ExecutionError):
            transition_run_status(terminal, event)  # type: ignore[arg-type]


def test_illegal_transitions_fail_closed():
    # 未领取直接收尾必须拒绝（queued 只允许 pick_up/cancel）
    for event in ("finish_passed", "finish_failed", "finish_error", "timeout"):
        with pytest.raises(ExecutionError):
            transition_run_status(RunStatus.QUEUED, event)  # type: ignore[arg-type]
    with pytest.raises(ExecutionError):
        transition_run_status(RunStatus.RUNNING, "pick_up")


def test_requeue_only_valid_from_running():
    # 崩溃恢复边：running 可回 queued 重领；queued 本身不能再 requeue
    assert transition_run_status(RunStatus.RUNNING, "requeue") == RunStatus.QUEUED
    with pytest.raises(ExecutionError):
        transition_run_status(RunStatus.QUEUED, "requeue")


# ── 结果聚合 / run_id / 重跑判定 ──────────────────────────────────────────────


def test_run_outcome_aggregation():
    assert run_outcome([]) == RunStatus.ERROR
    assert run_outcome([_case(CaseStatus.PASSED)]) == RunStatus.PASSED
    assert run_outcome([
        _case(CaseStatus.PASSED, "T/t1"), _case(CaseStatus.SKIPPED, "T/t2")
    ]) == RunStatus.PASSED
    assert run_outcome([
        _case(CaseStatus.PASSED, "T/t1"), _case(CaseStatus.FAILED, "T/t2")
    ]) == RunStatus.FAILED
    assert run_outcome([_case(CaseStatus.ERROR)]) == RunStatus.FAILED


def test_run_id_content_addressed():
    assert run_id_for("idem-1") == run_id_for("idem-1")
    assert run_id_for("idem-1") != run_id_for("idem-2")
    assert run_id_for("idem-1").startswith("run-")


def test_is_rerun():
    assert not is_rerun(_req())
    assert is_rerun(_req(daily_budget_key="AlarmKitSpike:2026-11-10"))


# ── 重跑配额 ─────────────────────────────────────────────────────────────────


def _entry(request_id: str, date: str = "2026-11-10",
           key: str = "k", run_id: str = "r1") -> BudgetEntry:
    return BudgetEntry(budget_key=key, date=date, request_id=request_id, run_id=run_id)


def test_budget_first_reruns_consume_slots_then_deny():
    ledger: list[BudgetEntry] = []
    for i in range(3):
        d = evaluate_rerun(request_id=f"req-{i}", budget_key="k",
                           date="2026-11-10", ledger=ledger, quota=3)
        assert d.allowed and d.reason == BUDGET_OK and not d.already_counted
        ledger.append(_entry(f"req-{i}"))
    d = evaluate_rerun(request_id="req-3", budget_key="k",
                       date="2026-11-10", ledger=ledger, quota=3)
    assert not d.allowed and d.reason == BUDGET_DENIED


def test_budget_replay_is_idempotent():
    ledger = [_entry("req-1")]
    d = evaluate_rerun(request_id="req-1", budget_key="k",
                       date="2026-11-10", ledger=ledger, quota=1)
    assert d.allowed and d.reason == BUDGET_REPLAY and d.already_counted


def test_budget_resets_next_day():
    ledger = [_entry("req-1", date="2026-11-10")]
    d = evaluate_rerun(request_id="req-2", budget_key="k",
                       date="2026-11-11", ledger=ledger, quota=1)
    assert d.allowed and d.reason == BUDGET_OK


def test_budget_quota_zero_blocks_new_allows_replay():
    d = evaluate_rerun(request_id="req-new", budget_key="k",
                       date="2026-11-10", ledger=[], quota=0)
    assert not d.allowed and d.reason == BUDGET_DENIED
    d2 = evaluate_rerun(request_id="req-1", budget_key="k",
                        date="2026-11-10", ledger=[_entry("req-1")], quota=0)
    assert d2.allowed and d2.reason == BUDGET_REPLAY


def test_budget_replay_with_contradictory_key_fails():
    with pytest.raises(ExecutionError):
        evaluate_rerun(request_id="req-1", budget_key="other-key",
                       date="2026-11-10", ledger=[_entry("req-1", key="k")], quota=3)


# ── 租约回收 ─────────────────────────────────────────────────────────────────


def test_reclaim_ignores_non_running_and_unexpired():
    now = _T0
    assert decide_reclaim(
        JobLease(job_id="j1", status="queued", attempts=1,
                 lease_expires_at=now - timedelta(seconds=1)),
        now=now, max_attempts=3,
    ).action == RECLAIM_NONE
    assert decide_reclaim(
        JobLease(job_id="j2", status="running", attempts=1,
                 lease_expires_at=now + timedelta(seconds=10)),
        now=now, max_attempts=3,
    ).action == RECLAIM_NONE
    assert decide_reclaim(
        JobLease(job_id="j3", status="running", attempts=1),
        now=now, max_attempts=3,
    ).action == RECLAIM_NONE


def test_reclaim_expired_lease_is_requeued():
    d = decide_reclaim(
        JobLease(job_id="j1", status="running", attempts=1,
                 lease_expires_at=_T0 - timedelta(seconds=1)),
        now=_T0, max_attempts=3,
    )
    assert d.action == RECLAIM_REQUEUE and d.reason == "lease_expired"


def test_reclaim_exhausted_attempts_dead_letters():
    d = decide_reclaim(
        JobLease(job_id="j1", status="running", attempts=3,
                 lease_expires_at=_T0 - timedelta(seconds=1)),
        now=_T0, max_attempts=3,
    )
    assert d.action == RECLAIM_DEAD_LETTER and d.reason == "max_attempts_exceeded"
