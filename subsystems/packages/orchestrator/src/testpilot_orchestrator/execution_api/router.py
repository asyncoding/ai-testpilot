"""/api/runs/* 路由：执行账本、排队提交与归因详情（S3.5b）。

薄 HTTP 包装原则同 badcase_api：排队/取消/配额/状态机语义都在
workers.execution_service，本层只做容器取接与序列化，不写业务规则。
异常映射复用 qa_api/errors：NotFound→404、PolicyDenied（含配额）→403、
AdapterUnavailable→503；请求体直接复用语言中立的 core TestExecutionRequest，
不另造 HTTP 专用模型，避免契约双写漂移（FR-EX-001）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request

from testpilot_core.models import TestExecutionRequest
from testpilot_workers import execution_service as svc

execution_router = APIRouter(prefix="/api/runs", tags=["execution"])


@execution_router.get("")
async def list_runs(
    request: Request,
    status: str | None = Query(default=None),
    project: str | None = Query(default=None),
) -> dict[str, Any]:
    """执行账本盘点：可按 status/project 过滤，含归因计数投影。"""
    rows = svc.list_runs(
        request.app.state.container, status=status, project=project)
    return {"runs": rows}


@execution_router.post("")
async def submit_run(
    body: TestExecutionRequest, request: Request
) -> dict[str, Any]:
    """登记请求并入队；同 idempotency_key 重放返回同一 run_id（replayed=true）。"""
    return await svc.submit_execution(request.app.state.container, body)


@execution_router.get("/{run_id}")
async def get_run(run_id: str, request: Request) -> dict[str, Any]:
    """单 run 详情：账本投影 + TestRun（用例/制品/归因结论/未覆盖层）；幽灵 id 404。"""
    return svc.get_run_detail(request.app.state.container, run_id)


@execution_router.post("/{run_id}/cancel")
async def cancel_run(run_id: str, request: Request) -> dict[str, Any]:
    """协作式取消：只对 queued/running 的 job 生效，幂等返回标记结果。"""
    return await svc.request_cancel(request.app.state.container, run_id)
