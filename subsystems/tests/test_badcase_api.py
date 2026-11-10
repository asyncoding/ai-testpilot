"""S2.6c bad case 闭环 HTTP API 端到端测试（TestClient + 注入容器，FR-EV-005）。

hermetic：offline-demo profile + tmp data_dir，不读环境、不碰真实 offline 库；
lifespan 退出自动关 store。覆盖六端点 + 422/404 映射 + 摄入→分诊→golden→回归全链路。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from testpilot_core.enums import FeedbackKind, FeedbackReason
from testpilot_core.models import EvalRun, EvalVersion, Feedback
from testpilot_orchestrator.app import create_app
from testpilot_orchestrator.config_loader import load_profile
from testpilot_orchestrator.factory import build_container

_SUBSYSTEMS = Path(__file__).resolve().parents[1]
_PROFILE = _SUBSYSTEMS / "config" / "profiles" / "offline-demo.yaml"


@pytest.fixture
def container(tmp_path):
    config = load_profile(_PROFILE)
    config.paths["data_dir"] = str(tmp_path / "data")
    return build_container(config, _SUBSYSTEMS)


def _client(container) -> TestClient:
    return TestClient(create_app(container=container))


def _seed_feedback(store, *, run_id: str = "run-1",
                   kind: FeedbackKind = FeedbackKind.NOT_USEFUL,
                   reason: FeedbackReason | None = FeedbackReason.STALE_REFERENCE,
                   detail: str = "引用旧版逻辑") -> int:
    store.insert_qa_run(record={
        "run_id": run_id,
        "question": f"问题-{run_id}", "status": "succeeded",
        "data_version": "d7", "model_version": "m3",
        "prompt_version": "p2", "params_version": "x1"})
    return store.add_feedback(Feedback(
        run_id=run_id, target_urn=f"urn:testpilot:qa:run:{run_id}",
        kind=kind, reason=reason, detail=detail, model_version="m3"))


def test_empty_catalog_stats_and_ingest_zero_report(container):
    with _client(container) as client:
        assert client.get("/api/bad-cases").json() == {"bad_cases": []}
        stats = client.get("/api/bad-cases/stats").json()
        assert stats["total"] == 0 and stats["closure_rate"] == 0.0
        report = client.post("/api/bad-cases/ingest-feedback").json()
        assert report["ingested"] == 0 and report["scanned"] == 0


def test_full_lifecycle_over_http_and_error_mapping(container):
    store = container.get("store")
    fb_id = _seed_feedback(store)
    with _client(container) as client:
        # 摄入：👍 混入一条验证跳过口径
        _seed_feedback(store, run_id="run-2", kind=FeedbackKind.USEFUL,
                       reason=None, detail="有用")
        report = client.post("/api/bad-cases/ingest-feedback").json()
        assert report["ingested"] == 1 and report["skipped_useful"] == 1
        # 重放幂等
        assert client.post("/api/bad-cases/ingest-feedback").json() == {
            **report, "scanned": 2, "ingested": 0,
            "skipped_useful": 1, "skipped_duplicate": 1,
            "bad_case_ids": []}

        case = client.get("/api/bad-cases").json()["bad_cases"][0]
        bc_id = case["bad_case_id"]
        assert case["feedback_id"] == str(fb_id)
        assert case["snapshot"]["data_version"] == "d7"
        assert client.get(
            "/api/bad-cases", params={"status": "open"}).json()["bad_cases"]
        assert client.get(
            "/api/bad-cases", params={"status": "fixed"}).json() == {
            "bad_cases": []}

        # 未知 ID → 404（NotFoundError 映射）
        r404 = client.post("/api/bad-cases/bc-missing000/triage",
                           json={"decision": "accept", "actor": "li"})
        assert r404.status_code == 404
        assert r404.json()["error"]["type"] == "NotFoundError"

        # 请求体枚举越界 → FastAPI 原生 422
        bad_body = client.post(
            f"/api/bad-cases/{bc_id}/triage",
            json={"decision": "maybe", "actor": "li"})
        assert bad_body.status_code == 422
        # actor 空白 → pydantic min_length 原生 422
        no_actor = client.post(
            f"/api/bad-cases/{bc_id}/triage",
            json={"decision": "accept", "actor": ""})
        assert no_actor.status_code == 422

        tri = client.post(f"/api/bad-cases/{bc_id}/triage", json={
            "decision": "accept", "actor": "qa-li",
            "reason": "factual_error", "note": "复现"}).json()
        assert tri["status"] == "accepted" and tri["triaged_by"] == "qa-li"

        promoted = client.post(f"/api/bad-cases/{bc_id}/promote",
                               json={"actor": "qa-li"}).json()
        assert promoted["bad_case"]["status"] == "promoted"
        golden_id = promoted["bad_case"]["golden_case_id"]
        assert promoted["eval_case"]["case_id"] == golden_id
        assert promoted["eval_case"]["kind"] == "bad_case"

        # promoted 再分诊：非法迁移 → BadCaseError 422
        re_triage = client.post(f"/api/bad-cases/{bc_id}/triage",
                                json={"decision": "reject", "actor": "li"})
        assert re_triage.status_code == 422
        assert re_triage.json()["error"]["type"] == "BadCaseError"

        # 幽灵回归 run → 404
        ghost = client.post(f"/api/bad-cases/{bc_id}/regression", json={
            "eval_run_id": "eval-ghost", "outcome": "pass", "actor": "ci"})
        assert ghost.status_code == 404

        store.add_eval_run(EvalRun(
            eval_run_id="eval-42",
            version=EvalVersion(data_version="d8", model_version="m4",
                                prompt_version="p3", params_version="x1")))
        failed = client.post(f"/api/bad-cases/{bc_id}/regression", json={
            "eval_run_id": "eval-42", "outcome": "fail",
            "actor": "ci-bot"}).json()
        assert failed["status"] == "regression_failed"
        assert failed["closed_at"] is None

        store.add_eval_run(EvalRun(
            eval_run_id="eval-43",
            version=EvalVersion(data_version="d9", model_version="m4",
                                prompt_version="p3", params_version="x1")))
        fixed = client.post(f"/api/bad-cases/{bc_id}/regression", json={
            "eval_run_id": "eval-43", "outcome": "pass",
            "actor": "ci-bot"}).json()
        assert fixed["status"] == "fixed" and fixed["closed_at"]

        stats = client.get("/api/bad-cases/stats").json()
        assert stats["total"] == 1 and stats["closed_count"] == 1
        assert stats["by_status"]["fixed"] == 1
        assert stats["closure_rate"] == 1.0


def test_reject_path_never_creates_golden(container):
    store = container.get("store")
    _seed_feedback(store)
    with _client(container) as client:
        client.post("/api/bad-cases/ingest-feedback")
        bc_id = client.get("/api/bad-cases").json()["bad_cases"][0]["bad_case_id"]
        rejected = client.post(f"/api/bad-cases/{bc_id}/triage",
                               json={"decision": "reject", "actor": "wang"}).json()
        assert rejected["status"] == "rejected" and rejected["closed_at"]
        # 终态转 golden → 422，题集零 bad_case 行
        r = client.post(f"/api/bad-cases/{bc_id}/promote",
                        json={"actor": "wang"})
        assert r.status_code == 422
        assert store.conn.execute(
            "SELECT COUNT(*) AS n FROM eval_cases WHERE kind='bad_case'"
        ).fetchone()["n"] == 0
