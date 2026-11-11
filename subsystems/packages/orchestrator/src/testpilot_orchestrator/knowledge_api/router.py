"""/api/knowledge/* 路由：用例关系浏览、证据原文与影响面分析（S2.6b/S4.3）、
知识管理页盘点与治理编辑（S4.5 FR-UI-004）。

薄 HTTP 包装：派生语义在 core.case_relations / core.impact / core.governance，
落账在 workers.relation_building / workers.impact_service 与 store，本层只做
查询参数收口与容器取接。关系只读本 API；唯一写端点是文档治理（R1 内部写，
审计历史进 raw_json，不触任何外部系统）。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from testpilot_core.case_relations import (
    RELATION_SCOPE_URN,
    CaseRelationError,
    assemble_evidence_dossier,
)
from testpilot_core.errors import NotFoundError
from testpilot_core.governance import apply_governance
from testpilot_core.impact import ImpactRequest, PrecisionRequest
from testpilot_workers import impact_service as impact_svc

from .schemas import GovernanceEdit

knowledge_router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])

_LIST_LIMIT_CAP = 500


def _clamped_limit(limit: int) -> int:
    return max(1, min(limit, _LIST_LIMIT_CAP))

_KINDS = ("case_validates_requirement", "case_references_symbol")


@knowledge_router.get("/relations")
async def list_relations(
    request: Request,
    kind: str | None = Query(default=None),
    include_low: bool = Query(default=False),
) -> dict[str, Any]:
    """关系浏览口；低置信默认藏起（FR-ING-004：不面向用户，仅检索降权）。"""
    if kind is not None and kind not in _KINDS:
        raise CaseRelationError(
            f"未知关系类型 {kind!r}，允许: {list(_KINDS)}")
    store = request.app.state.container.get("store")
    relations = store.list_case_relations(kind=kind, include_low=include_low)
    return {
        "scope_urn": RELATION_SCOPE_URN,
        "kind": kind,
        "include_low": include_low,
        "low_confidence_threshold": 0.60,
        "relations": [r.model_dump() for r in relations],
    }


@knowledge_router.get("/relations/{relation_urn:path}/evidence")
async def relation_evidence(relation_urn: str, request: Request) -> dict[str, Any]:
    """单关系证据 dossier；解析不了的证据 URN 标 unresolved（不编造原文）。"""
    store = request.app.state.container.get("store")
    relation = store.get_case_relation(relation_urn)
    if relation is None:
        raise NotFoundError(f"用例关系不存在: {relation_urn}")
    rows = store.get_evidence_rows(relation.evidence_urns)
    dossier = assemble_evidence_dossier(relation, rows)
    return {
        "relation": relation.model_dump(),
        "dossier": dossier.model_dump(),
    }


# ── S4.3 影响面分析 / MR 摘要 / 精度回算（FR-FRS-003）──────────────────────


@knowledge_router.post("/impact")
async def analyze_impact_endpoint(
    body: ImpactRequest, request: Request
) -> dict[str, Any]:
    """影响链三分类 + diff 反推 MR 摘要（确定性，不读 commit message）。

    未解析符号如实挂在 ``unresolved_symbols``；工单适配器缺省时历史缺陷为空。
    """
    container = request.app.state.container
    analysis = await impact_svc.analyze_impact(
        container,
        base_commit=body.base_commit,
        head_commit=body.head_commit,
        requirement_urns=body.requirement_urns,
    )
    summary = await impact_svc.build_mr_summary(
        container,
        base_commit=body.base_commit,
        head_commit=body.head_commit,
    )
    return {
        "analysis": analysis.model_dump(mode="json"),
        "summary": summary.model_dump(mode="json"),
    }


@knowledge_router.post("/impact/precision")
async def impact_precision_endpoint(
    body: PrecisionRequest, request: Request
) -> dict[str, Any]:
    """以真实执行 run 回算推荐精度（docs/06 回归选择有效率，门限 0.8）。"""
    container = request.app.state.container
    analysis = await impact_svc.analyze_impact(
        container,
        base_commit=body.base_commit,
        head_commit=body.head_commit,
        requirement_urns=body.requirement_urns,
    )
    precision = await impact_svc.precision_from_run(
        container,
        analysis=analysis,
        run_id=body.run_id,
        full_suite_size=body.full_suite_size,
    )
    return {
        "analysis": analysis.model_dump(mode="json"),
        "precision": precision.model_dump(mode="json"),
    }


# ── S4.5 知识管理页（FR-UI-004：文档/切块/实体/质量分/stale 盘点 + 治理编辑）──────


@knowledge_router.get("/documents")
async def list_knowledge_documents(
    request: Request,
    branch: str | None = Query(default=None),
    limit: int = Query(default=200),
) -> dict[str, Any]:
    """文档盘点：切块/stale 计数、最高质量分、owner 与权威等级一屏齐。"""
    store = request.app.state.container.get("store")
    rows = store.list_knowledge_documents(branch=branch)
    return {"documents": rows[: _clamped_limit(limit)], "total": len(rows)}


@knowledge_router.get("/documents/{urn:path}/chunks")
async def list_document_chunks(
    urn: str, request: Request, limit: int = Query(default=200)
) -> dict[str, Any]:
    """单文档切块明细；文档不存在给 404，不返回匿名空数组掩盖拼写错误。"""
    store = request.app.state.container.get("store")
    if store.get_document(urn) is None:
        raise NotFoundError(f"知识文档不存在: {urn}")
    return {
        "doc_urn": urn,
        "chunks": store.list_doc_chunks(urn, limit=_clamped_limit(limit)),
    }


@knowledge_router.get("/stale")
async def list_stale(
    request: Request, limit: int = Query(default=100)
) -> dict[str, Any]:
    """stale 切块盘点（valid_to 即 reindex 留痕，页面展示 reindex 状态）。"""
    store = request.app.state.container.get("store")
    return {"stale_chunks": store.list_stale_chunks(limit=_clamped_limit(limit))}


@knowledge_router.get("/entities")
async def list_entities(
    request: Request, limit: int = Query(default=200)
) -> dict[str, Any]:
    """实体盘点（关系浏览的实体侧；关系侧仍走 /relations）。"""
    store = request.app.state.container.get("store")
    return {"entities": store.list_entities_browse(limit=_clamped_limit(limit))}


@knowledge_router.get("/inventory-export")
async def export_inventory(request: Request) -> JSONResponse:
    """盘点表导出（FR-UI-004 AC：可导出）；JSON 附件形态，与 CLI --inventory 同源数据。"""
    store = request.app.state.container.get("store")
    payload = json_safe(store.list_knowledge_documents())
    return JSONResponse(
        payload,
        headers={
            "Content-Disposition": (
                'attachment; filename="knowledge-inventory.json"'
            )
        },
    )


@knowledge_router.post("/documents/{urn:path}/governance")
async def edit_governance(
    urn: str, body: GovernanceEdit, request: Request
) -> dict[str, Any]:
    """owner/权威等级编辑（R1 内部写）：审计历史只追加进 raw_json 治理段，
    reindex 由 preserve_governance 回灌存活；URN 不存在 404 不臆造。"""
    store = request.app.state.container.get("store")
    doc = store.get_document(urn)
    if doc is None:
        raise NotFoundError(f"知识文档不存在: {urn}")
    updated = apply_governance(
        doc,
        owner=body.owner,
        authority_level=body.authority_level,
        edited_by=body.edited_by,
        edited_at=datetime.now(timezone.utc),
    )
    if not store.update_document_governance(updated):
        # 并发删除竞态：读时存在、写时消失，按不存在处理而非静默成功
        raise NotFoundError(f"知识文档已被删除: {urn}")
    return {
        "urn": urn,
        "owner": updated.metadata_.get("governance", {}).get("owner", ""),
        "authority_level": str(updated.authority_level),
        "governance": updated.metadata_["governance"],
    }


def json_safe(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """sqlite Row 投影已是原生 dict；包一层保持导出信封稳定。"""
    return {"exported_at": datetime.now(timezone.utc).isoformat(), "documents": rows}
