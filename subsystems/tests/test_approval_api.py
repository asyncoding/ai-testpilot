"""S2.5 审批 HTTP API 端到端测试（TestClient + 注入容器，FR-TD-004/FR-WR-001）。

hermetic：offline-demo profile + tmp data_dir，不读环境、不碰真实
store/offline-demo 库；lifespan 退出时自动关 store，测试内不重复 close。
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from testpilot_core.enums import OracleType
from testpilot_core.oracle_mining import MinedOracle
from testpilot_core.models import TestCase
from testpilot_orchestrator.app import create_app
from testpilot_orchestrator.config_loader import load_profile
from testpilot_orchestrator.factory import build_container

_SUBSYSTEMS = Path(__file__).resolve().parents[1]
_PROFILE = _SUBSYSTEMS / "config" / "profiles" / "offline-demo.yaml"
_DOC_URN = "urn:testpilot:fs-cases:document:cases/synthetic-cases.md"
_CASE_URN = "urn:testpilot:fs-cases:test_case:cases/synthetic-cases.md#TC-N2"
_FR1_URN = "urn:testpilot:fs-reqs:requirement:req.md#FR-1"


@pytest.fixture
def container(tmp_path):
    config = load_profile(_PROFILE)
    config.paths["data_dir"] = str(tmp_path / "data")
    return build_container(config, _SUBSYSTEMS)


def _client(container) -> TestClient:
    return TestClient(create_app(container=container))


def _seed_case_with_suggestion(store, *, expectation="落库一条 stopped 记录") -> str:
    case = TestCase(
        urn=_CASE_URN, external_id="TC-N2", title="只有步骤没有预期",
        steps=["打开 App", "触发闹钟"], tags=["FR-1"],
        module="模块 A", priority="P1", oracle_missing=True)
    store.upsert_test_case(_DOC_URN, case, content_hash="h1")
    mined = MinedOracle(
        type=OracleType.STATE_ASSERTION, expectation=expectation,
        source_kind="requirement_ac", provenance="deterministic",
        source_refs=[_FR1_URN], confidence=0.5)
    store.upsert_oracle_suggestion(
        _DOC_URN, _CASE_URN, "TC-N2", mined, content_hash="h1")
    return store.list_pending_suggestions_for_case(_CASE_URN)[0]["urn"]


def _decide_url(case_urn: str) -> str:
    return f"/api/approvals/cases/{quote(case_urn, safe='')}/decide"


# ── 队列 ────────────────────────────────────────────────────────────────────

def test_queue_empty_then_lists_seeded_item(container):
    with _client(container) as client:
        assert client.get("/api/approvals/queue").json() == {"queue": []}
        urn = _seed_case_with_suggestion(container.get("store"))

        data = client.get("/api/approvals/queue").json()["queue"]
        assert len(data) == 1
        item = data[0]
        assert item["external_id"] == "TC-N2"
        assert item["suggestions"][0]["urn"] == urn
        assert item["suggestions"][0]["oracle"]["expectation"] == "落库一条 stopped 记录"


# ── 决策 ────────────────────────────────────────────────────────────────────

def test_decide_adopt_returns_full_plan_and_persists(container):
    with _client(container) as client:
        urn = _seed_case_with_suggestion(container.get("store"))
        resp = client.post(_decide_url(_CASE_URN), json={
            "decision": "approved", "reviewer": "expert-zhang",
            "reason": "评审通过",
            "items": [{"suggestion_urn": urn, "action": "adopt"}]})
        assert resp.status_code == 200
        plan = resp.json()
        assert plan["approval"]["decision"] == "approved"
        assert plan["approval"]["action_level"] == "R2"
        assert plan["outbox"]["status"] == "pending"
        assert plan["outbox"]["idempotency_key"] == plan["idempotency_key"]
        assert plan["suggestion_updates"] == [
            {"urn": urn, "status": "approved", "reason": "评审通过"}]
        # 队列已排空（动作即时落库，FR-UI-002）
        assert client.get("/api/approvals/queue").json()["queue"] == []


def test_decide_ghost_suggestion_is_422(container):
    with _client(container) as client:
        _seed_case_with_suggestion(container.get("store"))
        resp = client.post(_decide_url(_CASE_URN), json={
            "decision": "approved", "reviewer": "expert-zhang",
            "items": [{"suggestion_urn": "urn:ghost:s1", "action": "adopt"}]})
        assert resp.status_code == 422
        assert resp.json()["error"]["type"] == "ApprovalValidationError"


def test_decide_unknown_case_is_404(container):
    with _client(container) as client:
        resp = client.post(_decide_url("urn:testpilot:fs-cases:test_case:x.md#TC-X"),
                           json={
                               "decision": "approved", "reviewer": "expert-zhang",
                               "items": [{
                                   "suggestion_urn": "urn:testpilot:x:s1",
                                   "action": "adopt"}]})
        assert resp.status_code == 404
        assert resp.json()["error"]["type"] == "NotFoundError"


def test_decide_request_body_validation_is_native_422(container):
    with _client(container) as client:
        _seed_case_with_suggestion(container.get("store"))
        # reviewer 空串违反 min_length；items 空数组违反 min_items
        resp = client.post(_decide_url(_CASE_URN), json={
            "decision": "approved", "reviewer": "", "items": []})
        assert resp.status_code == 422


# ── 审计账本：FR-WR-001 AC1 四要素 ──────────────────────────────────────────

def test_records_and_outbox_expose_audit_four_elements(container):
    with _client(container) as client:
        urn = _seed_case_with_suggestion(container.get("store"))
        client.post(_decide_url(_CASE_URN), json={
            "decision": "approved", "reviewer": "expert-zhang",
            "reason": "评审通过",
            "items": [{"suggestion_urn": urn, "action": "adopt"}]})

        records = client.get("/api/approvals/records").json()["records"]
        assert len(records) == 1
        approval = records[0]
        assert approval["reviewer"] == "expert-zhang"
        assert approval["decided_at"]  # AC2：评审人/时间/结论持久化
        assert approval["request_diff"]["case_urn"] == _CASE_URN

        outbox = client.get("/api/approvals/outbox").json()["records"]
        assert len(outbox) == 1
        row = outbox[0]
        # AC1 四要素：目标系统、请求体 diff、幂等键、回滚说明
        assert row["target_system"] == "markdown-case-repo"
        assert row["request_body"]["markdown"]
        assert row["idempotency_key"].startswith("case-writeback:")
        assert row["rollback_note"]


# ── 写策略：只能收紧不能越权放开 ────────────────────────────────────────────

def test_write_policy_tightening_only(container):
    with _client(container) as client:
        view = client.get("/api/approvals/write-policy").json()
        assert view == {
            "profile_external_write_enabled": False,
            "runtime_disabled": False,
            "effective_writes_enabled": False,
        }
        # profile 关着：请求放开 → 403
        resp = client.post("/api/approvals/write-policy",
                           json={"runtime_disabled": False})
        assert resp.status_code == 403
        assert resp.json()["error"]["type"] == "PolicyDeniedError"
        # 收紧始终允许
        ok = client.post("/api/approvals/write-policy",
                         json={"runtime_disabled": True})
        assert ok.status_code == 200 and ok.json()["effective_writes_enabled"] is False

        # 模拟 profile 开启（测试接缝）：运行期可停用也可恢复
        container.write_policy.profile_enabled = True
        client.post("/api/approvals/write-policy",
                    json={"runtime_disabled": True})
        assert client.get("/api/approvals/write-policy").json()[
            "effective_writes_enabled"] is False
        client.post("/api/approvals/write-policy",
                    json={"runtime_disabled": False})
        assert client.get("/api/approvals/write-policy").json()[
            "effective_writes_enabled"] is True


# ── 派发：先 blocked 后 confirmed 的完整链路 ────────────────────────────────

def test_dispatch_blocked_then_confirmed_full_flow(container):
    with _client(container) as client:
        urn = _seed_case_with_suggestion(container.get("store"))
        decide = client.post(_decide_url(_CASE_URN), json={
            "decision": "approved", "reviewer": "expert-zhang",
            "items": [{"suggestion_urn": urn, "action": "adopt"}]})
        key = decide.json()["idempotency_key"]

        # profile 默认关写：派发被 kill switch 拦截，pending 不烧 attempts
        blocked = client.post("/api/approvals/outbox/dispatch").json()
        assert blocked["status"] == "succeeded"
        assert blocked["blocked"] == 1 and blocked["confirmed"] == 0
        row = container.get("store").get_outbox_record(key)
        assert row.status.value == "pending" and row.attempts == 0

        # 开启外写后同一 outbox 转正，文件落在独立写根（语料仓零接触）
        container.write_policy.profile_enabled = True
        confirmed = client.post("/api/approvals/outbox/dispatch").json()
        assert confirmed["confirmed"] == 1 and confirmed["failed"] == 0
        assert set(confirmed) == {
            "status", "scanned", "dispatched", "confirmed", "blocked",
            "failed", "skipped_unsupported", "results"}
        row = container.get("store").get_outbox_record(key)
        assert row.status.value == "confirmed" and row.external_ref
        write_root = container.data_dir / "case-writeback"
        assert (write_root / "cases/synthetic-cases/TC-N2.md").exists()

        # 再派发：队列已空，零扫描
        drained = client.post("/api/approvals/outbox/dispatch").json()
        assert drained["scanned"] == 0
