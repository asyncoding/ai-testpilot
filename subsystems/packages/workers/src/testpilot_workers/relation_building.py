"""case-relations-worker：用例↔需求/符号关系派生入库（docs/08 FR-ING-004，S2.6b）。

纯确定性，无模型调用：从 test_cases / requirement_skeletons /
oracle_suggestions / test_points 四类**既有 S2 产物**派生闭集关系，写入
relations 表的独立 scope（doc_urn=RELATION_SCOPE_URN）。摄入管线的 calls 等
关系 doc_urn 是真实文档 URN，与本 scope 物理隔离，整组替换互不误伤。

两条低置信扩链路径（2026-11-21 切片 A+B，均 < LOW_CONFIDENCE_THRESHOLD，
知识浏览默认不可见、仅影响面等机器消费者显式取低置信时生效）：

- 路径⑤检索挂链：:func:`collect_relation_inputs` 对每条活跃用例的标题+步骤
  跑 ``kinds=("code_symbol",)`` 确定性检索（FTS 是固定 SQL 排序；向量通道即便
  启用也是固定权重前向计算），把命中切块的 1:1 固化字段 ``symbol_urn`` 投影成
  :class:`RetrievalSymbolRef`，core 以 0.50 置信挂链；
- 路径⑥calls 传播：从 relations 表 kind='calls' 投影调用边
  :class:`CallsEdge`，core 沿 caller→callee 出边 BFS 两跳
  （hop1 0.45 / hop2 0.40，环安全）。

检索适配器缺省（stub profile）时路径⑤无输入，其余路径行为不变。检索故障
fail-loud：本 worker 的输入指纹是增量闸门，吞错会让残缺输入被指纹背书、
后续构建错误跳过，故不做静默降级（CLI 非零退出、无写入，可安全重跑）。

增量：输入指纹（canonical JSON）落 meta，未变跳过；--full 强制重建。
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import Any

from testpilot_core.case_relations import (
    RELATION_SCOPE_URN,
    CallsEdge,
    PointRef,
    RetrievalSymbolRef,
    SuggestionRef,
    build_case_relations,
    build_fr_index,
)
from testpilot_core.oracle_mining import suggestion_urn
from testpilot_core.rule_assets import canonical_json
from testpilot_core.testpoint import point_urn

# v2：纳入路径⑤检索挂链输入、路径⑥ calls 边与规则版本位。v1→v2 key 改名后
# 老指纹天然失配，升级后首次运行自动整组重建，无需手工 --full。
_HASH_KEY = "case-relations-hash:v2"
_RELATION_KINDS = ("case_validates_requirement", "case_references_symbol")
# 与 oracle_mining/testpoint_generation 的证据检索口径对齐：同查询整形、同 400
# 字符截断；top-k=8 与 kinds 过滤是 11-21 实测覆盖率定参（49.0%→76.5%）。
_RETRIEVAL_KINDS = ("code_symbol",)
_RETRIEVAL_TOP_K = 8
_QUERY_MAX_CHARS = 400


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


def _case_query(case: Any) -> str:
    """用例文本检索串：标题 + 步骤，与 oracle 证据挖掘同口径截断。"""
    return " ".join([case.title, *case.steps])[:_QUERY_MAX_CHARS]


async def collect_relation_inputs(
    container: Any, cases: list
) -> tuple[list[RetrievalSymbolRef], list[CallsEdge]]:
    """路径⑤/⑥的 IO 侧采集（core 保持零 IO）：检索挂链引用 + calls 边。

    检索只跑活跃用例（取消用例在 core 侧整体跳过，查了也是噪声）。同一用例
    同一符号多切块命中时保留首次命中（检索返回序本身确定），输出按用例库序
    稳定。search 适配器未装配（stub profile）→ 路径⑤空输入，不报错。
    """
    store = container.get("store")
    edges = store.list_calls_edges()

    search = container.get("search")
    if search is None:
        return [], edges

    project = container.config.project.name
    branch = container.config.project.default_branch
    refs: list[RetrievalSymbolRef] = []
    for case in cases:
        if case.lifecycle == "cancelled":
            continue
        query = _case_query(case)
        if not query.strip():
            continue
        result = await search.search(
            query,
            project=project,
            branch=branch,
            kinds=_RETRIEVAL_KINDS,
            limit=_RETRIEVAL_TOP_K,
        )
        # HybridResult 可迭代出 KnowledgeChunk；FTS 适配器直接返回切块 list
        chunks = list(result)
        seen_symbols: set[str] = set()
        for chunk in chunks:
            symbol_urn = chunk.symbol_urn
            if not symbol_urn or symbol_urn in seen_symbols:
                continue
            seen_symbols.add(symbol_urn)
            refs.append(RetrievalSymbolRef(
                case_urn=case.urn,
                symbol_urn=symbol_urn,
                chunk_urn=chunk.urn,
            ))
    return refs, edges


def _fingerprint(
    cases: list,
    fr_sources: list,
    suggestions: list[SuggestionRef],
    points: list[PointRef],
    retrieval_refs: Sequence[RetrievalSymbolRef] = (),
    calls_edges: Sequence[CallsEdge] = (),
) -> str:
    """全量输入的规范指纹：同输入恒等，任何引用变化都触发重建。"""
    payload = {
        "rule_version": 2,
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
        "retrieval_refs": sorted(
            (r.case_urn, r.symbol_urn, r.chunk_urn) for r in retrieval_refs
        ),
        "calls_edges": sorted(
            (e.source_urn, e.target_urn) for e in calls_edges
        ),
    }
    return canonical_json(payload)


def _counts_by_kind(relations: list) -> dict[str, int]:
    return {
        kind: sum(1 for r in relations if r.kind == kind)
        for kind in _RELATION_KINDS
    }


def _count_by_provenance(relations: list, tag: str) -> int:
    return sum(1 for r in relations if tag in r.provenance)


def run_relation_building(
    container: Any,
    *,
    full: bool = False,
    retrieval_refs: Sequence[RetrievalSymbolRef] = (),
    calls_edges: Sequence[CallsEdge] = (),
) -> dict[str, Any]:
    store = container.get("store")
    cases, fr_sources, suggestions, points = _gather_inputs(store)
    if not cases:
        raise ValueError("用例库为空：先跑 testpilot-ingest-cases 再构建用例关系")

    fingerprint = _fingerprint(
        cases, fr_sources, suggestions, points, retrieval_refs, calls_edges)
    if not full and store.get_meta(_HASH_KEY) == fingerprint:
        existing = store.list_case_relations(include_low=True)
        return {
            "status": "skipped_unchanged",
            "scope_urn": RELATION_SCOPE_URN,
            "cases_seen": len(cases),
            "relations_total": len(existing),
            "relations_by_kind": _counts_by_kind(existing),
            "low_confidence": sum(1 for r in existing if r.is_low_confidence),
            "retrieval_link_count": _count_by_provenance(existing, "retrieval_link"),
            "calls_propagated_count": _count_by_provenance(existing, "calls_propagation"),
            "retrieval_refs_seen": len(retrieval_refs),
            "calls_edges_seen": len(calls_edges),
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
        retrieval_refs=retrieval_refs,
        calls_edges=calls_edges,
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
        "retrieval_link_count": result.retrieval_link_count,
        "calls_propagated_count": result.calls_propagated_count,
        "retrieval_refs_seen": len(retrieval_refs),
        "calls_edges_seen": len(calls_edges),
        "replaced": replaced,
        "skipped_unchanged": 0,
        "ambiguous_fr_tags": result.ambiguous_fr_tags,
        "unknown_fr_tags": result.unknown_fr_tags,
        "unparseable_refs": result.unparseable_refs,
        "input_hash": hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:16],
    }
