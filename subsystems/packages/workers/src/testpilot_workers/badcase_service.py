"""bad case 闭环服务（docs/08 FR-EV-005，S2.6c）。

纪律与 S2.1～S2.6b 各 worker 一致：状态机语义全在 ``core.badcase`` 纯函数里
fail-closed，本服务只做取数、幂等预检与落账。

- ``ingest_feedback``：扫 feedback 表把 👎 负反馈投影成 bad case，👍 跳过、
  已入账跳过（feedback_id 幂等），四元组快照从 qa_runs 关联补齐；
- ``triage_case``：分诊 accept/reject（core 校验状态闭集）；
- ``promote_case``：accepted → golden 回归题（eval_cases 幂等 upsert）→
  状态翻 promoted，两步以「题先建、状态后翻」排序，崩溃重放可自愈不产生
  幻影 promoted（题集 upsert 同 case_id 幂等，accepted 态重放合法）；
- ``associate_regression_run``：回归 run 存在性校验后关联 pass/fail；
- ``bad_case_stats``：core 纯统计（闭环率/中位周期/各态计数）。
"""

from __future__ import annotations

from typing import Any

from testpilot_core.badcase import (
    GOLDEN_LOCKED_VERSION,
    BadCase,
    BadCaseSnapshot,
    apply_transition,
    associate_regression,
    cycle_time_stats,
    ingest_feedback_as_bad_case,
    promote_to_eval_case,
    triage,
)
from testpilot_core.enums import FeedbackKind
from testpilot_core.errors import NotFoundError


def _get_or_404(store: Any, bad_case_id: str) -> BadCase:
    case = store.get_bad_case(bad_case_id)
    if case is None:
        raise NotFoundError(f"bad case 不存在或未入账: {bad_case_id}")
    return case


def ingest_feedback(container: Any) -> dict[str, Any]:
    """扫反馈表做坏例入账；空表返回全零报告（巡检语义，不是错误）。"""
    store = container.get("store")
    ingested: list[str] = []
    skipped_useful = 0
    skipped_duplicate = 0

    for row in store.list_feedback():
        if row["kind"] != str(FeedbackKind.NOT_USEFUL):
            skipped_useful += 1
            continue
        feedback_id = str(row["id"])
        if store.get_bad_case_by_feedback(feedback_id) is not None:
            skipped_duplicate += 1
            continue
        qa_run = store.get_qa_run(row["run_id"])
        if qa_run is not None:
            snapshot = BadCaseSnapshot(
                data_version=qa_run.get("data_version") or "",
                model_version=qa_run.get("model_version")
                or row.get("model_version") or "",
                prompt_version=qa_run.get("prompt_version") or "",
                params_version=qa_run.get("params_version") or "",
            )
            question = qa_run.get("question") or ""
        else:
            # record_feedback 已拒绝孤儿反馈，这里只做防御：缺 run 也不漏台账，
            # 快照留空、question 留空，分诊时人工补
            snapshot = BadCaseSnapshot(
                model_version=row.get("model_version") or "")
            question = ""
        case = ingest_feedback_as_bad_case(
            feedback_row_id=row["id"], run_id=row["run_id"],
            kind=FeedbackKind.NOT_USEFUL, reason=row.get("reason") or "",
            detail=row.get("detail") or "", question=question,
            snapshot=snapshot)
        store.insert_bad_case(case)
        ingested.append(case.bad_case_id)

    return {
        "status": "succeeded",
        "scanned": skipped_useful + skipped_duplicate + len(ingested),
        "ingested": len(ingested),
        "skipped_useful": skipped_useful,
        "skipped_duplicate": skipped_duplicate,
        "bad_case_ids": ingested,
    }


def list_bad_cases(container: Any, *, status: str | None = None,
                   source: str | None = None) -> list[BadCase]:
    """台账盘点（可选状态/来源过滤，排序口径在 store）。"""
    return container.get("store").list_bad_cases(status=status, source=source)


def triage_case(container: Any, bad_case_id: str, *, decision: str,
                actor: str, reason: str = "", note: str = "") -> BadCase:
    """分诊：accept→accepted / reject→rejected（core 管状态闭集与审计事件）。"""
    store = container.get("store")
    case = _get_or_404(store, bad_case_id)
    updated = triage(case, decision=decision, actor=actor,
                     reason=reason, note=note)
    store.update_bad_case(updated)
    return updated


def promote_case(container: Any, bad_case_id: str, *, actor: str,
                 note: str = "") -> dict[str, Any]:
    """accepted 坏例转 golden 回归题并翻 promoted；题先建状态后翻，重放自愈。"""
    store = container.get("store")
    case = _get_or_404(store, bad_case_id)
    eval_case = promote_to_eval_case(case)  # 非 accepted 在此 fail-closed
    # upsert 幂等：同 case_id 重放只写同内容，不产生题集幻影
    store.upsert_eval_case(eval_case, locked_version=GOLDEN_LOCKED_VERSION)
    promoted = apply_transition(case, "promoted", actor=actor, note=note)
    promoted.golden_case_id = eval_case.case_id
    store.update_bad_case(promoted)
    return {"bad_case": promoted, "eval_case": eval_case}


def associate_regression_run(
    container: Any, bad_case_id: str, *, eval_run_id: str,
    outcome: str, actor: str, note: str = "",
) -> BadCase:
    """关联回归评测：run 必须真实存在（拒幽灵关联），pass→fixed/fail→regression_failed。"""
    store = container.get("store")
    case = _get_or_404(store, bad_case_id)
    if store.get_eval_run(eval_run_id) is None:
        raise NotFoundError(f"回归评测运行不存在，拒绝幽灵关联: {eval_run_id}")
    updated = associate_regression(
        case, eval_run_id=eval_run_id, outcome=outcome,
        actor=actor, note=note)
    store.update_bad_case(updated)
    return updated


def bad_case_stats(container: Any) -> dict[str, Any]:
    """闭环统计（FR-EV-005：闭环周期可统计），空库安全。"""
    stats = cycle_time_stats(container.get("store").list_bad_cases())
    return stats.model_dump()
