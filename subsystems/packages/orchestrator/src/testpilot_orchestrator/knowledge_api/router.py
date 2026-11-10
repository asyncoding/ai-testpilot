"""/api/knowledge/* 路由：用例关系浏览与证据原文（S2.6b FR-ING-004）。

薄 HTTP 包装：派生语义在 core.case_relations，落账在 workers.relation_building，
本层只做查询参数收口与容器取接。关系只读本 API，没有任何写端点
（构建走 testpilot-build-relations worker / CLI）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request

from testpilot_core.case_relations import (
    RELATION_SCOPE_URN,
    CaseRelationError,
    assemble_evidence_dossier,
)
from testpilot_core.errors import NotFoundError

knowledge_router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])

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
