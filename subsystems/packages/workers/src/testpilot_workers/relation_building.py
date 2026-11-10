"""case-relations-worker：用例↔需求/符号关系派生入库（docs/08 FR-ING-004，S2.6b）。

纯确定性，无模型调用：从 test_cases / requirement_skeletons /
oracle_suggestions / test_points 四类**既有 S2 产物**派生闭集关系，写入
relations 表的独立 scope（doc_urn=RELATION_SCOPE_URN）。摄入管线的 calls 等
关系 doc_urn 是真实文档 URN，与本 scope 物理隔离，整组替换互不误伤。

增量：输入指纹（canonical JSON）落 meta，未变跳过；--full 强制重建。
"""

from __future__ import annotations

import hashlib
from typing import Any

from testpilot_core.case_relations import (
    RELATION_SCOPE_URN,
    PointRef,
    SuggestionRef,
    build_case_relations,
    build_fr_index,
)
from testpilot_core.oracle_mining import suggestion_urn
from testpilot_core.rule_assets import canonical_json
from testpilot_core.testpoint import point_urn

_HASH_KEY = "case-relations-hash:v1"
_RELATION_KINDS = ("case_validates_requirement", "case_references_symbol")


def _gather_inputs(store: Any) -> tuple[list, list, list[SuggestionRef], list[PointRef]]:
    cases = store.list_test_cases()
    skeleton_rows = store.list_requirement_skeleton_rows()
    fr_sources = [
        (row["external_id"], [(f.fr_id, f.urn) for f in row["result"].features])
        for row in skeleton_rows
    ]
    suggestions = [
        SuggestionRef(
            case_urn=row["case_urn"],
            urn=suggestion_urn(row["external_id"], row["oracle"]),
            source_refs=list(row["oracle"].source_refs),
            source_kind=row["oracle"].source_kind,
            provenance=row["oracle"].provenance,
        )
        for row in store.list_oracle_suggestions()
    ]
    points = [
        PointRef(
            urn=point_urn(row["external_id"], row["point"]),
            fr_id=row["point"].fr_id,
            source_refs=list(row["point"].source_refs),
        )
        for row in store.list_test_point_rows()
    ]
    return cases, fr_sources, suggestions, points


def _fingerprint(cases: list, fr_sources: list,
                 suggestions: list[SuggestionRef], points: list[PointRef]) -> str:
    """四类输入的规范指纹：同输入恒等，任何引用变化都触发重建。"""
    payload = {
        "cases": sorted(
            (c.urn, c.lifecycle,
             sorted(t for t in c.tags if t.startswith("FR-")))
            for c in cases
        ),
        "fr_sources": sorted(
            (ext, sorted(pairs)) for ext, pairs in fr_sources
        ),
        "suggestions": sorted(
            (s.case_urn, s.urn, sorted(s.source_refs), s.source_kind, s.provenance)
            for s in suggestions
        ),
        "points": sorted(
            (p.urn, p.fr_id, sorted(p.source_refs)) for p in points
        ),
    }
    return canonical_json(payload)


def _counts_by_kind(relations: list) -> dict[str, int]:
    return {
        kind: sum(1 for r in relations if r.kind == kind)
        for kind in _RELATION_KINDS
    }


def run_relation_building(container: Any, *, full: bool = False) -> dict[str, Any]:
    store = container.get("store")
    cases, fr_sources, suggestions, points = _gather_inputs(store)
    if not cases:
        raise ValueError("用例库为空：先跑 testpilot-ingest-cases 再构建用例关系")

    fingerprint = _fingerprint(cases, fr_sources, suggestions, points)
    if not full and store.get_meta(_HASH_KEY) == fingerprint:
        existing = store.list_case_relations(include_low=True)
        return {
            "status": "skipped_unchanged",
            "scope_urn": RELATION_SCOPE_URN,
            "cases_seen": len(cases),
            "relations_total": len(existing),
            "relations_by_kind": _counts_by_kind(existing),
            "low_confidence": sum(1 for r in existing if r.is_low_confidence),
            "replaced": 0,
            "skipped_unchanged": 1,
            "ambiguous_fr_tags": [],
            "unknown_fr_tags": [],
            "unparseable_refs": [],
            "input_hash": hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:16],
        }

    fr_index, ambiguous = build_fr_index(fr_sources)
    result = build_case_relations(
        cases=cases,
        fr_index=fr_index,
        suggestions=suggestions,
        points=points,
        ambiguous_fr_tags=ambiguous,
    )
    replaced = store.replace_case_relations(RELATION_SCOPE_URN, result.relations)
    store.set_meta(_HASH_KEY, fingerprint)

    return {
        "status": "succeeded",
        "scope_urn": RELATION_SCOPE_URN,
        "cases_seen": result.cases_seen,
        "cases_cancelled": result.cases_cancelled,
        "relations_total": len(result.relations),
        "relations_by_kind": _counts_by_kind(result.relations),
        "low_confidence": result.low_confidence_count,
        "replaced": replaced,
        "skipped_unchanged": 0,
        "ambiguous_fr_tags": result.ambiguous_fr_tags,
        "unknown_fr_tags": result.unknown_fr_tags,
        "unparseable_refs": result.unparseable_refs,
        "input_hash": hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:16],
    }
