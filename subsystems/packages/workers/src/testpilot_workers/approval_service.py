"""用例草稿人审服务（docs/08 FR-TD-004 / FR-WR-001，S2.5）。

core 纯函数算计划、本服务只做取数与单事务落账，纪律与 S2.1～S2.4 各 worker 一致：

- ``get_review_queue``：pending 草稿 + 正式库用例 → core 分组出审批卡片；
- ``review_case``：校验幽灵 URN/覆盖完整性等语义全部在 core fail-closed，
  通过后 ``persist_case_decision`` 单事务落 Approval/outbox/偏好样本/草稿状态。

正式 ``test_cases`` 表在本服务**只读不写**（FR-TD-004 AC1）：回写动作以 outbox
记账，由 dispatch worker 派发到独立写根的 MarkdownCaseRepository。
"""

from __future__ import annotations

from typing import Any

from testpilot_core.approval import (
    CaseDecisionPlan,
    CaseDecisionRequest,
    ReviewQueueItem,
    SuggestionView,
    build_review_queue,
    plan_case_decision,
)
from testpilot_core.errors import NotFoundError


def _to_views(rows: list[dict[str, Any]]) -> list[SuggestionView]:
    # store 行带评审留痕等额外字段，pydantic 默认 ignore，只取视图契约字段
    return [SuggestionView(**row) for row in rows]


def get_review_queue(container: Any) -> list[ReviewQueueItem]:
    """全库待审卡片（已评审草稿不出现），供审批页 GET queue。"""
    store = container.get("store")
    views = _to_views(store.list_review_queue_rows())
    cases = store.list_test_cases(include_cancelled=True)
    return build_review_queue(views, cases)


def review_case(container: Any, request: CaseDecisionRequest) -> CaseDecisionPlan:
    """一次用例级人审：core 出计划 → store 单事务落四件套，返回计划供 API 呈现。"""
    store = container.get("store")
    case = store.get_test_case(request.case_urn)
    if case is None:
        raise NotFoundError(f"待审用例不存在或未入库: {request.case_urn}")
    views = _to_views(store.list_pending_suggestions_for_case(request.case_urn))
    plan = plan_case_decision(request, case, views)
    store.persist_case_decision(
        plan, reviewed_at=plan.approval.decided_at.isoformat())
    return plan
