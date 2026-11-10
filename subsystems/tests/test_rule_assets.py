"""FR-KNO-002 L3 规则资产版本化纯逻辑 hostless 测试（S2.6a）。

不碰数据库/文件系统：canonical hash、版本链单调性、同内容幂等、kind 不可变、
字段级/行级 diff、偏好样本回流 few-shot 全部在内存验证。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from testpilot_core.models import PreferenceSample
from testpilot_core.rule_assets import (
    AssetValidationError,
    append_revision,
    build_fewshot_from_preferences,
    canonical_json,
    diff_revisions,
    hash_content,
    validate_asset_id,
)

_NOW = datetime(2026, 11, 10, 2, 0, tzinfo=timezone.utc)


def _sample(sid: str, *, case_urn: str = "urn:testpilot:fs-cases:test_case:c.md#TC-1") -> PreferenceSample:
    return PreferenceSample(
        sample_id=sid,
        case_urn=case_urn,
        suggestion_urn=f"urn:testpilot:fs-cases:oracle_suggestion:{sid}",
        reviewer="qa-zhang",
        reason="预期需含响铃持续",
        original={"type": "state_assertion", "expectation": "页面展示提醒卡片"},
        revised={"type": "ui_assertion", "expectation": "前台弹出全屏提醒并持续响铃"},
        diff={"expectation": {"from": "页面展示提醒卡片", "to": "前台弹出全屏提醒并持续响铃"}},
        created_at=_NOW,
    )


# ── canonical 序列化与 hash ────────────────────────────────────────────────

def test_canonical_json_key_order_and_chinese_stable():
    a = {"b": 1, "a": "中文"}
    b = {"a": "中文", "b": 1}
    assert canonical_json(a) == canonical_json(b)
    assert "中文" in canonical_json(a)  # ensure_ascii=False
    assert ", " not in canonical_json(a) and ": " not in canonical_json(a)


def test_hash_content_deterministic_and_distinct():
    assert hash_content({"x": 1}) == hash_content({"x": 1})
    assert hash_content({"x": 1}) != hash_content({"x": 2})


@pytest.mark.parametrize("bad", ["", " leading", "a/b", "a b", "中文id", "a" * 129])
def test_validate_asset_id_rejects(bad):
    with pytest.raises(AssetValidationError):
        validate_asset_id(bad)


@pytest.mark.parametrize("good", ["a", "tp-prompt.v1:fewshot", "A_1-2.3", "z" * 128])
def test_validate_asset_id_accepts(good):
    validate_asset_id(good)  # 不抛即过


# ── 版本链 ─────────────────────────────────────────────────────────────────

def test_first_revision_is_v1_with_content_hash():
    rev = append_revision(
        [], asset_id="tp.prompt:qa", kind="prompt", name="问答 prompt",
        content={"template": "你是测试助手"}, author="li",
    )
    assert rev.version == 1
    assert rev.content_hash == hash_content({"template": "你是测试助手"})
    assert rev.kind == "prompt"
    assert rev.created_at is not None


def test_revisions_are_monotonic_and_immutable_history():
    history = []
    r1 = append_revision(history, asset_id="a", kind="glossary", name="n", content={"v": 1})
    history = [r1]
    r2 = append_revision(history, asset_id="a", kind="glossary", name="n", content={"v": 2})
    assert r2.version == 2
    assert r1.version == 1 and r1.content == {"v": 1}  # 历史版本不被改写


def test_same_content_is_idempotent_no_new_version():
    r1 = append_revision([], asset_id="a", kind="golden", name="n", content={"v": 1})
    again = append_revision([r1], asset_id="a", kind="golden", name="n2", content={"v": 1})
    assert again.version == 1
    assert again is r1  # 原样返回，版本链不增生


def test_kind_is_immutable_per_asset():
    r1 = append_revision([], asset_id="a", kind="prompt", name="n", content={"v": 1})
    with pytest.raises(AssetValidationError, match="类型不可变"):
        append_revision([r1], asset_id="a", kind="golden", name="n", content={"v": 2})


def test_unknown_kind_empty_content_blank_name_rejected():
    with pytest.raises(AssetValidationError):
        append_revision([], asset_id="a", kind="unknown", name="n", content={"v": 1})
    with pytest.raises(AssetValidationError):
        append_revision([], asset_id="a", kind="prompt", name="n", content={})
    with pytest.raises(AssetValidationError):
        append_revision([], asset_id="a", kind="prompt", name="  ", content={"v": 1})


# ── diff ───────────────────────────────────────────────────────────────────

def test_diff_added_removed_changed_with_dotted_paths():
    r1 = append_revision([], asset_id="a", kind="prompt", name="n",
                         content={"template": "旧文案", "meta": {"owner": "li"}, "drop": 1})
    r2 = append_revision([r1], asset_id="a", kind="prompt", name="n",
                         content={"template": "新文案", "meta": {"owner": "wang"}, "added": [1, 2]})
    diff = diff_revisions(r1, r2)
    assert diff.added == ["added"]
    assert diff.removed == ["drop"]
    paths = {c.path for c in diff.changed}
    assert paths == {"template", "meta.owner"}
    template_change = next(c for c in diff.changed if c.path == "template")
    assert template_change.before == "旧文案" and template_change.after == "新文案"
    # 字符串叶子带行级 unified diff
    assert any(line.startswith("-旧文案") for line in template_change.unified_diff)
    assert any(line.startswith("+新文案") for line in template_change.unified_diff)


def test_diff_composite_leaf_has_canonical_snapshot_without_unified():
    r1 = append_revision([], asset_id="a", kind="tool_whitelist", name="n",
                         content={"tools": ["x"]})
    r2 = append_revision([r1], asset_id="a", kind="tool_whitelist", name="n",
                         content={"tools": ["x", "y"]})
    diff = diff_revisions(r1, r2)
    assert len(diff.changed) == 1
    assert diff.changed[0].unified_diff == []  # 复合值只给 canonical 前后快照
    assert diff.changed[0].before == '["x"]' and diff.changed[0].after == '["x","y"]'


def test_diff_rejects_cross_asset_and_reverse_direction():
    r1 = append_revision([], asset_id="a", kind="prompt", name="n", content={"v": 1})
    other = append_revision([], asset_id="b", kind="prompt", name="n", content={"v": 1})
    with pytest.raises(AssetValidationError, match="跨资产"):
        diff_revisions(r1, other)
    r2 = append_revision([r1], asset_id="a", kind="prompt", name="n", content={"v": 2})
    with pytest.raises(AssetValidationError, match="版本方向"):
        diff_revisions(r2, r1)


# ── 偏好回流 few-shot ──────────────────────────────────────────────────────

def test_fewshot_build_rejects_empty():
    with pytest.raises(AssetValidationError, match="空内容"):
        build_fewshot_from_preferences([])


def test_fewshot_build_entries_traceable_and_sealed():
    s1, s2 = _sample("s-1"), _sample("s-2", case_urn="urn:testpilot:fs-cases:test_case:c.md#TC-2")
    content = build_fewshot_from_preferences([s1, s2], author="li", note="S2.6a 回流")
    assert content["format_version"] == 1
    assert content["entry_count"] == 2
    assert content["source_sample_ids"] == ["s-1", "s-2"]
    entry = content["entries"][0]
    assert entry["sample_id"] == "s-1"
    assert entry["original"]["expectation"] == "页面展示提醒卡片"
    assert entry["revised"]["expectation"] == "前台弹出全屏提醒并持续响铃"
    assert entry["diff"]["expectation"]["to"].startswith("前台弹出")
    # seal 只覆盖除 seal 外的 canonical 内容
    without_seal = {k: v for k, v in content.items() if k != "content_seal"}
    assert content["content_seal"] == hash_content(without_seal)


def test_fewshot_build_dedupes_sample_ids():
    s1 = _sample("s-1")
    content = build_fewshot_from_preferences([s1, s1])
    assert content["entry_count"] == 1
    assert content["source_sample_ids"] == ["s-1"]


def test_fewshot_content_is_versionable_asset():
    # 回流产物能直接作为 few_shot 资产进版本链，且同批样本重跑幂等
    content = build_fewshot_from_preferences([_sample("s-1")])
    r1 = append_revision([], asset_id="tp.fewshot:oracle", kind="few_shot",
                         name="oracle 人审偏好", content=content)
    r_again = append_revision([r1], asset_id="tp.fewshot:oracle", kind="few_shot",
                              name="oracle 人审偏好", content=content)
    assert r_again.version == 1 and r_again is r1
