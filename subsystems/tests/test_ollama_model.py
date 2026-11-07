"""OllamaModelGateway 契约（docs/08 FR-CON-006）：httpx MockTransport，不起真实服务。

覆盖：批量 embed 形状、chat 信封与 token 计量、传输层退避重试与快速失败、
structured 的 format 入参 / 解析失败重试上限 / 降级信封、trace 元数据。
"""

import json

import httpx
import pytest

from testpilot_adapters.ollama_model import OllamaModelGateway, _has_required_nonempty
from testpilot_core.errors import AdapterUnavailableError


def make_transport(handlers):
    """httpx.MockTransport（同步 handler 可用于 AsyncClient）+ 请求记录。

    超出预置数量的请求复用最后一个 handler；handler 为异常实例则抛出。
    返回 (transport, requests)。
    """
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        h = handlers[min(len(requests) - 1, len(handlers) - 1)]
        if isinstance(h, Exception):
            raise h
        status, body = h
        return httpx.Response(status, json=body)

    return httpx.MockTransport(handler), requests


def bodies_of(requests):
    return [json.loads(r.content) for r in requests]


class FakeSleeper:
    def __init__(self):
        self.waited = []

    async def __call__(self, seconds):
        self.waited.append(seconds)


class FakeSpan:
    def __init__(self, captured, meta):
        self.captured = captured
        self.meta = meta

    def __enter__(self):
        self.captured.append(self.meta)
        return self

    def __exit__(self, *exc):
        return False


class FakeTracer:
    def __init__(self):
        self.spans = []

    def span(self, name, *, run_id="", metadata=None):
        return FakeSpan(self.spans, {"name": name, **(metadata or {})})


def _gateway(handlers, *, sleeper=None, **kw):
    transport, requests = make_transport(handlers)
    gw = OllamaModelGateway(
        transport=transport,
        sleeper=sleeper,
        max_retries=kw.pop("max_retries", 2),
        backoff_seconds=kw.pop("backoff_seconds", 0.5),
        **kw,
    )
    return gw, requests


CHAT_BODY = {
    "model": "qwen2.5:7b",
    "message": {"role": "assistant", "content": "裸按侧边键只停钟"},
    "prompt_eval_count": 120,
    "eval_count": 30,
    "done": True,
}
CHAT_OK = (200, CHAT_BODY)


# ── embed ────────────────────────────────────────────────────────────────────

async def test_embed_batch_payload_and_vectors():
    gw, tr = _gateway([(200, {"model": "dmeta", "embeddings": [[0.1, 0.2], [0.3, 0.4]]})],
                      embed_model="dmeta-x")
    vectors = await gw.embed(["物理键", "侧边键"])
    assert vectors == [[0.1, 0.2], [0.3, 0.4]]
    assert tr[0].url.path == "/api/embed"
    # 默认 keep_alive=30m 随每个请求下发，防止 Ollama 空闲 5m 卸载（冷加载 55.3s）
    assert bodies_of(tr)[0] == {
        "model": "dmeta-x", "input": ["物理键", "侧边键"], "keep_alive": "30m"}


async def test_keep_alive_none_omits_field_on_embed_and_chat():
    gw, tr = _gateway([(200, {"embeddings": [[0.1]]}), CHAT_OK], keep_alive=None)
    await gw.embed(["x"])
    await gw.chat([])
    embed_body, chat_body = bodies_of(tr)
    assert "keep_alive" not in embed_body and "keep_alive" not in chat_body


async def test_embed_empty_input_skips_http():
    gw, tr = _gateway([])
    assert await gw.embed([]) == []
    assert tr == []


async def test_embed_shape_mismatch_raises():
    gw, _ = _gateway([(200, {"embeddings": [[0.1]]})])  # 2 条输入只回 1 条
    with pytest.raises(AdapterUnavailableError):
        await gw.embed(["a", "b"])


# ── chat 信封与重试 ───────────────────────────────────────────────────────────

