"""/api/bad-cases/* 路由：反馈/bad case 闭环台账（S2.6c FR-EV-005）。

薄 HTTP 包装原则同 approval_api/assets_api：状态机语义在 core.badcase，
落账在 workers.badcase_service，本层只做 HTTP 模型转换与容器取接。
异常映射复用 qa_api/errors.py：BadCaseError → 422，NotFoundError → 404。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request

from testpilot_workers import badcase_service as svc

from .schemas import PromoteHttp, RegressionHttp, TriageHttp

badcase_router = APIRouter(prefix="/api/bad-cases", tags=["bad-cases"])


@badcase_router.get("")
async def list_bad_cases(
    request: Request,
    status: str | None = Query(default=None),
    source: str | None = Query(default=None),
) -> dict[str, Any]:
    """台账盘点：可按状态/来源过滤，排序口径在 store。"""
    container = request.app.state.container
    cases = svc.list_bad_cases(container, status=status, source=source)
    return {"bad_cases": [c.model_dump(mode="json") for c in cases]}


@badcase_router.get("/stats")
async def bad_case_stats(request: Request) -> dict[str, Any]:
    """闭环统计：各态计数/闭环率/中位周期（空库安全）。"""
    return svc.bad_case_stats(request.app.state.container)


@badcase_router.post("/ingest-feedback")
async def ingest_feedback(request: Request) -> dict[str, Any]:
    """扫反馈表把 👎 负反馈投影成 bad case；👍 与已入账项跳过，报告键稳定。"""
    return svc.ingest_feedback(request.app.state.container)


@badcase_router.post("/{bad_case_id}/triage")
async def triage_case(
    bad_case_id: str, body: TriageHttp, request: Request
) -> dict[str, Any]:
    """分诊 accept/reject；core 校验状态闭集并补审计事件。"""
    case = svc.triage_case(
        request.app.state.container, bad_case_id,
        decision=body.decision, actor=body.actor,
        reason=body.reason, note=body.note)
    return case.model_dump(mode="json")


@badcase_router.post("/{bad_case_id}/promote")
async def promote_case(
    bad_case_id: str, body: PromoteHttp, request: Request
) -> dict[str, Any]:
    """accepted 坏例转 golden 回归题（题先建状态后翻，重放自愈）。"""
    result = svc.promote_case(
        request.app.state.container, bad_case_id,
        actor=body.actor, note=body.note)
    return {
        "bad_case": result["bad_case"].model_dump(mode="json"),
        "eval_case": result["eval_case"].model_dump(mode="json"),
    }


@badcase_router.post("/{bad_case_id}/regression")
async def associate_regression(
    bad_case_id: str, body: RegressionHttp, request: Request
) -> dict[str, Any]:
    """关联回归评测：幽灵 run → 404；pass→fixed 闭环 / fail→regression_failed。"""
    case = svc.associate_regression_run(
        request.app.state.container, bad_case_id,
        eval_run_id=body.eval_run_id, outcome=body.outcome,
        actor=body.actor, note=body.note)
    return case.model_dump(mode="json")
