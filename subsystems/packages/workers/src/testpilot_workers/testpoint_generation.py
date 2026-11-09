"""测试点生成管线（docs/08 FR-TD-002，S2.3）。

输入是 S2.2 已入库的 requirement_skeletons（管线串联：先抽取后生成）；
每个需求文档执行：

1. 影响面检索（相似用例/事故/符号），证据 URN 构成模型白名单的一部分；
2. core 确定性脊柱先跑（无模型也产出完整清单）；
3. 模型只补 boundary/negative 点，core 白名单逐行校验：幻觉行进
   dead_letters（source=tp-gen），绝不入库、绝不进正式清单；
4. 整文档替换落 test_points 表（schema v10），content_hash 增量对账。

模型不可用/降级/整包形状非法 → 保留确定性脊柱并计入 degraded_docs，不抛出。
"""

from __future__ import annotations

from typing import Any

from testpilot_core.errors import AdapterUnavailableError
from testpilot_core.retrieval import shape_trigram_query
from testpilot_core.testpoint import (
    ImpactEvidence,
    TestPointValidationError,
    apply_model_points,
    build_impact_query,
    build_testpoint_messages,
    deterministic_result,
    deterministic_test_points,
    partition_impact_chunks,
    testpoint_json_schema,
)

_DEAD_LETTER_SOURCE = "tp-gen"
_GEN_KEY_PREFIX = "tp_gen:"


def _hash_meta_key(external_id: str) -> str:
    return f"tp-doc-hash:{external_id}"


async def _gather_evidence(
    container: Any, result: Any, branch: str
) -> tuple[list[ImpactEvidence], int]:
    """相似用例（本地 FR tag/标题）+ 事故/符号（现有检索适配器）。

    返回 (证据列表, 检索降级次数)；检索故障不阻断生成。
    """
    store = container.get("store")
    evidence: list[ImpactEvidence] = []
    retrieval_degraded = 0

    fr_ids = [f.fr_id for f in result.active_features]
    query = build_impact_query(result)
    _, like_terms = shape_trigram_query(query)
    for row in store.search_test_cases(fr_ids, like_terms, limit=10):
        evidence.append(ImpactEvidence(
            urn=row["urn"], kind="test_case",
            title=row["title"], text=row["text"], fr_ids=row["tags"],
        ))

    search = container.get("search")
    if search is not None and query.strip():
        try:
            chunks = await search.search(
                query,
                project=container.config.project.name,
                branch=branch,
                limit=8,
            )
        except Exception:  # 检索故障不阻断生成：证据留空，降级由报告计数可观测
            retrieval_degraded += 1
        else:
            incidents, symbols = partition_impact_chunks(list(chunks))
            evidence.extend(incidents)
            evidence.extend(symbols)
    return evidence, retrieval_degraded


async def _generate_with_model(
    model: Any, result: Any, spine: list, evidence: list
) -> tuple[Any, bool]:
    """返回 (生成结果, 是否降级)。任何异常/降级信封都退回确定性脊柱。"""
    try:
        envelope = await model.structured(
            build_testpoint_messages(result, evidence, spine),
            testpoint_json_schema(),
            key=f"{_GEN_KEY_PREFIX}{result.external_id}",
            required_nonempty=("points",),
        )
    except AdapterUnavailableError:
        return deterministic_result(result, evidence), True
    if envelope.get("degraded") or not envelope.get("data"):
        return deterministic_result(result, evidence), True
    try:
        return apply_model_points(result, spine, envelope["data"], evidence), False
    except TestPointValidationError:
        return deterministic_result(result, evidence), True


async def run_testpoint_generation(container: Any, *, full: bool = False) -> dict[str, Any]:
    store = container.get("store")
    if store is None or store.count_requirement_skeletons() == 0:
        raise ValueError(
            "requirement_skeletons 为空：先跑 testpilot-extract-requirements（S2.2）"
        )
    model = container.get("model")  # stub profile 下为 None：纯确定性模式

    rows = store.list_requirement_skeleton_rows()
    report: dict[str, Any] = {
        "status": "succeeded",
        "docs_seen": len(rows),
        "skipped_unchanged": 0,
        "docs_rebuilt": 0,
        "points_total": 0,
        "spine_points": 0,
        "model_points": 0,
        "model_rejected": 0,
        "model_deduped": 0,
        "hallucination_rate": 0.0,
        "degraded_docs": 0,
        "evidence_cases": 0,
        "evidence_incidents": 0,
        "evidence_symbols": 0,
        "retrieval_degraded": 0,
        "p0_points": 0,
        "p1_points": 0,
    }
    rejected_total = 0
    model_denominator = 0

    for row in rows:
        result = row["result"]
        external_id = row["external_id"]
        meta_key = _hash_meta_key(external_id)
        if not full and store.get_meta(meta_key) == row["content_hash"]:
            report["skipped_unchanged"] += 1
            continue

        evidence, retrieval_degraded = await _gather_evidence(
            container, result, row["branch"]
        )
        report["retrieval_degraded"] += retrieval_degraded
        spine = deterministic_test_points(result)
        if model is not None:
            generation, degraded = await _generate_with_model(
                model, result, spine, evidence
            )
            if degraded:
                report["degraded_docs"] += 1
        else:
            generation = deterministic_result(result, evidence)

        for rejected in generation.rejected:
            store.add_dead_letter(
                _DEAD_LETTER_SOURCE,
                rejected.model_dump() | {"doc_external_id": external_id},
                f"{rejected.reason}: {rejected.detail}",
            )
            rejected_total += 1

        store.delete_test_points_for_doc(row["doc_urn"])
        for point in generation.points:
            store.upsert_test_point(
                row["doc_urn"], external_id, point,
                content_hash=row["content_hash"], branch=row["branch"],
            )
        store.set_meta(meta_key, row["content_hash"])

        report["docs_rebuilt"] += 1
        report["points_total"] += len(generation.points)
        report["spine_points"] += generation.spine_count
        report["model_points"] += len(generation.model_points)
        report["model_rejected"] += len(generation.rejected)
        report["model_deduped"] += generation.deduped
        report["evidence_cases"] += sum(1 for e in evidence if e.kind == "test_case")
        report["evidence_incidents"] += sum(1 for e in evidence if e.kind == "incident")
        report["evidence_symbols"] += sum(1 for e in evidence if e.kind == "code_symbol")
        report["p0_points"] += sum(1 for p in generation.points if p.priority == "P0")
        report["p1_points"] += sum(1 for p in generation.points if p.priority == "P1")
        model_denominator += len(generation.model_points) + len(generation.rejected)

    report["hallucination_rate"] = (
        round(rejected_total / model_denominator, 6) if model_denominator else 0.0
    )
    return report
