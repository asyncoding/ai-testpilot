"""录制模型网关契约（docs/08 FR-CON-006）。"""

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
