"""FileHybridSessionStore 测试（docs/01 §6，H3）：原子写、往返、穿越守卫、排序。"""

from __future__ import annotations

import json

import pytest

from testpilot_adapters.hybrid_session_store import (
    FileHybridSessionStore,
    SessionIdUnsafeError,
)
from testpilot_core.enums import SessionStatus, StepStatus
from testpilot_core.hybrid_plan import (
    HybridSessionRecord,
    SessionCheckpoint,
)


def _record(session_id: str, updated_at: str) -> HybridSessionRecord:
    return HybridSessionRecord(
        session_id=session_id,
        plan={"plan_schema_version": "hybrid-plan-v1", "steps": []},
        checkpoint=SessionCheckpoint(
            session_id=session_id, plan_hash="a" * 64,
            status=SessionStatus.RUNNING,
            step_statuses={"s1": StepStatus.RUNNING},
            current_step_id="s1", updated_at=updated_at),
    )


def test_roundtrip_and_atomic_layout(tmp_path):
    store = FileHybridSessionStore(tmp_path)
    record = _record("hsess-0123456789ab", "2026-11-14T08:00:00Z")
    store.put(record)

    path = tmp_path / "hybrid-sessions" / "hsess-0123456789ab.json"
    assert path.exists()
    # 落盘是合法 JSON 且含计划+游标两部分（崩溃恢复不依赖外部引用）
    blob = json.loads(path.read_text(encoding="utf-8"))
    assert blob["checkpoint"]["current_step_id"] == "s1"
    assert blob["plan"]["plan_schema_version"] == "hybrid-plan-v1"
    # 原子写残片不留名
    assert not list((tmp_path / "hybrid-sessions").glob("*.tmp"))

    loaded = store.get("hsess-0123456789ab")
    assert loaded is not None
    assert loaded.checkpoint.status is SessionStatus.RUNNING
    assert store.get("hsess-ffffffffffff") is None


def test_overwrite_is_atomic_replace(tmp_path):
    store = FileHybridSessionStore(tmp_path)
    sid = "hsess-0123456789ab"
    store.put(_record(sid, "2026-11-14T08:00:00Z"))
    advanced = _record(sid, "2026-11-14T08:05:00Z")
    advanced.checkpoint.step_statuses["s1"] = StepStatus.COMPLETED
    store.put(advanced)

    reloaded = store.get(sid)
    assert reloaded.checkpoint.updated_at == "2026-11-14T08:05:00Z"
    assert reloaded.checkpoint.step_statuses["s1"] is StepStatus.COMPLETED


def test_list_all_sorted_by_updated_at(tmp_path):
    store = FileHybridSessionStore(tmp_path)
    store.put(_record("hsess-111111111111", "2026-11-14T09:00:00Z"))
    store.put(_record("hsess-000000000000", "2026-11-14T08:00:00Z"))
    ids = [r.session_id for r in store.list_all()]
    assert ids == ["hsess-000000000000", "hsess-111111111111"]


@pytest.mark.parametrize("bad", [
    "hsess-xyz",
    "../hsess-0123456789ab",
    "hsess-0123456789ab/../../etc",
    "gate-0123456789ab",
])
def test_unsafe_session_id_rejected(tmp_path, bad):
    store = FileHybridSessionStore(tmp_path)
    with pytest.raises(SessionIdUnsafeError):
        store.get(bad)
    with pytest.raises(SessionIdUnsafeError):
        record = _record(bad, "2026-11-14T08:00:00Z")
        store.put(record)
