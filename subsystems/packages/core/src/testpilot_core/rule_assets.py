"""L3 规则资产版本化纯逻辑（docs/08 FR-KNO-002，S2.6a）。

零 IO、零三方：术语表 / prompt 模板 / few-shot / golden set / 工具白名单作为
**可版本化资产**存储，每次改动产生不可变新版本并保留可回放 diff（docs/08
§6 四层知识架构的 L3）。纪律与 S2.1~S2.5 一致——纯函数先把账算清楚，持久化
由 workers/store 负责：

- ``RuleAssetRevision``：一条不可变版本（asset_id + version 构成自然键），
  内容 hash 走 canonical JSON（sort_keys、紧凑分隔符），同内容字节级稳定；
- ``append_revision``：版本号单调递增；**同 asset_id 的 kind 不可变**
  （拼写漂移/串档 fail-closed）；与最新版内容相同则幂等返回旧版、不增生；
- ``diff_revisions``：递归叶子级字段 diff（dotted path），字符串叶子附
  stdlib difflib unified diff，改动留痕可逐行回放；
- ``build_fewshot_from_preferences``：S2.5 人审 modify 产生的偏好样本
  （original/revised/diff）回流为 few_shot 资产内容，每条带来源 sample_id
  可追溯，空样本拒入（不制造空资产）。

资产内容一律 dict（结构化留 diff）；整块文本类资产放 ``{"template": "..."}``。
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
from datetime import datetime
from typing import Any, Literal, Sequence

from pydantic import BaseModel, Field

from .models import PreferenceSample, utcnow

AssetKind = Literal["prompt", "few_shot", "golden", "glossary", "tool_whitelist"]

# asset_id 同时用作 URL 路径段：字母数字开头，仅允许 . _ - : 分隔符
_ASSET_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\-]{0,127}$")
_ASSET_KINDS: frozenset[str] = frozenset(
    {"prompt", "few_shot", "golden", "glossary", "tool_whitelist"}
)


class AssetValidationError(ValueError):
    """资产语义非法（坏 ID、kind 串档、空内容、空样本回流等），HTTP 层映射 422。"""


class RuleAssetRevision(BaseModel):
    """L3 资产的一个不可变版本；只允许新增，不允许改写/删除。"""

    asset_id: str
    version: int = Field(ge=1)
    kind: AssetKind
    name: str
    content: dict[str, Any]
    content_hash: str
    author: str = ""
    note: str = ""
    created_at: datetime = Field(default_factory=utcnow)


class AssetFieldChange(BaseModel):
    """单个叶子字段的改动；字符串叶子附 unified diff，其余给 canonical 前后值。"""

    path: str
    before: str
    after: str
    unified_diff: list[str] = Field(default_factory=list)


class AssetDiff(BaseModel):
    """两个版本之间的完整留痕（docs/08：改动留 diff）。"""

    asset_id: str
    kind: str
    from_version: int
    to_version: int
    added: list[str] = Field(default_factory=list)
    removed: list[str] = Field(default_factory=list)
    changed: list[AssetFieldChange] = Field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.added or self.removed or self.changed)


def canonical_json(content: dict[str, Any]) -> str:
    """跨进程稳定的 canonical 序列化：sort_keys、不转义中文、紧凑分隔符。"""
    return json.dumps(content, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def hash_content(content: dict[str, Any]) -> str:
    """资产内容内容寻址 hash（sha256 hex）；同内容恒等 hash。"""
    return hashlib.sha256(canonical_json(content).encode("utf-8")).hexdigest()


def validate_asset_id(asset_id: str) -> None:
    if not isinstance(asset_id, str) or not _ASSET_ID_RE.match(asset_id):
        raise AssetValidationError(
            f"非法 asset_id（需字母数字开头、≤128 字符、仅含 ._-: 分隔符）: {asset_id!r}"
        )


def _validate_kind(kind: str) -> AssetKind:
    if kind not in _ASSET_KINDS:
        raise AssetValidationError(
            f"未知资产类型 {kind!r}，允许: {sorted(_ASSET_KINDS)}"
        )
    return kind  # type: ignore[return-value]


def _flatten(content: dict[str, Any], prefix: str = "") -> dict[str, str]:
    """递归拍平为 dotted-path 叶子；复合叶子转 canonical JSON 字符串比较。"""
    flat: dict[str, str] = {}
    for key in sorted(content):
        path = f"{prefix}.{key}" if prefix else str(key)
        value = content[key]
        if isinstance(value, dict):
            if value:
                flat.update(_flatten(value, path))
            else:
                flat[path] = "{}"
        elif isinstance(value, str):
            flat[path] = value
        elif value is None or isinstance(value, (list, int, float, bool)):
            flat[path] = canonical_json(value)
        else:
            flat[path] = str(value)
    return flat


def _get_path(content: dict[str, Any], path: str) -> Any:
    """按 dotted path 取原始叶子（供 diff 判断是否字符串叶子）。"""
    current: Any = content
    for part in path.split("."):
        current = current[part]
    return current


def diff_revisions(old: RuleAssetRevision, new: RuleAssetRevision) -> AssetDiff:
    """两版本字段级 diff。asset_id 不一致拒入（版本对比只能在同一资产内）。"""
    if old.asset_id != new.asset_id:
        raise AssetValidationError(
            f"不能跨资产 diff: {old.asset_id!r} vs {new.asset_id!r}"
        )
    if old.version >= new.version:
        raise AssetValidationError(
            f"diff 版本方向反了: from={old.version} to={new.version}"
        )
    old_flat = _flatten(old.content)
    new_flat = _flatten(new.content)
    old_keys, new_keys = set(old_flat), set(new_flat)

    changes: list[AssetFieldChange] = []
    for path in sorted(old_keys & new_keys):
        before, after = old_flat[path], new_flat[path]
        if before == after:
            continue
        unified: list[str] = []
        # 仅对字符串叶子给行级 unified diff（canonical JSON 复合值给前后快照即可）
        if isinstance(_get_path(old.content, path), str) and isinstance(
            _get_path(new.content, path), str
        ):
            unified = list(
                difflib.unified_diff(
                    before.splitlines(),
                    after.splitlines(),
                    fromfile=f"{old.asset_id}#v{old.version}:{path}",
                    tofile=f"{new.asset_id}#v{new.version}:{path}",
                    lineterm="",
                )
            )
        changes.append(
            AssetFieldChange(path=path, before=before, after=after, unified_diff=unified)
        )

    return AssetDiff(
        asset_id=old.asset_id,
        kind=new.kind,
        from_version=old.version,
        to_version=new.version,
        added=sorted(new_keys - old_keys),
        removed=sorted(old_keys - new_keys),
        changed=changes,
    )


def _latest(history: Sequence[RuleAssetRevision]) -> RuleAssetRevision | None:
    return max(history, key=lambda r: r.version) if history else None


def append_revision(
    history: Sequence[RuleAssetRevision],
    *,
    asset_id: str,
    kind: str,
    name: str,
    content: dict[str, Any],
    author: str = "",
    note: str = "",
) -> RuleAssetRevision:
    """在版本链上追加新版本。

    - 空 history → v1 首版；
    - 同 asset_id 后续版本 kind 必须一致（kind 是资产身份，不可变）；
    - 内容与最新版 hash 相同 → 幂等返回最新版，**不新增版本**（重放/重复提交
      不污染版本链）；
    - 其余情况 version = max(version)+1。
    """
    validate_asset_id(asset_id)
    checked_kind = _validate_kind(kind)
    if not isinstance(content, dict) or not content:
        raise AssetValidationError("资产内容必须是非空 dict")
    if not name or not name.strip():
        raise AssetValidationError("资产名称不能为空")

    latest = _latest(history)
    if latest is not None:
        if latest.kind != checked_kind:
            raise AssetValidationError(
                f"资产 {asset_id} 类型不可变：历史为 {latest.kind!r}，"
                f"本次提交 {checked_kind!r}"
            )
        if latest.content_hash == hash_content(content):
            return latest
        version = latest.version + 1
    else:
        version = 1

    return RuleAssetRevision(
        asset_id=asset_id,
        version=version,
        kind=checked_kind,
        name=name.strip(),
        content=content,
        content_hash=hash_content(content),
        author=author,
        note=note,
    )


def build_fewshot_from_preferences(
    samples: Sequence[PreferenceSample], *, author: str = "", note: str = ""
) -> dict[str, Any]:
    """人审 modify 偏好样本 → few_shot 资产内容（FR-KNO-002：采纳 diff 回流 L3）。

    每条 entry 完整保留 original/revised/diff 与来源 sample_id/用例/建议 URN，
    模型侧 few-shot 组装时可逐案追溯到人审决定。空样本拒入（不制造空资产）；
    sample_id 去重防御（store 侧本已 UNIQUE）。content_seal 覆盖除 seal 外的
    canonical 内容，供消费方校验 few-shot 包未被篡改。
    """
    if not samples:
        raise AssetValidationError("没有偏好样本可回流：few-shot 资产不允许空内容")

    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for sample in samples:
        if sample.sample_id in seen:
            continue
        seen.add(sample.sample_id)
        entries.append(
            {
                "sample_id": sample.sample_id,
                "case_urn": sample.case_urn,
                "suggestion_urn": sample.suggestion_urn,
                "reviewer": sample.reviewer,
                "reason": sample.reason,
                "original": sample.original,
                "revised": sample.revised,
                "diff": sample.diff,
                "created_at": sample.created_at.isoformat(),
            }
        )

    content: dict[str, Any] = {
        "format_version": 1,
        "entry_count": len(entries),
        "source_sample_ids": sorted(seen),
        "entries": entries,
    }
    content["content_seal"] = hash_content(content)
    return content
