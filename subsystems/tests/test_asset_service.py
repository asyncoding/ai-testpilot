"""S2.6a L3 资产服务 worker 层测试（FR-KNO-002：版本登记/diff/偏好回流）。

shim 容器只暴露 get("store")，状态全落 tmp SQLite，不起完整 profile。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from testpilot_adapters.store import bootstrap_store
from testpilot_core.errors import NotFoundError
from testpilot_core.models import PreferenceSample
from testpilot_core.rule_assets import AssetValidationError
from testpilot_workers.asset_service import (
    DEFAULT_FEWSHOT_ASSET_ID,
    get_asset_diff,
    promote_preferences_to_fewshot,
    register_asset_revision,
)

_NOW = datetime(2026, 11, 10, 2, 45, tzinfo=timezone.utc)


class _ShimContainer:
    def __init__(self, store):
        self._store = store

    def get(self, name):
        return self._store if name == "store" else None


@pytest.fixture
def container(tmp_path):
    store = bootstrap_store(tmp_path)
    try:
        yield _ShimContainer(store)
    finally:
        store.close()


def test_register_versions_and_idempotent_replay(container):
    r1 = register_asset_revision(
        container, asset_id="tp.glossary:terms", kind="glossary",
        name="术语表", content={"terms": {"闹钟": "alarm"}})
    assert r1["inserted"] is True and r1["revision"].version == 1

    r2 = register_asset_revision(
        container, asset_id="tp.glossary:terms", kind="glossary",
        name="术语表", content={"terms": {"闹钟": "alarm", "提醒": "reminder"}})
    assert r2["inserted"] is True and r2["revision"].version == 2

    # 同内容重放：core 判同 hash 原样返回 v2，store DO NOTHING
    again = register_asset_revision(
        container, asset_id="tp.glossary:terms", kind="glossary",
        name="术语表", content={"terms": {"闹钟": "alarm", "提醒": "reminder"}})
    assert again["inserted"] is False and again["revision"].version == 2


def test_kind_drift_rejected_before_store_write(container):
    register_asset_revision(
        container, asset_id="a", kind="prompt", name="n", content={"v": 1})
    with pytest.raises(AssetValidationError):
        register_asset_revision(
            container, asset_id="a", kind="golden", name="n", content={"v": 2})
    # 被拒后版本链仍只有 v1
    store = container.get("store")
    assert [r.version for r in store.list_asset_revisions("a")] == [1]


def test_get_asset_diff_happy_and_not_found(container):
    register_asset_revision(
        container, asset_id="a", kind="tool_whitelist",
        name="白名单", content={"tools": ["x"]})
    register_asset_revision(
        container, asset_id="a", kind="tool_whitelist",
        name="白名单", content={"tools": ["x", "y"]})
    diff = get_asset_diff(container, asset_id="a", from_version=1, to_version=2)
    assert diff.changed[0].path == "tools"

    with pytest.raises(NotFoundError):
        get_asset_diff(container, asset_id="a", from_version=1, to_version=9)
    with pytest.raises(NotFoundError):
        get_asset_diff(container, asset_id="missing", from_version=1, to_version=2)


def test_promote_preferences_full_flow_and_idempotent(container):
    store = container.get("store")
    store.save_preference_sample(PreferenceSample(
        sample_id="s-1",
        case_urn="urn:testpilot:fs-cases:test_case:c.md#TC-1",
        suggestion_urn="urn:testpilot:fs-cases:oracle_suggestion:s-1",
        reviewer="qa-zhang", reason="补响铃",
        original={"expectation": "旧"}, revised={"expectation": "新"},
        diff={"expectation": {"from": "旧", "to": "新"}},
        created_at=_NOW,
    ))

    report = promote_preferences_to_fewshot(container, author="li")
    assert report["asset_id"] == DEFAULT_FEWSHOT_ASSET_ID
    assert report["version"] == 1
    assert report["inserted"] is True
    assert report["samples_consumed"] == 1
    assert report["source_sample_ids"] == ["s-1"]
    assert len(report["content_hash"]) == 64  # sha256 hex

    # 回流产物确实落在 few_shot 资产版本链上
    revisions = store.list_asset_revisions(DEFAULT_FEWSHOT_ASSET_ID)
    assert len(revisions) == 1 and revisions[0].kind == "few_shot"
    assert revisions[0].content["entries"][0]["sample_id"] == "s-1"

    # 同批样本二次回流：内容不变 → 幂等
    again = promote_preferences_to_fewshot(container, author="li")
    assert again["inserted"] is False and again["version"] == 1


def test_promote_without_samples_raises(container):
    with pytest.raises(AssetValidationError):
        promote_preferences_to_fewshot(container)
