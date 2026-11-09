"""/api/approvals/* 路由：人审队列/决策/审计账本/派发/写策略（S2.5）。

薄 HTTP 包装原则同 qa_api：业务语义在 core.approval 与 workers.approval_service，
本层只做 HTTP 模型转换与容器取接；异常映射复用 qa_api/errors.py。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request

from testpilot_core.approval import CaseDecisionRequest, SuggestionDecision
from testpilot_core.errors import PolicyDeniedError
from testpilot_workers.approval_service import get_review_queue, review_case
from testpilot_workers.outbox_dispatch import dispatch_pending_outbox

from .schemas import CaseDecisionHttp, WritePolicyHttp

approval_router = APIRouter(prefix="/api/approvals", tags=["approvals"])


def _policy_view(container) -> dict[str, Any]:
    policy = container.write_policy
    return {
        "profile_external_write_enabled": policy.profile_enabled,
        "runtime_disabled": policy.runtime_disabled,
        "effective_writes_enabled": policy.writes_enabled,
    }


@approval_router.get("/queue")
async def review_queue(request: Request) -> dict[str, Any]:
    """待审队列：只含 pending 草稿分组，已评审项不出现（FR-UI-002）。"""
    container = request.app.state.container
    items = get_review_queue(container)
    return {"queue": [item.model_dump(mode="json") for item in items]}


@approval_router.post("/cases/{case_urn:path}/decide")
async def decide_case(
    case_urn: str, body: CaseDecisionHttp, request: Request
) -> dict[str, Any]:
    """一次用例级人审；core fail-closed 校验，单事务落四件套。"""
    container = request.app.state.container
    core_request = CaseDecisionRequest(
        case_urn=case_urn,
        decision=body.decision,
        reviewer=body.reviewer,
        reason=body.reason,
        items=[
            SuggestionDecision(
                suggestion_urn=item.suggestion_urn,
                action=item.action,
                oracle_type=item.oracle_type,
                expectation=item.expectation,
            )
            for item in body.items
        ],
    )
    plan = review_case(container, core_request)
    return plan.model_dump(mode="json")


@approval_router.get("/records")
async def list_records(
    request: Request, limit: int = Query(default=100, ge=1, le=500)
) -> dict[str, Any]:
    """审批动作账本：评审人/时间/结论/请求 diff（FR-WR-001 AC2）。"""
    store = request.app.state.container.get("store")
    records = store.list_approvals(limit=limit)
    return {"records": [a.model_dump(mode="json") for a in records]}


@approval_router.get("/outbox")
async def list_outbox(request: Request) -> dict[str, Any]:
    """外部副作用账本：目标系统/请求体/幂等键/回滚说明四要素齐（FR-WR-001 AC1）。"""
    store = request.app.state.container.get("store")
    records = store.list_outbox_records()
    return {"records": [r.model_dump(mode="json") for r in records]}


@approval_router.post("/outbox/dispatch")
async def dispatch_outbox(request: Request) -> dict[str, Any]:
    """触发一轮 pending/failed outbox 派发；报告键稳定，便于前端轮询与钉测。"""
    container = request.app.state.container
    return await dispatch_pending_outbox(container)


@approval_router.get("/write-policy")
async def get_write_policy(request: Request) -> dict[str, Any]:
    return _policy_view(request.app.state.container)


@approval_router.post("/write-policy")
async def set_write_policy(body: WritePolicyHttp, request: Request) -> dict[str, Any]:
    """kill switch 翻转：只允许收紧。profile 关着时请求放开 → 403，绝不越权。"""
    container = request.app.state.container
    policy = container.write_policy
    if not body.runtime_disabled and not policy.profile_enabled:
        raise PolicyDeniedError(
            "profile 未启用外部写（external_write_enabled=false），"
            "运行期只能收紧不能越权放开"
        )
    policy.runtime_disabled = body.runtime_disabled
    return _policy_view(container)
