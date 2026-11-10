"""S2.6a schema v13 存储测试（FR-KNO-002 L3 规则资产版本链持久化口径）。

覆盖：
- v12→v13 老库升级：rule_assets 经 CREATE TABLE IF NOT EXISTS 补建，
  既有表数据无损，升级后立即可写版本；
- 不可变版本只 INSERT、同内容幂等 DO NOTHING、版本链/最新版查询口径。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from testpilot_adapters.store import bootstrap_store
from testpilot_core import SCHEMA_VERSION
from testpilot_core.rule_assets import RuleAssetRevision, append_revision, hash_content

_NOW = datetime(2026, 11, 10, 2, 30, tzinfo=timezone.utc)


def _table_names(conn) -> set[str]:
    return {
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }


def _revision(asset_id: str, version: int, content: dict, *, kind: str = "prompt",
              name: str = "测试资产") -> RuleAssetRevision:
    return RuleAssetRevision(
        asset_id=asset_id, version=version, kind=kind, name=name,
        content=content, content_hash=hash_content(content),
        author="li", note=f"v{version}", created_at=_NOW,
    )


# ── v12→v13 老库迁移 ───────────────────────────────────────────────────────

def test_v12_store_upgrades_with_rule_assets_table(tmp_path):
    store = bootstrap_store(tmp_path)
    try:
        # v13 新库直接有表
        assert "rule_assets" in _table_names(store.conn)
        # 模拟 v12 老库：拆表并把版本号打回 12
        store.conn.execute("DROP TABLE rule_assets")
        store.conn.execute(
            "UPDATE meta SET value='12' WHERE key='schema_version'")
        store.conn.commit()
        assert "rule_assets" not in _table_names(store.conn)
    finally:
        store.close()

    store2 = bootstrap_store(tmp_path)
    try:
        assert store2.get_meta("schema_version") == str(SCHEMA_VERSION) == "15"
        assert "rule_assets" in _table_names(store2.conn)
        # 升级后立即可写，且 unique 约束就位
        inserted = store2.append_asset_revision(
            _revision("tp.prompt:qa", 1, {"template": "v1"}))
        assert inserted is True
    finally:
        store2.close()


# ── 版本链持久化与幂等 ─────────────────────────────────────────────────────

def test_append_list_get_and_latest_roundtrip(tmp_path):
    store = bootstrap_store(tmp_path)
    try:
        r1 = _revision("tp.golden:core", 1, {"cases": ["q1"]}, kind="golden")
        r2 = _revision("tp.golden:core", 2, {"cases": ["q1", "q2"]}, kind="golden")
        other = _revision("tp.glossary:terms", 1, {"terms": {"闹钟": "alarm"}},
                          kind="glossary")
        assert store.append_asset_revision(r1) is True
        assert store.append_asset_revision(r2) is True
        assert store.append_asset_revision(other) is True

        chain = store.list_asset_revisions("tp.golden:core")
        assert [r.version for r in chain] == [1, 2]
        assert chain[0].content == {"cases": ["q1"]}
        assert chain[0].created_at == _NOW  # datetime 无损往返

        got = store.get_asset_revision("tp.golden:core", 2)
        assert got is not None and got.content_hash == r2.content_hash
        assert store.get_asset_revision("tp.golden:core", 99) is None

        latest = {r.asset_id: r.version for r in store.list_latest_assets()}
        assert latest == {"tp.glossary:terms": 1, "tp.golden:core": 2}
    finally:
        store.close()


def test_same_content_hash_insert_is_idempotent_noop(tmp_path):
    store = bootstrap_store(tmp_path)
    try:
        r1 = _revision("tp.prompt:qa", 1, {"template": "同内容"})
        assert store.append_asset_revision(r1) is True
        # 同 asset_id + 同 content_hash（即便 version 字段不同）被 DO NOTHING 拦截
        duplicate = _revision("tp.prompt:qa", 2, {"template": "同内容"})
        assert store.append_asset_revision(duplicate) is False
        rows = store.list_asset_revisions("tp.prompt:qa")
        assert len(rows) == 1 and rows[0].version == 1
    finally:
        store.close()


def test_core_append_then_store_roundtrip_is_stable(tmp_path):
    """core 版本链计算与 store 幂等串起来：重复提交同一内容不增生版本。"""
    store = bootstrap_store(tmp_path)
    try:
        history: list[RuleAssetRevision] = []
        for content in ({"v": 1}, {"v": 2}, {"v": 2}):  # 第三次与最新版同内容
            rev = append_revision(
                history, asset_id="tp.fewshot:oracle", kind="few_shot",
                name="oracle 偏好", content=content)
            store.append_asset_revision(rev)
            history = store.list_asset_revisions("tp.fewshot:oracle")
        versions = [r.version for r in history]
        assert versions == [1, 2]  # 同内容第三次幂等，不出现 v3
    finally:
        store.close()
