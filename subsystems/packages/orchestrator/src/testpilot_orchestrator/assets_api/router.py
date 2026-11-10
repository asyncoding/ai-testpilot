"""/api/assets/* 路由：L3 规则资产版本链与偏好回流（S2.6a FR-KNO-002）。

薄 HTTP 包装原则同 qa_api/approval_api：版本语义在 core.rule_assets，落账在
workers.asset_service，本层只做 HTTP 模型转换与容器取接。资产只增不改，
没有任何 delete/update 端点。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request

from testpilot_core.errors import NotFoundError
from testpilot_workers.asset_service import (
    DEFAULT_FEWSHOT_ASSET_ID,
    DEFAULT_FEWSHOT_NAME,
    get_asset_diff,
    promote_preferences_to_fewshot,
    register_asset_revision,
)

from .schemas import AssetRevisionHttp, PromoteFewshotHttp

assets_router = APIRouter(prefix="/api/assets", tags=["assets"])


@assets_router.get("")
async def list_assets(request: Request) -> dict[str, Any]:
    """资产清单：每个 asset_id 只返回最新版本。"""
    store = request.app.state.container.get("store")
    items = store.list_latest_assets()
    return {"assets": [r.model_dump(mode="json") for r in items]}


@assets_router.post("/fewshot/promote")
async def promote_fewshot(body: PromoteFewshotHttp, request: Request) -> dict[str, Any]:
    """人审 modify 偏好样本回流为 few_shot 资产新版本（采纳 diff 回流 L3）。"""
    container = request.app.state.container
    report = promote_preferences_to_fewshot(
        container,
        asset_id=body.asset_id or DEFAULT_FEWSHOT_ASSET_ID,
        name=body.name or DEFAULT_FEWSHOT_NAME,
        author=body.author,
        note=body.note,
    )
    return report


@assets_router.get("/{asset_id}/revisions")
async def list_revisions(asset_id: str, request: Request) -> dict[str, Any]:
    """单个资产的完整不可变版本链（按版本号升序）。"""
    store = request.app.state.container.get("store")
    revisions = store.list_asset_revisions(asset_id)
    return {
        "asset_id": asset_id,
        "revisions": [r.model_dump(mode="json") for r in revisions],
    }


@assets_router.get("/{asset_id}/revisions/{version}")
async def get_revision(asset_id: str, version: int, request: Request) -> dict[str, Any]:
    store = request.app.state.container.get("store")
    revision = store.get_asset_revision(asset_id, version)
    if revision is None:
        raise NotFoundError(f"资产版本不存在: {asset_id} v{version}")
    return revision.model_dump(mode="json")


@assets_router.get("/{asset_id}/diff")
async def revision_diff(
    asset_id: str,
    request: Request,
    from_version: int = Query(alias="from", ge=1),
    to_version: int = Query(alias="to", ge=1),
) -> dict[str, Any]:
    """两版本字段级/行级 diff；版本方向反了由 core 拒入（422）。"""
    container = request.app.state.container
    diff = get_asset_diff(
        container, asset_id=asset_id, from_version=from_version, to_version=to_version
    )
    return diff.model_dump(mode="json")


@assets_router.post("/{asset_id}/revisions")
async def append_asset(
    asset_id: str, body: AssetRevisionHttp, request: Request
) -> dict[str, Any]:
    """登记新版本；同内容重放幂等（inserted=false，版本号不增长）。"""
    container = request.app.state.container
    result = register_asset_revision(
        container,
        asset_id=asset_id,
        kind=body.kind,
        name=body.name,
        content=body.content,
        author=body.author,
        note=body.note,
    )
    return {
        "revision": result["revision"].model_dump(mode="json"),
        "inserted": result["inserted"],
    }
