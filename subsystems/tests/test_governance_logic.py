"""知识治理纯逻辑 hostless 测试（FR-UI-004，S4.5）。

零 IO：owner/权威等级编辑、审计历史只追加、reindex 治理存活与非法枚举回退。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from testpilot_core.enums import AuthorityLevel
from testpilot_core.governance import (
    GOVERNANCE_KEY,
    apply_governance,
    governance_owner,
    preserve_governance,
)
from testpilot_core.models import SourceDocument

_T0 = datetime(2026, 11, 11, 8, 0, tzinfo=timezone.utc)


def _doc(**over) -> SourceDocument:
    base = dict(
        urn="urn:testpilot:fs-docs:docs/kill-backup.md",
        source_system="fs-docs",
        uri="file://fixtures/docs/kill-backup.md",
        title="kill-backup",
        content_hash="h1",
    )
    base.update(over)
    return SourceDocument(**base)


def test_apply_owner_writes_governance_metadata_without_touching_input():
    doc = _doc()
    out = apply_governance(
        doc, owner="alice", edited_by="web-console", edited_at=_T0
    )
    assert governance_owner(out) == "alice"
    g = out.metadata_[GOVERNANCE_KEY]
    assert g["updated_by"] == "web-console"
    assert g["authority_level"] is None  # 未覆盖权威等级，跟随适配器
    assert g["history"] == [
        {"at": _T0.isoformat(), "by": "web-console",
         "owner": "alice", "authority_level": None}
    ]
    # 不可变纪律：入参不被原地修改
    assert GOVERNANCE_KEY not in doc.metadata_


def test_apply_authority_override_updates_enum_and_column_value():
    doc = _doc(authority_level=AuthorityLevel.L2_EXPERIENCE)
    out = apply_governance(
        doc,
        authority_level=AuthorityLevel.L1_AUTHORITATIVE,
        edited_by="bob",
        edited_at=_T0,
    )
    assert out.authority_level is AuthorityLevel.L1_AUTHORITATIVE
    assert out.metadata_[GOVERNANCE_KEY]["authority_level"] == "L1_authoritative"


def test_second_edit_keeps_owner_and_appends_history():
    once = apply_governance(
        _doc(), owner="alice", edited_by="web-console", edited_at=_T0
    )
    twice = apply_governance(
        once,
        authority_level=AuthorityLevel.L3_RULE,
        edited_by="carol",
        edited_at=_T0,
    )
    g = twice.metadata_[GOVERNANCE_KEY]
    assert g["owner"] == "alice"  # 只改权威等级，owner 不丢
    assert len(g["history"]) == 2
    assert g["history"][1]["by"] == "carol"


def test_empty_edit_and_blank_owner_fail_loudly():
    with pytest.raises(ValueError, match="至少提供一个"):
        apply_governance(_doc(), edited_by="x", edited_at=_T0)
    with pytest.raises(ValueError, match="owner 不允许空白"):
        apply_governance(
            _doc(), owner="   ", edited_by="x", edited_at=_T0
        )
    with pytest.raises(ValueError, match="edited_by"):
        apply_governance(
            _doc(), owner="a", edited_by=" ", edited_at=_T0
        )


def test_history_is_capped():
    doc = _doc()
    for i in range(25):
        doc = apply_governance(
            doc, owner=f"u{i}", edited_by="cron", edited_at=_T0
        )
    assert len(doc.metadata_[GOVERNANCE_KEY]["history"]) == 20
    # 保留的是最近 20 条（u5..u24）
    assert doc.metadata_[GOVERNANCE_KEY]["history"][0]["owner"] == "u5"


def test_preserve_governance_carries_owner_and_authority_through_reindex():
    governed = apply_governance(
        _doc(authority_level=AuthorityLevel.L2_EXPERIENCE),
        owner="alice",
        authority_level=AuthorityLevel.L1_AUTHORITATIVE,
        edited_by="web-console",
        edited_at=_T0,
    )
    # reindex 产物：适配器按 profile 给回默认 L2，hash 变了，无治理字段
    incoming = _doc(content_hash="h2", authority_level=AuthorityLevel.L2_EXPERIENCE)
    preserved = preserve_governance(incoming, governed)

    assert preserved.content_hash == "h2"  # 新内容不被回退
    assert governance_owner(preserved) == "alice"
    assert preserved.authority_level is AuthorityLevel.L1_AUTHORITATIVE
    assert preserved.metadata_[GOVERNANCE_KEY]["history"] == (
        governed.metadata_[GOVERNANCE_KEY]["history"]
    )


def test_preserve_without_governance_history_is_noop():
    incoming = _doc(content_hash="h2")
    assert preserve_governance(incoming, _doc()) is incoming
    assert preserve_governance(incoming, None) is incoming


def test_preserve_ignores_unknown_legacy_enum_but_keeps_audit():
    governed = apply_governance(
        _doc(), owner="alice", edited_by="web-console", edited_at=_T0
    )
    # 模拟旧版本写入过当前闭集之外的权威值
    governed.metadata_[GOVERNANCE_KEY]["authority_level"] = "L9_legacy"
    incoming = _doc(content_hash="h2", authority_level=AuthorityLevel.L2_EXPERIENCE)
    preserved = preserve_governance(incoming, governed)
    # 非法枚举不写列，回退适配器默认；审计记录仍保留可追查
    assert preserved.authority_level is AuthorityLevel.L2_EXPERIENCE
    assert preserved.metadata_[GOVERNANCE_KEY]["authority_level"] == "L9_legacy"
