"""/api/gates 人工闸门 HTTP 端到端测试（docs/15 §7，H2）。

hermetic：offline-demo profile + tmp data_dir，TestClient 不起端口；
过期路径用 gate_service 的 now 注入显式推时间，不依赖真实 sleep。
红证覆盖：过期单拒绝响应、空证据/野字段 422、未知单 404、挂单幂等、
非法 gate_id 路径穿越 → 404。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from testpilot_orchestrator.app import create_app
from testpilot_orchestrator.config_loader import load_profile
from testpilot_orchestrator.factory import build_container
from testpilot_workers import gate_service

_SUBSYSTEMS = Path(__file__).resolve().parents[1]
_PROFILE = _SUBSYSTEMS / "config" / "profiles" / "offline-demo.yaml"


@pytest.fixture
def container(tmp_path):
    config = load_profile(_PROFILE)
    config.paths["data_dir"] = str(tmp_path / "data")
    return build_container(config, _SUBSYSTEMS)


def _client(container) -> TestClient:
    return TestClient(create_app(container=container))


def _action_body(**over) -> dict:
    body = {
        "session_id": "sess-1",
        "step_id": "s3-human-lock",
        "actor": "human:action",
        "action": "lock_screen",
        "prompt": "请在 60 秒内按电源键锁屏",
        "deadline_seconds": 120,
    }
    body.update(over)
    return body


def _verdict_body() -> dict:
    return {
        "session_id": "sess-1",
        "step_id": "s5-sensory",
        "actor": "human:verdict",
        "action": "sensory_check",
        "prompt": "确认锁屏期间铃声与震动",
        "verdict_form": {
            "fields": [
                {"key": "ringtone_heard", "label": "听到铃声", "kind": "boolean"},
                {"key": "confidence", "label": "把握", "kind": "choice",
                 "options": ["high", "medium", "low"], "required": True},
            ],
            "require_evidence": True,
        },
        "evidence_urns": ["urn:testpilot:store:artifact:oslog-ring-chain"],
        "deadline_seconds": 120,
    }


# ── 挂单与队列 ───────────────────────────────────────────────────────────────


def test_open_gate_list_and_detail(container):
    with _client(container) as client:
        resp = client.post("/api/gates", json=_action_body())
        assert resp.status_code == 200
        gate = resp.json()
        assert gate["status"] == "pending"
        assert gate["actor"] == "human:action"
        assert gate["response"] is None
        # 路由挂真实挂钟：deadline 必须恰为 created_at + 120s
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        delta = datetime.strptime(gate["deadline"], fmt) - datetime.strptime(
            gate["created_at"], fmt)
        assert delta == timedelta(seconds=120)

        listing = client.get("/api/gates").json()["gates"]
        assert [g["gate_id"] for g in listing] == [gate["gate_id"]]

        detail = client.get(f"/api/gates/{gate['gate_id']}").json()
        assert detail["prompt"] == _action_body()["prompt"]


def test_open_gate_is_idempotent_for_same_session_and_step(container):
    with _client(container) as client:
        first = client.post("/api/gates", json=_action_body()).json()
        # 崩溃重建：同会话同步骤再挂，必须返回同一条而非弹两张卡
        second = client.post("/api/gates", json=_action_body()).json()
        assert second["gate_id"] == first["gate_id"]
        assert second["created_at"] == first["created_at"]
        assert len(client.get("/api/gates?status=pending").json()["gates"]) == 1


def test_auto_actor_gate_rejected_422(container):
    with _client(container) as client:
        body = _action_body(actor="auto:dh-ax", action="home",
                            step_id="s2-auto")
        resp = client.post("/api/gates", json=body)
        assert resp.status_code == 422
        assert "不是人工步" in resp.json()["error"]["message"]


def test_deadline_bounds_rejected(container):
    with _client(container) as client:
        resp = client.post("/api/gates", json=_action_body(deadline_seconds=0))
        assert resp.status_code == 422  # pydantic Field 约束
        resp = client.post("/api/gates", json=_action_body(deadline_seconds=4000))
        assert resp.status_code == 422


# ── 裁决响应 ─────────────────────────────────────────────────────────────────


def test_verdict_gate_respond_happy_and_queue_filters(container):
    with _client(container) as client:
        gate = client.post("/api/gates", json=_verdict_body()).json()

        ok = client.post(f"/api/gates/{gate['gate_id']}/respond", json={
            "values": {"ringtone_heard": True, "confidence": "high"},
            "evidence_urns": ["urn:testpilot:store:artifact:oslog-ring-chain"],
            "decided_by": "tester-zhang",
        })
        assert ok.status_code == 200
        responded = ok.json()
        assert responded["status"] == "responded"
        assert responded["response"]["decided_by"] == "tester-zhang"
        assert responded["response"]["decided_at"]  # 服务端盖章

        pending = client.get("/api/gates?status=pending").json()["gates"]
        assert pending == []
        done = client.get("/api/gates?status=responded").json()["gates"]
        assert len(done) == 1


def test_verdict_requires_evidence_and_rejects_wild_fields(container):
    with _client(container) as client:
        gate = client.post("/api/gates", json=_verdict_body()).json()
        url = f"/api/gates/{gate['gate_id']}/respond"

        no_evidence = client.post(url, json={
            "values": {"ringtone_heard": True, "confidence": "high"},
            "evidence_urns": [], "decided_by": "t1"})
        assert no_evidence.status_code == 422
        assert "空证据裁决" in no_evidence.json()["error"]["message"]

        wild = client.post(url, json={
            "values": {"ringtone_heard": True, "confidence": "high",
                       "ghost": 1},
            "evidence_urns": ["urn:testpilot:store:artifact:oslog-ring-chain"],
            "decided_by": "t1"})
        assert wild.status_code == 422
        assert "表单外字段" in wild.json()["error"]["message"]

        bad_choice = client.post(url, json={
            "values": {"ringtone_heard": True, "confidence": "absolute"},
            "evidence_urns": ["urn:testpilot:store:artifact:oslog-ring-chain"],
            "decided_by": "t1"})
        assert bad_choice.status_code == 422
        assert "不在 options" in bad_choice.json()["error"]["message"]

        missing_witness = client.post(url, json={
            "values": {"ringtone_heard": True, "confidence": "high"},
            "evidence_urns": ["urn:testpilot:store:artifact:oslog-ring-chain"],
            "decided_by": ""})
        assert missing_witness.status_code == 422  # pydantic min_length


def test_action_gate_ack_needs_no_form(container):
    with _client(container) as client:
        gate = client.post("/api/gates", json=_action_body()).json()
        resp = client.post(f"/api/gates/{gate['gate_id']}/respond", json={
            "values": {}, "evidence_urns": [], "decided_by": "tester-li"})
        assert resp.status_code == 200
        assert resp.json()["status"] == "responded"


# ── 过期/续期/放弃 ───────────────────────────────────────────────────────────


def test_expired_gate_blocks_response_renew_reopens(container):
    with _client(container) as client:
        gate = client.post("/api/gates", json=_verdict_body()).json()
        gid = gate["gate_id"]

        # 服务层注入未来时间触发读时清扫并持久化 expired（相对真实挂钟，
        # 不 sleep：闸门 deadline=真实 now+120s，推到 +300s 必过期）
        future = datetime.now(timezone.utc) + timedelta(seconds=300)
        swept = gate_service.list_gates(container, now=future)
        assert swept[0].status.value == "expired"

        stale = client.post(f"/api/gates/{gid}/respond", json={
            "values": {"ringtone_heard": True, "confidence": "high"},
            "evidence_urns": ["urn:testpilot:store:artifact:oslog-ring-chain"],
            "decided_by": "t1"})
        assert stale.status_code == 422
        assert "先续期" in stale.json()["error"]["message"]

        renewed = client.post(f"/api/gates/{gid}/renew",
                              json={"extra_seconds": 300}).json()
        assert renewed["status"] == "pending"
        assert renewed["renewed_at"] is not None

        ok = client.post(f"/api/gates/{gid}/respond", json={
            "values": {"ringtone_heard": True, "confidence": "high"},
            "evidence_urns": ["urn:testpilot:store:artifact:oslog-ring-chain"],
            "decided_by": "t1"})
        assert ok.status_code == 200


def test_abandon_cancels_and_terminal_guard(container):
    with _client(container) as client:
        gate = client.post("/api/gates", json=_action_body()).json()
        gid = gate["gate_id"]

        canceled = client.post(f"/api/gates/{gid}/abandon").json()
        assert canceled["status"] == "canceled"

        # 已取消不能再响应（状态闭集），也不能再取消
        assert client.post(f"/api/gates/{gid}/respond", json={
            "values": {}, "decided_by": "t1"}).status_code == 422
        assert client.post(f"/api/gates/{gid}/abandon").status_code == 422


def test_unknown_and_unsafe_gate_ids(container):
    with _client(container) as client:
        assert client.get("/api/gates/gate-000000000000").status_code == 404
        # 路径穿越字符不进文件拼接，按不存在处理而非 500
        resp = client.get("/api/gates/..%2Fescape")
        assert resp.status_code in (404, 422)