async def test_chat_envelope_with_usage_and_latency():
    gw, tr = _gateway([CHAT_OK])
    resp = await gw.chat([{"role": "user", "content": "侧边键兜底？"}], temperature=0.2)
    assert resp["text"] == "裸按侧边键只停钟"
    assert resp["model"] == "qwen2.5:7b"
    assert resp["tokens_in"] == 120 and resp["tokens_out"] == 30
    assert resp["degraded"] is False and resp["latency_ms"] >= 0
    body = bodies_of(tr)[0]
    assert body["stream"] is False and body["options"] == {"temperature": 0.2}


async def test_chat_retries_503_then_succeeds_with_backoff():
    sleeper = FakeSleeper()
    gw, tr = _gateway([(503, {"error": "busy"}), CHAT_OK], sleeper=sleeper)
    resp = await gw.chat([{"role": "user", "content": "hi"}])
    assert resp["text"].startswith("裸按侧边键")
    assert len(tr) == 2
    assert sleeper.waited == [0.5]  # 0.5 * 2^0


async def test_chat_400_fails_fast_without_retry():
    gw, tr = _gateway([(400, {"error": "bad request"})])
    with pytest.raises(AdapterUnavailableError):
        await gw.chat([])
    assert len(tr) == 1


async def test_chat_timeout_retried_then_unavailable():
    sleeper = FakeSleeper()
    gw, tr = _gateway(
        [httpx.ConnectError("boom"), httpx.ConnectError("boom")], sleeper=sleeper
    )
    with pytest.raises(AdapterUnavailableError):
        await gw.chat([])
    assert len(tr) == 3  # 1 次首发 + 2 次重试（max_retries=2）
    assert sleeper.waited == [0.5, 1.0]


# ── structured ───────────────────────────────────────────────────────────────

async def test_structured_passes_schema_as_format_and_returns_data():
    gw, tr = _gateway([(200, {**CHAT_BODY, "message": {"content": '{"answer": "四层", "citations": ["u1"]}'}})])
    schema = {"type": "object", "required": ["answer", "citations"]}
    resp = await gw.structured([{"role": "user", "content": "..."}], schema)
    assert resp["degraded"] is False and resp["attempts"] == 1
    assert resp["data"] == {"answer": "四层", "citations": ["u1"]}
    assert bodies_of(tr)[0]["format"] == schema


async def test_structured_retries_on_bad_json_then_succeeds():
    gw, tr = _gateway([
        (200, {**CHAT_BODY, "message": {"content": "抱歉，答案是 ```json {乱"},
              "eval_count": 10}),
        (200, {**CHAT_BODY, "message": {"content": '{"answer": "ok"}'}, "eval_count": 7}),
    ])
    schema = {"type": "object", "required": ["answer"]}
    resp = await gw.structured([{"role": "user", "content": "q"}], schema)
    assert resp["degraded"] is False and resp["attempts"] == 2
    assert resp["data"] == {"answer": "ok"}
    # 第二次请求追加了 assistant 坏输出 + user 纠错消息
    second_messages = bodies_of(tr)[1]["messages"]
    assert second_messages[-1]["role"] == "user" and "JSON" in second_messages[-1]["content"]
    assert second_messages[-2]["role"] == "assistant"
    # token 跨尝试累计
    assert resp["tokens_out"] == 17


async def test_structured_missing_required_field_counts_as_failure():
    bad = (200, {**CHAT_BODY, "message": {"content": '{"other": 1}'}})
    gw, _ = _gateway([bad, bad], structured_max_attempts=2)
    resp = await gw.structured([], {"type": "object", "required": ["answer"]})
    assert resp["degraded"] is True and resp["attempts"] == 2 and resp["data"] is None


async def test_structured_all_bad_returns_degraded_without_raising():
    bad = (200, {**CHAT_BODY, "message": {"content": "not json"}})
    gw, tr = _gateway([bad, bad, bad], structured_max_attempts=3)
    resp = await gw.structured([], {"type": "object"})
    assert resp["degraded"] is True and resp["attempts"] == 3 and resp["data"] is None
    assert resp["text"] == "not json"
    assert len(tr) == 3


