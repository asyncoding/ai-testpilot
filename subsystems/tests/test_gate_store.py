"""FileGateStore 文件态闸门存储 hermetic 测试（docs/15 §7，H2）。

tmp_path 隔离：原子写、排序、gate_id 路径穿越守卫、崩溃重建读回完整生命周期。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from testpilot_adapters.gate_store import FileGateStore, GateIdUnsafeError
from testpilot_core.enums import GateStatus, StepActor
from testpilot_core.hybrid_plan import (
    HumanVerdict,
    PlanStep,
    VerdictField,
    VerdictForm,
    build_gate_request,
)

_T0 = datetime(2026, 11, 14, 8, 0, tzinfo=timezone.utc)


def _human_step(step_id: str, *, with_form: bool = False) -> PlanStep:
    return PlanStep(
        step_id=step_id,
        actor=StepActor.HUMAN_VERDICT if with_form else StepActor.HUMAN_ACTION,
        action="sensory_check" if with_form else "lock_screen",
        prompt="确认铃声" if with_form else "请锁屏",
        verdict_form=VerdictForm(fields=[
            VerdictField(key="ringtone_heard", label="铃声", kind="boolean")])
        if with_form else None,
        evidence_bindings=["urn:testpilot:store:artifact:e1"] if with_form else [],
        idempotency_key=f"k-{step_id}")


def test_put_get_roundtrip_and_missing_none(tmp_path):
    store = FileGateStore(tmp_path)
    gate = build_gate_request(
        session_id="sess-1", step=_human_step("s3"), evidence_urns=[],
        now=_T0, deadline_seconds=90)

    store.put(gate)
    loaded = store.get(gate.gate_id)
    assert loaded == gate
    assert store.get("gate-000000000000") is None
    # 落盘在 gates/ 子目录，零 DDL 不碰 sqlite
    assert (tmp_path / "gates" / f"{gate.gate_id}.json").is_file()


def test_atomic_replace_leaves_no_tmp_and_latest_wins(tmp_path):
    store = FileGateStore(tmp_path)
    gate = build_gate_request(
        session_id="sess-1", step=_human_step("s3"), evidence_urns=[],
        now=_T0, deadline_seconds=90)
    store.put(gate)
    store.put(gate.model_copy(update={"status": GateStatus.EXPIRED}))

    files = list((tmp_path / "gates").iterdir())
    assert all(p.suffix == ".json" for p in files)
    assert store.get(gate.gate_id).status is GateStatus.EXPIRED


def test_list_all_sorted_by_created_at(tmp_path):
    store = FileGateStore(tmp_path)
    g1 = build_gate_request(
        session_id="sess-1", step=_human_step("s3"), evidence_urns=[],
        now=_T0, deadline_seconds=90)
    g2 = build_gate_request(
        session_id="sess-1", step=_human_step("s4"), evidence_urns=[],
        now=_T0 + timedelta(minutes=2), deadline_seconds=90)
    store.put(g2)
    store.put(g1)  # 乱序写入

    assert [g.gate_id for g in store.list_all()] == [g1.gate_id, g2.gate_id]


def test_full_lifecycle_payload_survives_reload(tmp_path):
    store = FileGateStore(tmp_path)
    gate = build_gate_request(
        session_id="sess-1", step=_human_step("s5", with_form=True),
        evidence_urns=["urn:testpilot:store:artifact:oslog"],
        now=_T0, deadline_seconds=120)
    verdict = HumanVerdict(
        gate_id=gate.gate_id,
        values={"ringtone_heard": True},
        evidence_urns=["urn:testpilot:store:artifact:oslog"],
        note="环境安静，确认听到", decided_by="tester-zhang",
        decided_at="2026-11-14T08:05:00Z")
    store.put(gate.model_copy(update={
        "status": GateStatus.RESPONDED, "response": verdict}))

    loaded = store.get(gate.gate_id)
    assert loaded.status is GateStatus.RESPONDED
    assert loaded.response.values == {"ringtone_heard": True}
    assert loaded.verdict_form.fields[0].key == "ringtone_heard"


def test_unsafe_gate_id_rejected_before_path_join(tmp_path):
    store = FileGateStore(tmp_path)
    evil = "../escape"
    with pytest.raises(GateIdUnsafeError):
        store.get(evil)
    with pytest.raises(GateIdUnsafeError):
        store._path("gate-xyz")  # 非 12 位 hex 同样拒绝
    # 守卫失败后 gates 目录外无任何写入
    assert not (tmp_path / "escape").exists()


def test_gate_id_shape_contract(tmp_path):
    # 钉死 gate_id 闭集形态，store 正则与 core 哈希实现互为红证
    gate = build_gate_request(
        session_id="sess-1", step=_human_step("s3"), evidence_urns=[],
        now=_T0, deadline_seconds=60)
    assert gate.gate_id.startswith("gate-") and len(gate.gate_id) == len("gate-") + 12
    int(gate.gate_id.split("-")[1], 16)
    # GateRequest 本身可直接构造异常 id 测 store 守卫
    with pytest.raises(GateIdUnsafeError):
        FileGateStore(tmp_path).get("gate-gggggggggggg")
