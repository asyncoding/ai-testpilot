"""L3 规则资产服务（docs/08 FR-KNO-002，S2.6a）。

core 纯函数管版本语义（单调版本、kind 不可变、同内容幂等、diff），本服务只做
取数与落账：

- ``register_asset_revision``：读历史版本链 → core 追加 → store 不可变 INSERT；
- ``get_asset_diff``：取两版本交 core 现算字段级/行级 diff（diff 不冗余落库）；
- ``promote_preferences_to_fewshot``：S2.5 人审 modify 偏好样本回流为 few_shot
  资产（FR-KNO-002「采纳 diff 回流 L3」），空样本由 core fail-closed 拒入。
"""

from __future__ import annotations

from typing import Any

from testpilot_core.errors import NotFoundError
from testpilot_core.rule_assets import (
    AssetDiff,
    RuleAssetRevision,
    append_revision,
    build_fewshot_from_preferences,
    diff_revisions,
)

# 偏好回流的默认资产 ID；调用方可覆盖（多评审队列可分资产）
DEFAULT_FEWSHOT_ASSET_ID = "tp.fewshot:oracle-review"
DEFAULT_FEWSHOT_NAME = "oracle 人审偏好 few-shot"


def register_asset_revision(
    container: Any,
    *,
    asset_id: str,
    kind: str,
    name: str,
    content: dict[str, Any],
    author: str = "",
    note: str = "",
) -> dict[str, Any]:
    """登记一个资产新版本；同内容重放 inserted=False 且版本号不增长。"""
    store = container.get("store")
    history = store.list_asset_revisions(asset_id)
    revision: RuleAssetRevision = append_revision(
        history,
        asset_id=asset_id,
        kind=kind,
        name=name,
        content=content,
        author=author,
        note=note,
    )
    inserted = store.append_asset_revision(revision)
    return {"revision": revision, "inserted": inserted}


def get_asset_diff(
    container: Any, *, asset_id: str, from_version: int, to_version: int
) -> AssetDiff:
    """两版本 diff；任一版本不存在 → NotFoundError（HTTP 层映射 404）。"""
    store = container.get("store")
    old = store.get_asset_revision(asset_id, from_version)
    new = store.get_asset_revision(asset_id, to_version)
    if old is None or new is None:
        raise NotFoundError(
            f"资产版本不存在: {asset_id} v{from_version}/v{to_version}"
        )
    return diff_revisions(old, new)


def promote_preferences_to_fewshot(
    container: Any,
    *,
    asset_id: str = DEFAULT_FEWSHOT_ASSET_ID,
    name: str = DEFAULT_FEWSHOT_NAME,
    author: str = "",
    note: str = "",
) -> dict[str, Any]:
    """把全量偏好样本回流成 few_shot 资产新版本。

    报告键稳定可钉：asset_id/version/content_hash/inserted/samples_consumed/
    source_sample_ids。无样本时 core 抛 AssetValidationError（422），不造空资产。
    """
    store = container.get("store")
    samples = store.list_preference_samples()
    content = build_fewshot_from_preferences(samples, author=author, note=note)
    result = register_asset_revision(
        container,
        asset_id=asset_id,
        kind="few_shot",
        name=name,
        content=content,
        author=author,
        note=note,
    )
    revision = result["revision"]
    return {
        "asset_id": revision.asset_id,
        "version": revision.version,
        "content_hash": revision.content_hash,
        "inserted": result["inserted"],
        "samples_consumed": content["entry_count"],
        "source_sample_ids": content["source_sample_ids"],
    }