# ── structured 业务级非空字段（required_nonempty，P1 浅校验收紧）────────────────

async def test_structured_empty_required_field_retries_with_dedicated_hint_then_passes():
    # 首稿 citations=[]：schema required 通过但业务非空不过，适配器循环内当场重发，
    # 不白跑一轮图迭代（真实事故里零引用草稿要到 citation_check 才被拦）
    gw, tr = _gateway([
        (200, {**CHAT_BODY, "message": {"content": '{"summary": "x", "citations": []}'}}),
        (200, {**CHAT_BODY, "message": {
            "content": '{"summary": "x", "citations": [{"urn": "u1", "quote": "q"}]}'}}),
    ])
    schema = {"type": "object", "required": ["summary", "citations"]}
    resp = await gw.structured([{"role": "user", "content": "q"}], schema,
                               required_nonempty=("citations",))
    assert resp["degraded"] is False and resp["attempts"] == 2
    assert resp["data"]["citations"][0]["urn"] == "u1"
    hint = bodies_of(tr)[1]["messages"][-1]["content"]
    assert "缺少非空字段" in hint and "citations" in hint


async def test_structured_empty_required_field_exhausts_to_degraded():
    empty = (200, {**CHAT_BODY, "message": {"content": '{"summary": "x", "citations": []}'}})
    gw, tr = _gateway([empty, empty], structured_max_attempts=2)
    resp = await gw.structured([], {"type": "object", "required": ["summary", "citations"]},
                               required_nonempty=("citations",))
    assert resp["degraded"] is True and resp["data"] is None and resp["attempts"] == 2
    assert len(tr) == 2


async def test_structured_without_required_nonempty_keeps_legacy_shape_only():
    # 不传该选项时维持旧契约：键存在即过，空列表不算适配器层失败
    gw, tr = _gateway([
        (200, {**CHAT_BODY, "message": {"content": '{"summary": "x", "citations": []}'}}),
    ])
    resp = await gw.structured([], {"type": "object", "required": ["summary", "citations"]})
    assert resp["degraded"] is False and resp["attempts"] == 1
    assert resp["data"] == {"summary": "x", "citations": []}
    assert len(tr) == 1


def test_has_required_nonempty_pure_function_contract():
    # hostless 纯函数：不触网、不依赖事件循环，直接钉死判定口径
    assert _has_required_nonempty({}, ()) is True  # 无要求永远通过
    assert _has_required_nonempty({"citations": ["u"]}, ("citations",)) is True
    assert _has_required_nonempty({"citations": "u"}, ("citations",)) is True
    assert _has_required_nonempty({"citations": []}, ("citations",)) is False
    assert _has_required_nonempty({"citations": ""}, ("citations",)) is False
    assert _has_required_nonempty({}, ("citations",)) is False  # 缺键
    assert _has_required_nonempty("not-a-dict", ("citations",)) is False
    # 多字段全真才过
    assert _has_required_nonempty(
        {"a": ["x"], "b": "y"}, ("a", "b")) is True
    assert _has_required_nonempty(
        {"a": ["x"], "b": ""}, ("a", "b")) is False


# ── trace（FR-CON-006 AC3）────────────────────────────────────────────────────

async def test_trace_span_carries_model_tokens_latency():
    tracer = FakeTracer()
    transport, _ = make_transport([CHAT_OK])
    gw = OllamaModelGateway(transport=transport, tracer=tracer)
    await gw.chat([])
    assert tracer.spans[-1]["name"] == "model.chat"
    assert tracer.spans[-1]["model"] == "qwen2.5:7b"
    assert tracer.spans[-1]["tokens_in"] == 120
    assert "latency_ms" in tracer.spans[-1]


async def test_trace_records_error_when_transport_exhausted():
    tracer = FakeTracer()
    gw = OllamaModelGateway(
        transport=make_transport([httpx.ConnectError("x")])[0],
        tracer=tracer,
        max_retries=0,
        sleeper=FakeSleeper(),
    )
    with pytest.raises(AdapterUnavailableError):
        await gw.chat([])
    assert tracer.spans[-1]["error"]
