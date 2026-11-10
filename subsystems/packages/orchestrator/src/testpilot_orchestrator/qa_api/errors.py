"""核心异常 → HTTP 状态码映射（只允许存在于 orchestrator，core 零 fastapi 导入）。

统一错误体：{"error": {"type": 异常类名, "message": 异常文案}}。
pydantic 请求体校验失败走 FastAPI 原生 422，不在此映射内。
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from testpilot_core.approval import ApprovalValidationError
from testpilot_core.badcase import BadCaseError
from testpilot_core.case_relations import CaseRelationError
from testpilot_core.rule_assets import AssetValidationError
from testpilot_core.errors import (

    AdapterUnavailableError,
    EvidenceInsufficientError,
    NotFoundError,
    PolicyDeniedError,
    SchemaValidationError,
    TestPilotError,
    UnsupportedCapabilityError,
)

# 显式映射表：新增核心异常类型时必须在此登记 HTTP 语义，杜绝静默落到 500
_STATUS_MAP: dict[type[TestPilotError], int] = {
    NotFoundError: 404,
    PolicyDeniedError: 403,
    AdapterUnavailableError: 503,
    UnsupportedCapabilityError: 501,
    SchemaValidationError: 502,
    EvidenceInsufficientError: 422,
}


def _error_body(exc: Exception) -> dict:
    return {"error": {"type": type(exc).__name__, "message": str(exc)}}


def register_exception_handlers(app: FastAPI) -> None:
    """按具体异常类型注册 handler；TestPilotError 本身兜底 500。"""

    def make_handler(status_code: int):
        async def handler(_request: Request, exc: TestPilotError) -> JSONResponse:
            return JSONResponse(status_code=status_code, content=_error_body(exc))

        return handler

    for exc_type, status_code in _STATUS_MAP.items():
        app.add_exception_handler(exc_type, make_handler(status_code))

    async def fallback(_request: Request, exc: TestPilotError) -> JSONResponse:
        # 未登记的核心异常视为基础设施级失败，不吞异常、不伪造成功响应
        return JSONResponse(status_code=500, content=_error_body(exc))

    app.add_exception_handler(TestPilotError, fallback)

    # S2.5：人审语义错误（幽灵 URN/覆盖不全/动作矛盾）是 ValueError 子类，
    # 不在 TestPilotError 体系内，单独映射 422（与 EvidenceInsufficient 同位）
    async def approval_validation_handler(
        _request: Request, exc: ApprovalValidationError
    ) -> JSONResponse:
        return JSONResponse(status_code=422, content=_error_body(exc))

    app.add_exception_handler(ApprovalValidationError, approval_validation_handler)

    # S2.6a：L3 资产版本语义错误（坏 ID/kind 串档/空内容/空样本回流）同为
    # ValueError 子类，映射 422；NotFoundError 已在 _STATUS_MAP 映射 404。
    async def asset_validation_handler(
        _request: Request, exc: AssetValidationError
    ) -> JSONResponse:
        return JSONResponse(status_code=422, content=_error_body(exc))

    app.add_exception_handler(AssetValidationError, asset_validation_handler)

    # S2.6b：关系派生语义错误（坏 scope/闭集外 kind）同为 ValueError 子类，映射 422。
    async def case_relation_handler(
        _request: Request, exc: CaseRelationError
    ) -> JSONResponse:
        return JSONResponse(status_code=422, content=_error_body(exc))

    app.add_exception_handler(CaseRelationError, case_relation_handler)

    # S2.6c：bad case 状态机语义错误（非法迁移/非 accepted 转 golden/👍 强投影）
    # 同为 ValueError 子类，映射 422；NotFoundError 已在 _STATUS_MAP 映射 404。
    async def bad_case_handler(
        _request: Request, exc: BadCaseError
    ) -> JSONResponse:
        return JSONResponse(status_code=422, content=_error_body(exc))

    app.add_exception_handler(BadCaseError, bad_case_handler)
