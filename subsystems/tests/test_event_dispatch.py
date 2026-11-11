"""事件消费骨架测试（FR-CON-005 / FR-FRS-001 AC2，tempfile 库 hermetic）。"""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from testpilot_adapters.json_events import JsonReplayEventSource
from testpilot_adapters.store import bootstrap_store
from testpilot_adapters.tracers import NoopTracer
from testpilot_core.models import EventEnvelope
from testpilot_workers.event_dispatch import (
    dispatch_events,
    register_handler,
    reset_handlers,
)


class _FakeContainer:
    """只装 dispatch 用到的 store/tracer/ci_events 三个键。"""

    def __init__(self, store, event_source=None):
        self.adapters = {
            "store": store,
            "tracer": NoopTracer(),
        }
        if event_source is not None:
            self.adapters["ci_events"] = event_source

    def get(self, name: str):
        return self.adapters.get(name)


def _envelope(event_id: str, event_type: str = "CommitPushed", **overrides) -> EventEnvelope:
    base = dict(
        event_id=event_id,
        event_type=event_type,
        source_system="json-git",
        occurred_at=datetime(2026, 11, 11, 8, 0, tzinfo=timezone.utc),
        project="alarmkit",
        subject_ref="deadbeef",
        idempotency_key=f"idem-{event_id}",
        payload={"commit": "deadbeef"},
    )
    base.update(overrides)
    return EventEnvelope(**base)


def _write_event(events_dir: Path, name: str, data: dict) -> None:
    (events_dir / f"{name}.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8"
    )


@pytest.fixture(autouse=True)
def _clean_handlers():
    yield
    reset_handlers()


async def test_dispatch_happy_path_marks_processed(tmp_path):
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    for i, event_type in enumerate(
        ["CommitPushed", "PipelineFinished", "TestCaseUpdated"]
    ):
        _write_event(events_dir, f"e{i}", _envelope(f"evt-{i}", event_type).model_dump(mode="json"))
    store = bootstrap_store(tmp_path / "data")
    container = _FakeContainer(store, JsonReplayEventSource(events_dir))

    report = await dispatch_events(container)

    assert report.scanned == 3
    assert report.new == 3
    assert report.processed == 3
    assert report.dead == 0
    assert report.by_type == {"CommitPushed": 1, "PipelineFinished": 1, "TestCaseUpdated": 1}
    rows = store.list_events()
    assert {r["status"] for r in rows} == {"processed"}
    assert len(rows) == 3


async def test_replay_is_idempotent_handler_not_rerun(tmp_path):
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    _write_event(events_dir, "e0", _envelope("evt-0").model_dump(mode="json"))
    calls: list[str] = []

    async def recording_handler(container, event):
        calls.append(event.event_id)

    register_handler("CommitPushed", recording_handler)
    store = bootstrap_store(tmp_path / "data")
    container = _FakeContainer(store, JsonReplayEventSource(events_dir))

    first = await dispatch_events(container)
    second = await dispatch_events(container)

    assert first.new == 1 and first.processed == 1
    assert second.new == 0 and second.replayed == 1 and second.processed == 0
    assert calls == ["evt-0"]  # 重放不重跑 handler
    assert len(store.list_events()) == 1  # 事件行零增生


async def test_malformed_envelope_goes_dead_letter_batch_survives(tmp_path):
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    # 缺 event_id/idempotency_key：pydantic 解析关就过不去
    _write_event(events_dir, "bad", {
        "event_type": "CommitPushed", "source_system": "json-git",
        "project": "alarmkit", "subject_ref": "x",
        "occurred_at": "2026-11-11T08:00:00Z",
    })
    _write_event(events_dir, "good", _envelope("evt-good").model_dump(mode="json"))
    store = bootstrap_store(tmp_path / "data")
    container = _FakeContainer(store, JsonReplayEventSource(events_dir))

    report = await dispatch_events(container)

    assert report.scanned == 2
    assert report.dead == 1
    assert report.processed == 1  # 同批好事件不受牵连
    dead = store.list_dead_letters()
    assert len(dead) == 1
    assert dead[0]["source"] == "event-dispatch:parse_error"
    assert store.list_events(status="processed")[0]["event_id"] == "evt-good"


async def test_unknown_event_type_marked_dead_with_reason(tmp_path):
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    _write_event(
        events_dir, "weird",
        _envelope("evt-weird", event_type="SomethingHappened").model_dump(mode="json"),
    )
    store = bootstrap_store(tmp_path / "data")
    container = _FakeContainer(store, JsonReplayEventSource(events_dir))

    report = await dispatch_events(container)

    assert report.dead == 1
    assert report.dead_reasons == {"unknown_event_type": 1}
    assert store.list_events(status="dead")[0]["event_id"] == "evt-weird"
    assert store.list_dead_letters()[0]["source"] == "event-dispatch:unknown_event_type"


async def test_blank_required_field_marked_dead(tmp_path):
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    _write_event(
        events_dir, "blank",
        _envelope("evt-blank", source_system=" ").model_dump(mode="json"),
    )
    store = bootstrap_store(tmp_path / "data")
    container = _FakeContainer(store, JsonReplayEventSource(events_dir))

    report = await dispatch_events(container)

    assert report.dead_reasons == {"missing_field": 1}
    assert store.list_events(status="dead")[0]["event_id"] == "evt-blank"


async def test_handler_exception_isolated_to_dead_status(tmp_path):
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    _write_event(events_dir, "e0", _envelope("evt-0").model_dump(mode="json"))
    _write_event(
        events_dir, "e1",
        _envelope("evt-1", event_type="PipelineFinished").model_dump(mode="json"),
    )

    async def boom(container, event):
        raise RuntimeError("handler 炸了")

    register_handler("CommitPushed", boom)
    store = bootstrap_store(tmp_path / "data")
    container = _FakeContainer(store, JsonReplayEventSource(events_dir))

    report = await dispatch_events(container)

    assert report.dead == 1 and report.processed == 1
    dead_row = next(r for r in store.list_events() if r["event_id"] == "evt-0")
    assert dead_row["status"] == "dead"
    good_row = next(r for r in store.list_events() if r["event_id"] == "evt-1")
    assert good_row["status"] == "processed"
    assert store.list_dead_letters()[0]["source"] == "event-dispatch:handler_error"


async def test_source_filter_applied_at_replay(tmp_path):
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    _write_event(
        events_dir, "a",
        _envelope("evt-a", source_system="json-git").model_dump(mode="json"),
    )
    _write_event(
        events_dir, "b",
        _envelope("evt-b", source_system="json-ci",
                  event_type="PipelineFinished").model_dump(mode="json"),
    )
    store = bootstrap_store(tmp_path / "data")
    container = _FakeContainer(store, JsonReplayEventSource(events_dir))

    report = await dispatch_events(container, source="json-ci")

    assert report.scanned == 1
    assert store.list_events()[0]["event_id"] == "evt-b"


async def test_missing_event_source_is_loud_error(tmp_path):
    store = bootstrap_store(tmp_path / "data")
    container = _FakeContainer(store)
    with pytest.raises(ValueError, match="ci_events"):
        await dispatch_events(container)


async def test_replay_raw_returns_dicts_without_validation(tmp_path):
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    _write_event(events_dir, "e0", _envelope("evt-0").model_dump(mode="json"))
    source = JsonReplayEventSource(events_dir)
    raw = await source.replay_raw("")
    assert len(raw) == 1
    path, data = raw[0]
    assert path.name == "e0.json"
    assert data["event_id"] == "evt-0"
