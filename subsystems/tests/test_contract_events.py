"""事件重放契约（docs/08 FR-CON-005）。"""

from pathlib import Path

from testpilot_adapters.json_events import JsonReplayEventSource

FIXTURES = Path(__file__).parent / "fixtures"


async def test_replay_filters_by_source():
    source = JsonReplayEventSource(FIXTURES / "events")
    events = await source.replay("json-ci")
    assert len(events) == 1
    e = events[0]
    assert e.event_type == "PipelineFinished"
    assert e.payload["build_id"] == "ci-42"
    assert await source.replay("other-system") == []

    assert await JsonReplayEventSource(FIXTURES / "no-such-dir").replay("") == []
