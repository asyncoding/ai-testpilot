"""问答 HTTP API（S1.5，FR-UI-001 后端面）。

分层约束：本包是 fastapi 能出现的唯一新位置（import_lint 守门，core 零 web 依赖）。
router 只做 core/orchestrator 冻结契约的薄包装 + HTTP 异常映射，业务逻辑全部在
QaService；请求模型 AskHttpRequest 是编排层入参，core AskRequest 保持冻结不改。
"""

from .errors import register_exception_handlers
from .router import api_router

__all__ = ["api_router", "register_exception_handlers"]
