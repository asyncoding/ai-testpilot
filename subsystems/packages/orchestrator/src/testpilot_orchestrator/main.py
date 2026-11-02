"""uvicorn 入口：testpilot-orchestrator 仅在 S0 提供健康检查。"""

from .app import app

__all__ = ["app"]
