"""Ollama 模型网关（ModelGatewayPort，docs/08 FR-CON-006）。

httpx 直连 Ollama HTTP API，不引官方 SDK：
- POST /api/embed  批量向量（input 传字符串数组，一次往返）；
- POST /api/chat   多轮对话（stream=False），prompt_eval_count/eval_count 即 token；
- structured() 在 chat 基础上传 format=<JSON Schema>，解析失败按上限重试，
  超过上限返回 degraded 信封（data=None），不抛异常——调用方按降级路径走。

可靠性：超时/连接错误/429/5xx 做指数退避重试，重试用尽抛 AdapterUnavailableError
（structured 的 JSON 解析失败不算传输故障，走自己的 attempts 上限）。
每次调用回传 model/tokens/latency_ms；可选 tracer 把同样信息写 span（AC3）。

base_url 指向任何 OpenAI/Ollama 兼容端点时由子类/配置换路径；本类只实现 Ollama 协议。
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Awaitable, Callable

import httpx

from testpilot_adapters.http_util import trust_env_for
from testpilot_core.errors import AdapterUnavailableError

_RETRY_STATUS = {429, 500, 502, 503, 504}


class OllamaModelGateway:
    def __init__(
        self,
        *,
        base_url: str = "http://localhost:11434",
        chat_model: str = "qwen2.5:7b",
        embed_model: str = "shaw/dmeta-embedding-zh",
        timeout: float = 60.0,
        max_retries: int = 2,
        backoff_seconds: float = 0.5,
        structured_max_attempts: int = 2,
        transport: httpx.AsyncBaseTransport | None = None,
        tracer: Any | None = None,
        sleeper: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.chat_model = chat_model
        self.embed_model = embed_model
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_seconds = backoff_seconds
        self.structured_max_attempts = structured_max_attempts
        self._transport = transport
        self._tracer = tracer
        self._sleeper = sleeper or asyncio.sleep
        self._client: httpx.AsyncClient | None = None

    async def _client_get(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout,
                transport=self._transport,
                # loopback 端点绕过 Clash 类系统代理（否则 localhost 被代理 502）
                trust_env=trust_env_for(self.base_url),
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _post_json(
        self, path: str, payload: dict[str, Any], *, span_name: str
    ) -> dict[str, Any]:
        """带超时/退避重试的 POST，返回解析后的 JSON。重试用尽抛 AdapterUnavailableError。"""
        client = await self._client_get()
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            t0 = time.monotonic()
            try:
                resp = await client.post(path, json=payload)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = exc
                resp = None
            latency_ms = int((time.monotonic() - t0) * 1000)
            if resp is not None:
                if resp.status_code < 400:
                    self._trace(span_name, payload, resp.json(), latency_ms, error="")
                    return resp.json()
                last_exc = httpx.HTTPStatusError(
                    f"Ollama {path} HTTP {resp.status_code}: {resp.text[:200]}",
                    request=resp.request,
                    response=resp,
                )
                if resp.status_code not in _RETRY_STATUS:
                    break
            if attempt < self.max_retries:
                await self._sleeper(self.backoff_seconds * (2**attempt))
        self._trace(span_name, payload, {}, 0, error=str(last_exc))
        raise AdapterUnavailableError(f"Ollama 调用失败（{path}）：{last_exc}")

    def _trace(
        self,
        span_name: str,
        payload: dict[str, Any],
        result: dict[str, Any],
        latency_ms: int,
        *,
        error: str,
    ) -> None:
        """CON-006 AC3：模型版本/token/延迟落 trace；tracer 缺省为空操作。"""
        if self._tracer is None:
            return
        meta: dict[str, Any] = {
            "model": result.get("model", payload.get("model", "")),
            "latency_ms": latency_ms,
            "tokens_in": result.get("prompt_eval_count", 0),
            "tokens_out": result.get("eval_count", 0),
        }
        if error:
            meta["error"] = error
        with self._tracer.span(span_name, metadata=meta):
            pass

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        data = await self._post_json(
            "/api/embed",
            {"model": self.embed_model, "input": texts},
            span_name="model.embed",
        )
        vectors = data.get("embeddings")
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            raise AdapterUnavailableError(
                f"Ollama embed 响应形状异常：期望 {len(texts)} 条，"
                f"实际 {0 if not isinstance(vectors, list) else len(vectors)} 条"
            )
        return vectors

    async def chat(
        self, messages: list[dict[str, str]], **options: Any
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": str(options.pop("model", self.chat_model)),
            "messages": messages,
            "stream": False,
        }
        extra = {k: v for k, v in options.items() if k in {"temperature", "top_p", "seed"}}
        if extra:
            payload["options"] = extra
        t0 = time.monotonic()
        data = await self._post_json("/api/chat", payload, span_name="model.chat")
        latency_ms = int((time.monotonic() - t0) * 1000)
        return {
            "text": data.get("message", {}).get("content", ""),
            "model": data.get("model", payload["model"]),
            "tokens_in": data.get("prompt_eval_count", 0),
            "tokens_out": data.get("eval_count", 0),
            "latency_ms": latency_ms,
            "degraded": False,
        }

    async def structured(
        self,
        messages: list[dict[str, str]],
        schema: dict[str, Any],
        **options: Any,
    ) -> dict[str, Any]:
        """JSON 结构化输出。

        Ollama 支持 format=<JSON Schema> 约束输出；返回仍需自行 json.loads。
        解析（或对象形状）失败时追加纠错提示重试，至多 structured_max_attempts 次；
        用尽后返回 degraded=True、data=None 的信封，不抛异常（FR-CON-006 AC2）。
        """
        attempts_messages = list(messages)
        last_text = ""
        attempts = 0
        t0 = time.monotonic()
        tokens_in = tokens_out = 0
        model = ""
        for attempts in range(1, self.structured_max_attempts + 1):
            payload: dict[str, Any] = {
                "model": str(options.pop("model", self.chat_model)),
                "messages": attempts_messages,
                "stream": False,
                "format": schema,
            }
            data = await self._post_json(
                "/api/chat", payload, span_name="model.structured"
            )
            last_text = data.get("message", {}).get("content", "")
            model = data.get("model", payload["model"])
            tokens_in += data.get("prompt_eval_count", 0)
            tokens_out += data.get("eval_count", 0)
            try:
                parsed = json.loads(last_text)
            except json.JSONDecodeError:
                parsed = None
            if parsed is not None and _matches_schema_shape(parsed, schema):
                return {
                    "data": parsed,
                    "text": last_text,
                    "model": model,
                    "tokens_in": tokens_in,
                    "tokens_out": tokens_out,
                    "latency_ms": int((time.monotonic() - t0) * 1000),
                    "attempts": attempts,
                    "degraded": False,
                }
            attempts_messages = attempts_messages + [
                {"role": "assistant", "content": last_text},
                {
                    "role": "user",
                    "content": (
                        "上一次输出不是符合所给 JSON Schema 的合法 JSON。"
                        "请只输出一个 JSON 对象，不要包含 markdown 代码块或任何解释文字。"
                    ),
                },
            ]
        return {
            "data": None,
            "text": last_text,
            "model": model,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "latency_ms": int((time.monotonic() - t0) * 1000),
            "attempts": attempts,
            "degraded": True,
        }


def _matches_schema_shape(parsed: Any, schema: dict[str, Any]) -> bool:
    """轻量形状校验：只核对 type=object 时的顶层必填字段（避免引 jsonschema 重依赖）。"""
    expected_type = schema.get("type")
    if expected_type == "object":
        if not isinstance(parsed, dict):
            return False
        return all(k in parsed for k in schema.get("required", []))
    if expected_type == "array":
        return isinstance(parsed, list)
    return True
