"""知识治理纯逻辑（FR-UI-004，S4.5）：权威等级与知识 owner 编辑、reindex 存活。

零 IO / 零三方：只操作 :class:`SourceDocument` 的 ``metadata`` 自由 dict
（schema v15 零变更，owner 与审计历史都承载在 raw_json 里）。

约定：治理信息放在 ``metadata["governance"]`` 子 dict，形状::

    {
      "owner": "alice",
      "authority_level": "L1_authoritative",   # 人工覆盖值；None=跟随适配器默认
      "updated_by": "web-console",
      "updated_at": "2026-11-11T08:00:00+00:00",
      "history": [ {编辑事件，只追加不抹除}, ... ],
    }

为什么需要 :func:`preserve_governance`：reindex（``replace_document_index``）
会用适配器产出的新文档整行覆盖 raw_json，人工治理数据若不回灌就被静默抹掉。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .enums import AuthorityLevel
from .models import SourceDocument

GOVERNANCE_KEY = "governance"
_HISTORY_LIMIT = 20  # 审计轨迹保留最近 N 条，防止 metadata 无限膨胀


def _governance(doc: SourceDocument) -> dict[str, Any]:
    raw = doc.metadata_.get(GOVERNANCE_KEY)
    return dict(raw) if isinstance(raw, dict) else {}


def apply_governance(
    doc: SourceDocument,
    *,
    owner: str | None = None,
    authority_level: AuthorityLevel | None = None,
    edited_by: str,
    edited_at: datetime,
) -> SourceDocument:
    """返回应用治理编辑后的新文档（不可变风格，不修改入参）。

    owner/authority_level 至少给一个；owner 空串响亮报错，不允许匿名空 owner
    把既有值悄悄清空。权威等级取闭集枚举，非法值在 pydantic 边界即拒。
    """
    if owner is None and authority_level is None:
        raise ValueError("owner 与 authority_level 至少提供一个，空编辑不允许落账")
    if owner is not None and not owner.strip():
        raise ValueError("owner 不允许空白字符串")
    if not edited_by.strip():
        raise ValueError("edited_by 不允许空白（审计必须留操作人）")

    previous = _governance(doc)
    history = list(previous.get("history", []))
    history.append(
        {
            "at": edited_at.isoformat(),
            "by": edited_by,
            "owner": owner,
            "authority_level": (
                authority_level.value if authority_level is not None else None
            ),
        }
    )
    governance = {
        "owner": owner if owner is not None else previous.get("owner", ""),
        # 人工覆盖一旦设定即固化；显式传 None 且本字段缺省时为 None（跟随适配器）
        "authority_level": (
            authority_level.value
            if authority_level is not None
            else previous.get("authority_level")
        ),
        "updated_by": edited_by,
        "updated_at": edited_at.isoformat(),
        "history": history[-_HISTORY_LIMIT:],
    }
    metadata = dict(doc.metadata_)
    metadata[GOVERNANCE_KEY] = governance
    updated = doc.model_copy(deep=True)
    updated.metadata_ = metadata
    if authority_level is not None:
        updated.authority_level = authority_level
    return updated


def preserve_governance(
    incoming: SourceDocument, previous: SourceDocument | None
) -> SourceDocument:
    """reindex 覆盖前回灌人工治理数据（owner/权威覆盖/审计历史）。

    适配器不知道也不应该知道人工治理字段；由 store 在整行替换前调本函数，
    把旧行 raw_json 里的治理子 dict 合并到新文档。无治理历史时原样返回。
    """
    if previous is None:
        return incoming
    governance = _governance(previous)
    if not governance:
        return incoming
    metadata = dict(incoming.metadata_)
    metadata[GOVERNANCE_KEY] = governance
    preserved = incoming.model_copy(deep=True)
    preserved.metadata_ = metadata
    override = governance.get("authority_level")
    if override:
        try:
            preserved.authority_level = AuthorityLevel(override)
        except ValueError:
            # 旧治理值不在当前闭集：保留审计记录但不强行写非法列，响亮程度由
            # 调用方日志承担；宁可回退适配器默认也不写脏枚举
            pass
    return preserved


def governance_owner(doc: SourceDocument) -> str:
    """页面/API 投影用：无治理记录时返回空串而非 KeyError。"""
    return _governance(doc).get("owner", "")
