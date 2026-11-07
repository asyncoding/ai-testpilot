"""lifespan 模型预热（_schedule_warmup）契约。

冷加载实测 55.3s，预热把这笔开销从首个真实提问移到启动后台：
- 仅 Ollama 网关安排任务，recorded/stub（model=None）跳过；
- 成功路径 chat+embed 各调一次；
- 失败只记日志、任务正常收口，绝不向 lifespan 抛穿（预热是优化不是契约）。
"""

from __future__ import annotations

import httpx
import pytest

from testpilot_adapters.ollama_model import OllamaModelGateway
from testpilot_adapters.recorded_model import RecordedModelGateway
from testpilot_orchestrator.app import _schedule_warmup

CHAT_BODY = {
    "model": "qwen2.5:7b",
    "message": {"role": "assistant", "content": "pong"},
    "prompt_eval_count": 1, "eval_count": 1, "done": True,
}


class _FakeContainer:
    """只实现 _schedule_warmup 实际使用的面（get + warmup_task 属性）。"""

    def __init__(self, model):
        self._model = model
        self.warmup_task = None

    def get(self, name):
        return self._model if name == "model" else None


def _transport(handlers):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        h = handlers[min(len(requests) - 1, len(handlers) - 1)]
        if isinstance(h, Exception):
            raise h
        return httpx.Response(h[0], json=h[1])

    return httpx.MockTransport(handler), requests


async def test_warmup_skipped_for_recorded_gateway():
    container = _FakeContainer(RecordedModelGateway("/nonexistent/fixtures.json"))
    _schedule_warmup(container)  # 类型名不匹配：不安排任何后台任务
    assert container.warmup_task is None


async def test_warmup_skipped_when_model_absent():
    container = _FakeContainer(None)
    _schedule_warmup(container)
    assert container.warmup_task is None


async def test_warmup_calls_chat_and_embed_then_task_finishes():
    transport, requests = _transport([
        (200, CHAT_BODY),
        (200, {"model": "dmeta", "embeddings": [[0.0]]}),
    ])
    gw = OllamaModelGateway(transport=transport, keep_alive="30m")
    container = _FakeContainer(gw)

    _schedule_warmup(container)
    assert container.warmup_task is not None
    await container.warmup_task  # 无异常抛出即成功收口

    paths = [r.url.path for r in requests]
    assert paths == ["/api/chat", "/api/embed"]
    assert container.warmup_task.done() and container.warmup_task.exception() is None
    await gw.aclose()


async def test_warmup_failure_is_contained_not_raised():
    # 传输层每次都连接失败：_warm 内部必须吞掉异常，lifespan 不受影响
    transport, _ = _transport([httpx.ConnectError("ollama down")])
    gw = OllamaModelGateway(transport=transport, max_retries=0)
    container = _FakeContainer(gw)

    _schedule_warmup(container)
    await container.warmup_task  # 不抛异常即通过（失败只走 logger.warning）
    assert container.warmup_task.done() and container.warmup_task.exception() is None
    await gw.aclose()
