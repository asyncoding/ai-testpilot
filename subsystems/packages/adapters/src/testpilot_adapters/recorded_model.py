"""录制型模型网关（ModelGatewayPort）——offline-demo profile 与无网测试用。

按 options["key"] 返回预置响应；响应里可声明 degraded=True 模拟降级。
生产替代：model-ollama / 云端 OpenAI 兼容适配器（docs/08 FR-CON-006）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from testpilot_core.errors import AdapterUnavailableError


class RecordedModelGateway:
    def __init__(self, fixtures_file: str | Path, *, model_version: str = "recorded-local") -> None:
        self.path = Path(fixtures_file)
        self.model_version = model_version
        self._responses: dict[str, dict[str, Any]] = {}
        if self.path.exists():
            self._responses = json.loads(self.path.read_text(encoding="utf-8"))

    async def chat(self, messages: list[dict[str, str]], **options: Any) -> dict[str, Any]:
        key = options.get("key", "default")
        if key not in self._responses:
            raise AdapterUnavailableError(f"录制响应缺少 key: {key}")
        resp = self._responses[key]
        return {
            "text": resp.get("text", ""),
            "model": self.model_version,
            "tokens_in": resp.get("tokens_in", 0),
            "tokens_out": resp.get("tokens_out", 0),
            "degraded": resp.get("degraded", False),
        }

    async def structured(self, messages: list[dict[str, str]], schema: dict[str, Any], **options: Any) -> dict[str, Any]:
        key = options.get("key", "default")
        if key not in self._responses:
            raise AdapterUnavailableError(f"录制响应缺少 key: {key}")
        return self._responses[key].get("structured", {})

    async def embed(self, texts: list[str]) -> list[list[float]]:
        # 离线演示用确定性伪向量（仅用于连通/形状测试，不用于真实相似度排序）
        return [[float((len(t) + i) % 7) / 7.0 for i in range(8)] for t in texts]
