"""录制模型网关契约（docs/08 FR-CON-006）。"""

import json
from pathlib import Path

import pytest  # noqa: F401  # 未来 structured 失败重试用例预留

from testpilot_adapters.recorded_model import RecordedModelGateway
from testpilot_core.errors import AdapterUnavailableError

FIXTURES = Path(__file__).parent / "fixtures" / "model" / "responses.json"


async def test_recorded_chat_carries_usage_and_degraded_flag():
    gw = RecordedModelGateway(FIXTURES)
    resp = await gw.chat([{"role": "user", "content": "侧边键兜底"}], key="qa_kill_backup")
    assert "30s" in resp["text"]
    assert resp["model"] == "recorded-local"
    assert resp["tokens_in"] == 850 and resp["tokens_out"] == 60
    assert resp["degraded"] is False

    degraded = await gw.chat([], key="qa_unanswerable")
    assert degraded["degraded"] is True


async def test_missing_key_and_embed_shape():
    gw = RecordedModelGateway(FIXTURES)
    with pytest.raises(AdapterUnavailableError):
        await gw.chat([], key="not-there")
    vectors = await gw.embed(["a", "bb"])
    assert len(vectors) == 2 and all(len(v) == 8 for v in vectors)


async def test_structured_required_nonempty_parity_with_ollama(tmp_path):
    # 与 OllamaModelGateway 同一业务级口径：空 citations 在适配器层即降级，
    # 用临时夹具隔离，不改动共享 responses.json（S0/S1.4 契约测试依赖它）
    fixture = tmp_path / "responses.json"
    fixture.write_text(json.dumps({
        "empty_cite": {"structured": {"summary": "x", "citations": []}},
        "good": {"structured": {
            "summary": "x", "citations": [{"urn": "u1", "quote": "q"}]}},
    }, ensure_ascii=False), encoding="utf-8")
    gw = RecordedModelGateway(fixture)

    bad = await gw.structured([], {"type": "object"}, key="empty_cite",
                              required_nonempty=("citations",))
    assert bad["degraded"] is True and bad["data"] is None

    ok = await gw.structured([], {"type": "object"}, key="good",
                             required_nonempty=("citations",))
    assert ok["degraded"] is False and ok["data"]["citations"][0]["urn"] == "u1"

    # 不传选项时维持旧行为：形状合法即返回，空列表不降级
    plain = await gw.structured([], {"type": "object"}, key="empty_cite")
    assert plain["degraded"] is False and plain["data"] == {"summary": "x", "citations": []}
