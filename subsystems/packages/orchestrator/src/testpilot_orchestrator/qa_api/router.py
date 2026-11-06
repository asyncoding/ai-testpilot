"""/api/qa/* 路由：QaService 的薄 HTTP 包装（无业务逻辑，异常映射在 errors.py）。

- 状态挂载：lifespan 把 container 放 app.state.container、QaService 放 app.state.qa；
- core 模型不被 fastapi 污染：入参用 qa_api.schemas 的 HTTP 模型，内部转 core AskRequest；
- trace 的 Langfuse 深链只在本层按 profile 注入：LangfuseTracer 以 session_id=run_id
  关联（tracers.py），noop tracer 下如实给 null，服务层不读配置也不造链接。
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, Query, Request

from testpilot_core.errors import NotFoundError
from testpilot_core.models import AskRequest

from .schemas import AskHttpRequest, FeedbackHttpRequest

api_router = APIRouter(prefix="/api")


def _langfuse_link(container, run_id: str) -> dict[str, str] | None:
    """tracer=langfuse 时给 session 深链；否则 null（不造数）。

    host 取 LANGFUSE_HOST——与 factory.build_container 构造 LangfuseTracer 时
    读的是同一个环境变量（tracer 对象本身不保留 host）。
    """
    if container.config.adapters.tracer.type != "langfuse":
        return None
    host = os.environ.get("LANGFUSE_HOST", "http://localhost:3030").rstrip("/")
    return {"host": host, "session_url": f"{host}/traces?session={run_id}"}


@api_router.post("/qa/ask")
async def ask(body: AskHttpRequest, request: Request) -> dict[str, Any]:
    qa = request.app.state.qa
    ask_request = AskRequest(
        question=body.question,
        project=body.project,
        branch=body.branch,
        acl_scope=body.acl_scope,
    )
    response = await qa.ask(ask_request, case_key=body.case_key)
    # mode="json"：StrEnum 序列化为字面值，前端只收字符串不收枚举对象
    return response.model_dump(mode="json")


@api_router.get("/qa/runs")
async def list_runs(
    request: Request,
    status: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    qa = request.app.state.qa
    return {"runs": await qa.list_runs(status=status, limit=limit)}


@api_router.get("/qa/runs/{run_id}")
async def get_run(run_id: str, request: Request) -> dict[str, Any]:
    qa = request.app.state.qa
    run = await qa.get_run(run_id)
    if run is None:
        raise NotFoundError(f"qa_run 不存在: {run_id}")
    return run


@api_router.get("/qa/runs/{run_id}/sources/{urn:path}")
async def get_run_source(run_id: str, urn: str, request: Request) -> dict[str, Any]:
    # urn:path 允许 URN 内含斜杠分段；白名单守卫在 QaService，路径穿越形态同归 404
    qa = request.app.state.qa
    return await qa.get_run_source(run_id, urn)


@api_router.get("/qa/runs/{run_id}/trace")
async def get_run_trace(run_id: str, request: Request) -> dict[str, Any]:
    qa = request.app.state.qa
    trace = await qa.get_run_trace(run_id)
    trace["langfuse"] = _langfuse_link(request.app.state.container, run_id)
    return trace


@api_router.post("/qa/runs/{run_id}/feedback")
async def record_feedback(
    run_id: str, body: FeedbackHttpRequest, request: Request
) -> dict[str, Any]:
    qa = request.app.state.qa
    # 幽灵 run 在服务层抛 NotFoundError → errors.py 映射 404，且不留孤儿行
    feedback_id = await qa.record_feedback(
        run_id, body.kind, reason=body.reason, detail=body.detail
    )
    return {"id": feedback_id}


@api_router.get("/qa/feedback/summary")
async def feedback_summary(request: Request) -> list[dict[str, Any]]:
    # 直通 store.feedback_summary 的聚合行（FR-QA-004 AC2）
    qa = request.app.state.qa
    return await qa.feedback_summary()


@api_router.get("/config")
async def get_config(request: Request) -> dict[str, Any]:
    """前端装配依据：录制夹具横幅（FR-OPS-002）、适配器形态、写开关都从这里读。"""
    container = request.app.state.container
    adapters = container.config.adapters
    return {
        "profile": container.config.profile,
        "project": container.config.project.name,
        "branch": container.config.project.default_branch,
        "model_adapter": adapters.model.type,
        "search_adapter": adapters.search.type,
        "recorded": adapters.model.type == "recorded",
        "external_write_enabled": container.config.external_write_enabled,
        "tracer": adapters.tracer.type,
    }
