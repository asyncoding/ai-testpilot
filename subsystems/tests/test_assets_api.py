"""S2.6a L3 资产 HTTP API 端到端测试（TestClient + 注入容器，FR-KNO-002）。

hermetic：offline-demo profile + tmp data_dir，不读环境、不碰真实 offline 库；
lifespan 退出自动关 store。覆盖六端点 + 422/404 映射 + 偏好回流全链路。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from testpilot_core.models import PreferenceSample
from testpilot_orchestrator.app import create_app
from testpilot_orchestrator.config_loader import load_profile
from testpilot_orchestrator.factory import build_container

_SUBSYSTEMS = Path(__file__).resolve().parents[1]
_PROFILE = _SUBSYSTEMS / "config" / "profiles" / "offline-demo.yaml"
_ASSET_ID = "tp.prompt:qa-v1"
_NOW = datetime(2026, 11, 10, 3, 0, tzinfo=timezone.utc)


@pytest.fixture
def container(tmp_path):
    config = load_profile(_PROFILE)
    config.paths["data_dir"] = str(tmp_path / "data")
    return build_container(config, _SUBSYSTEMS)


def _client(container) -> TestClient:
    return TestClient(create_app(container=container))


def _rev_payload(content: dict, *, kind: str = "prompt", name: str = "问答 prompt") -> dict:
    return {"kind": kind, "name": name, "content": content, "author": "li", "note": "n"}


def _asset_url(asset_id: str) -> str:
    return f"/api/assets/{quote(asset_id, safe='')}"


# ── 版本链端点 ─────────────────────────────────────────────────────────────

def test_empty_catalog_and_full_version_lifecycle(container):
    with _client(container) as client:
        assert client.get("/api/assets").json() == {"assets": []}

        r1 = client.post(
            f"{_asset_url(_ASSET_ID)}/revisions",
            json=_rev_payload({"template": "旧文案", "owner": "li"}),
        )
        assert r1.status_code == 200
        body = r1.json()
        assert body["inserted"] is True
        assert body["revision"]["version"] == 1
        assert body["revision"]["content_hash"]

        r2 = client.post(
            f"{_asset_url(_ASSET_ID)}/revisions",
            json=_rev_payload({"template": "新文案", "owner": "wang"}),
        )
        assert r2.json()["revision"]["version"] == 2

        chain = client.get(f"{_asset_url(_ASSET_ID)}/revisions").json()
        assert [r["version"] for r in chain["revisions"]] == [1, 2]

        single = client.get(f"{_asset_url(_ASSET_ID)}/revisions/2")
        assert single.status_code == 200
        assert single.json()["content"]["template"] == "新文案"

        latest = client.get("/api/assets").json()["assets"]
        assert len(latest) == 1 and latest[0]["version"] == 2


def test_same_content_post_is_idempotent(container):
    with _client(container) as client:
        payload = _rev_payload({"template": "同内容"})
        first = client.post(f"{_asset_url(_ASSET_ID)}/revisions", json=payload).json()
        again = client.post(f"{_asset_url(_ASSET_ID)}/revisions", json=payload).json()
        assert first["inserted"] is True and again["inserted"] is False
        assert again["revision"]["version"] == 1


def test_kind_change_rejected_422_and_bad_id_422(container):
    with _client(container) as client:
        client.post(f"{_asset_url(_ASSET_ID)}/revisions",
                    json=_rev_payload({"v": 1}, kind="prompt"))
        resp = client.post(f"{_asset_url(_ASSET_ID)}/revisions",
                           json=_rev_payload({"v": 2}, kind="golden"))
        assert resp.status_code == 422
        assert resp.json()["error"]["type"] == "AssetValidationError"

        bad = client.post("/api/assets/bad%20id/revisions",
                          json=_rev_payload({"v": 1}))
        assert bad.status_code == 422


def test_native_request_body_validation_422(container):
    with _client(container) as client:
        # content 为空 dict、name 缺失：pydantic 原生 422
        resp = client.post(f"{_asset_url('tp.x:1')}/revisions",
                           json={"kind": "prompt", "content": {}})
        assert resp.status_code == 422


def test_diff_endpoint_and_error_mappings(container):
    with _client(container) as client:
        client.post(f"{_asset_url(_ASSET_ID)}/revisions",
                    json=_rev_payload({"template": "旧文案"}))
        client.post(f"{_asset_url(_ASSET_ID)}/revisions",
                    json=_rev_payload({"template": "新文案"}))

        diff = client.get(f"{_asset_url(_ASSET_ID)}/diff?from=1&to=2").json()
        assert diff["from_version"] == 1 and diff["to_version"] == 2
        changed = {c["path"]: c for c in diff["changed"]}
        assert "template" in changed
        assert any(l.startswith("-旧文案") for l in changed["template"]["unified_diff"])

        # 版本方向反了 → core 422
        reverse = client.get(f"{_asset_url(_ASSET_ID)}/diff?from=2&to=1")
        assert reverse.status_code == 422
        # 不存在的版本 → 404
        missing = client.get(f"{_asset_url(_ASSET_ID)}/diff?from=1&to=9")
        assert missing.status_code == 404
        assert client.get(f"{_asset_url(_ASSET_ID)}/revisions/9").status_code == 404


# ── 偏好回流 ───────────────────────────────────────────────────────────────

def _seed_preference(store, sample_id: str = "s-1") -> None:
    store.save_preference_sample(PreferenceSample(
        sample_id=sample_id,
        case_urn="urn:testpilot:fs-cases:test_case:c.md#TC-1",
        suggestion_urn=f"urn:testpilot:fs-cases:oracle_suggestion:{sample_id}",
        reviewer="qa-zhang",
        reason="预期需含响铃持续",
        original={"type": "state_assertion", "expectation": "页面展示提醒卡片"},
        revised={"type": "ui_assertion", "expectation": "前台全屏提醒并持续响铃"},
        diff={"expectation": {"from": "页面展示提醒卡片", "to": "前台全屏提醒并持续响铃"}},
        created_at=_NOW,
    ))


def test_promote_preferences_to_fewshot_asset(container):
    store = container.get("store")
    _seed_preference(store)
    with _client(container) as client:
        resp = client.post("/api/assets/fewshot/promote", json={"author": "li"})
        assert resp.status_code == 200
        report = resp.json()
        assert report["samples_consumed"] == 1
        assert report["version"] == 1 and report["inserted"] is True
        asset_id = report["asset_id"]

        revision = client.get(f"{_asset_url(asset_id)}/revisions/1").json()
        assert revision["kind"] == "few_shot"
        entry = revision["content"]["entries"][0]
        assert entry["sample_id"] == "s-1"
        assert entry["revised"]["expectation"] == "前台全屏提醒并持续响铃"
        assert revision["content"]["content_seal"]

        # 同批样本重放：内容 hash 不变 → 幂等不增生
        again = client.post("/api/assets/fewshot/promote", json={"author": "li"})
        assert again.json()["inserted"] is False
        assert again.json()["version"] == 1


def test_promote_without_samples_rejected_422(container):
    with _client(container) as client:
        resp = client.post("/api/assets/fewshot/promote", json={})
        assert resp.status_code == 422
        assert resp.json()["error"]["type"] == "AssetValidationError"
