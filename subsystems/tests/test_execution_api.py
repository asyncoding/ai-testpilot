"""S3.5b 执行 HTTP API 端到端测试（TestClient + 注入容器，FR-EX-001/002/007）。

hermetic：offline-demo profile + tmp data_dir，不读环境、不碰真实 offline 库、
不起真实端口（TestClient 进程内调用）。覆盖：列表/提交幂等/详情（含归因结论）/
取消/幽灵 404/配额 403/坏体 422，异常映射复用 qa_api/errors。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from testpilot_orchestrator.app import create_app
from testpilot_orchestrator.config_loader import load_profile
from testpilot_orchestrator.factory import build_container
from testpilot_workers import execution_service as svc

_SUBSYSTEMS = Path(__file__).resolve().parents[1]
_PROFILE = _SUBSYSTEMS / "config" / "profiles" / "offline-demo.yaml"


@pytest.fixture
def container(tmp_path):
    config = load_profile(_PROFILE)
    config.paths["data_dir"] = str(tmp_path / "data")
    yield build_container(config, _SUBSYSTEMS)


def _client(container) -> TestClient:
    return TestClient(create_app(container=container))


def _body(idem: str = "idem-api-1", **over) -> dict:
    payload = {
        "request_id": "req-api-1",
        "idempotency_key": idem,
        "project": "demo",
        "commit_sha": "cafe1234",
        # recorded 夹具 xcode_hostless 内含失败用例，可验归因结论随详情返回
        "selector": {
            "test_ids": ["AlarmKitSpikeTests"],
            "fixture_hint": "xcode_hostless",
        },
    }
    payload.update(over)
    return payload


def test_empty_run_catalog(container):
    with _client(container) as client:
        assert client.get("/api/runs").json() == {"runs": []}


def test_submit_is_idempotent_and_queued_detail_has_no_run_yet(container):
    with _client(container) as client:
        resp = client.post("/api/runs", json=_body())
        assert resp.status_code == 200
        first = resp.json()
        assert first["status"] == "queued" and first["replayed"] is False

        # 同 idempotency_key 重放：同一 run_id，不重复入队
        replay = client.post("/api/runs", json=_body()).json()
        assert replay["replayed"] is True
        assert replay["run_id"] == first["run_id"]

        listing = client.get("/api/runs").json()["runs"]
        assert len(listing) == 1
        assert listing[0]["verdict_total"] == 0  # 还没处理，无归因

        detail = client.get(f"/api/runs/{first['run_id']}").json()
        assert detail["status"] == "queued" and detail["run"] is None

        assert client.get("/api/runs/run-ghost").status_code == 404


async def test_processed_run_detail_returns_cases_and_verdict(container):
    with _client(container) as client:
        run_id = client.post("/api/runs", json=_body()).json()["run_id"]
        # worker 侧处理（测试里直接驱动服务，不经过 HTTP 长轮询）
        summary = await svc.run_once(container)
        assert summary["run_id"] == run_id

        detail = client.get(f"/api/runs/{run_id}").json()
        assert detail["status"] == "failed"
        run = detail["run"]
        assert run["run_id"] == run_id  # 账本身份覆盖适配器内部 id
        assert len(run["case_results"]) >= 1
        # Null 证据提供者：失败用例一律 needs_human，结论带规则版本戳
        verdicts = run["attributions"]
        assert len(verdicts) == 1
        assert verdicts[0]["category"] == "needs_human"
        assert verdicts[0]["rule_version"]

        listing = client.get("/api/runs").json()["runs"][0]
        assert listing["verdict_total"] == 1 and listing["needs_human"] == 1


def test_cancel_returns_marker_and_unknown_run_404(container):
    with _client(container) as client:
        run_id = client.post("/api/runs", json=_body("idem-cancel")).json()["run_id"]
        marked = client.post(f"/api/runs/{run_id}/cancel").json()
        assert marked["cancel_requested"] is True
        assert marked["run_id"] == run_id
        assert client.post("/api/runs/run-ghost/cancel").status_code == 404


def test_rerun_quota_exceeded_maps_to_403(container):
    # RerunQuotaExceededError 是 PolicyDeniedError 子类 → qa_api/errors 映射 403
    container.config.limits.daily_rerun_quota = 0
    with _client(container) as client:
        resp = client.post("/api/runs", json=_body(
            "idem-rerun-denied",
            request_id="req-rerun-denied",
            daily_budget_key="demo:2026-11-11"))
        assert resp.status_code == 403
        assert resp.json()["error"]["type"] == "RerunQuotaExceededError"
        # 拒入零幻影：账本里没有这条 run
        assert client.get("/api/runs").json() == {"runs": []}


def test_invalid_request_body_maps_to_422(container):
    with _client(container) as client:
        bad = _body()
        del bad["idempotency_key"]
        assert client.post("/api/runs", json=bad).status_code == 422


def test_list_filter_by_status_and_project(container):
    with _client(container) as client:
        client.post("/api/runs", json=_body("idem-a", request_id="req-a"))
        client.post("/api/runs", json=_body(
            "idem-b", request_id="req-b", project="other"))
        queued = client.get("/api/runs", params={"status": "queued"}).json()["runs"]
        assert len(queued) == 2
        other = client.get("/api/runs", params={"project": "other"}).json()["runs"]
        assert len(other) == 1 and other[0]["project"] == "other"
        assert client.get("/api/runs", params={"status": "passed"}).json() == {
            "runs": []}
