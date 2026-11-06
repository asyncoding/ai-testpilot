"""核心异常 → HTTP 状态码映射（只允许存在于 orchestrator，core 零 fastapi 导入）。

统一错误体：{"error": {"type": 异常类名, "message": 异常文案}}。
pydantic 请求体校验失败走 FastAPI 原生 422，不在此映射内。
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

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


def _error_body(exc: TestPilotError) -> dict:
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
