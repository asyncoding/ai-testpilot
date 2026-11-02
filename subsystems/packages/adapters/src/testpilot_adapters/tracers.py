"""trace 适配器：NoopTracer（默认）与 LangfuseTracer（可选依赖，懒加载）。

S0 门禁：空链路 trace 出现在 Langfuse（用 scripts/smoke_trace.py 在 compose 起来后验证）。
"""

from __future__ import annotations

from contextlib import contextmanager
from types import TracebackType
from typing import Any


class NoopTracer:
    @contextmanager
    def span(self, name: str, *, run_id: str = "", metadata: dict[str, Any] | None = None):
        yield {"name": name, "run_id": run_id, "noop": True}


class LangfuseTracer:
    """Langfuse v3.9+/v5 OTel 风格 SDK。

    需要 `uv pip install 'langfuse>=5.0'` 与 LANGFUSE_* 环境变量。
    旧 v2 SDK 的 client.trace()/trace.span() 已移除，v5 用
    start_as_current_observation + propagate_attributes（trace 级属性）。
    """

    def __init__(self, *, host: str, public_key: str, secret_key: str) -> None:
        try:
            from langfuse import Langfuse
        except ImportError as exc:  # pragma: no cover - 依赖可选
            raise ImportError(
                "LangfuseTracer 需要安装 langfuse：uv pip install 'langfuse>=5.0'"
            ) from exc
        self._client = Langfuse(host=host, public_key=public_key, secret_key=secret_key)

    @contextmanager
    def span(self, name: str, *, run_id: str = "", metadata: dict[str, Any] | None = None):
        from langfuse import propagate_attributes

        with self._client.start_as_current_observation(name=name, metadata=metadata or {}):
            with propagate_attributes(
                trace_name=name,
                session_id=run_id or None,
                metadata=metadata or None,
            ):
                try:
                    yield
                except Exception as exc:
                    # 异常时给 span 打错误标记后原样抛出；观测不吞业务异常
                    self._client.update_current_span(
                        level="ERROR", status_message=str(exc)[:2000]
                    )
                    raise
        self._client.flush()

    def __enter__(self) -> "LangfuseTracer":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._client.flush()  # pragma: no cover
