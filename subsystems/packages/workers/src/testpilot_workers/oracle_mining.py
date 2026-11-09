"""oracle 四源挖掘管线（docs/08 FR-TD-003，S2.4）。

输入是 S2.1 已入库的 test_cases 与 S2.2 的 requirement_skeletons；每条活跃用例：

1. 影响面检索（相似用例/事故/符号），证据 URN 构成模型白名单的一部分；
2. core 确定性三源先挖（需求 AC / 自身与相似用例现有断言 / 事故义务句），
   无模型也产出完整草稿；代码符号只作锚点，不编造 expectation；
3. **模型只补 oracle_missing 用例**（预算守卫，docs/07 §5.4），core 白名单逐行
   校验：幻觉行进 dead_letters（source=oracle-mine），绝不入库；
4. 草稿落 oracle_suggestions 表（schema v11，status=pending，不回写正式
   test_cases.oracles，S2.5 HITL 审批才转正）；按用例整案替换防幽灵；
5. AC3 决策回写 test_cases.exploratory：挖不到 ui/log/state 可执行 oracle 的
   用例标记探索性，自动化执行链路据此排除。

模型不可用/降级/整包形状非法 → 保留确定性草稿并计入 degraded_cases，不抛出。
"""

from __future__ import annotations

from typing import Any

from testpilot_core.errors import AdapterUnavailableError
from testpilot_core.oracle_mining import (
    OracleMiningValidationError,
    OracleMiningResult,
    apply_model_oracles,
    build_oracle_messages,
    mine_deterministic,
    oracle_json_schema,
)
from testpilot_core.retrieval import shape_trigram_query
from testpilot_core.testpoint import ImpactEvidence, partition_impact_chunks
from testpilot_core.urn import parse_urn

_DEAD_LETTER_SOURCE = "oracle-mine"
_GEN_KEY_PREFIX = "oracle_mine:"
_HASH_KEY_PREFIX = "oracle-doc-hash:"


def _recording_key(case: Any) -> str:
    """录制模型/追踪键：URN 反解出 `<doc_external_id>#<case_id>`，不另造命名空间。"""
    _, _, external_body, _ = parse_urn(case.urn)
    return f"{_GEN_KEY_PREFIX}{external_body}"


def _hash_meta_key(doc_urn: str) -> str:
    _, _, doc_external_id, _ = parse_urn(doc_urn)
    return f"{_HASH_KEY_PREFIX}{doc_external_id}"


def _select_skeleton(case: Any, skeleton_rows: list[dict[str, Any]]) -> Any:
    """挑与用例 FR tag 有交集的需求骨架；无交集取第一份，无骨架返回 None。

    POC 每项目单需求文档（offline-demo 与 V1 真档均 1 行）；多文档场景下
    模型白名单只覆盖选中骨架，是已知边界，不在 POC 范围内虚构通用合并。
    """
    if not skeleton_rows:
        return None
    fr_tags = {t for t in case.tags if t.startswith("FR-")}
    if fr_tags:
        for row in skeleton_rows:
            known = {f.fr_id for f in row["result"].active_features}
            if fr_tags.intersection(known):
                return row["result"]
    return skeleton_rows[0]["result"]


async def _gather_case_evidence(
    container: Any, case: Any, branch: str, cases_by_urn: dict[str, Any]
) -> tuple[list[ImpactEvidence], list[Any], int]:
    """相似用例（FR tag/标题）+ 事故/符号（检索适配器）。

    返回 (证据列表, 相似用例实体列表, 检索降级次数)；检索故障不阻断挖掘。
    """
    store = container.get("store")
    evidence: list[ImpactEvidence] = []
    related: list[Any] = []
    retrieval_degraded = 0

    fr_tags = [t for t in case.tags if t.startswith("FR-")]
    raw_query = " ".join([case.title, *case.steps])[:400]
    _, like_terms = shape_trigram_query(raw_query)
    for row in store.search_test_cases(fr_tags, like_terms, limit=10):
        if row["urn"] == case.urn:
            continue
        related_case = cases_by_urn.get(row["urn"])
        if related_case is None:
            continue
        related.append(related_case)
        evidence.append(ImpactEvidence(
            urn=row["urn"], kind="test_case",
            title=row["title"], text=row["text"], fr_ids=row["tags"],
        ))

    search = container.get("search")
    if search is not None and raw_query.strip():
        try:
            chunks = await search.search(
                raw_query,
                project=container.config.project.name,
                branch=branch,
                limit=8,
            )
        except Exception:  # 检索故障不阻断：证据留空，降级由报告计数可观测
            retrieval_degraded += 1
        else:
            incidents, symbols = partition_impact_chunks(list(chunks))
            evidence.extend(incidents)
            evidence.extend(symbols)
    return evidence, related, retrieval_degraded


async def _mine_with_model(
    model: Any, case: Any, spine: OracleMiningResult,
    skeleton: Any, evidence: list[ImpactEvidence], related: list[Any],
) -> tuple[OracleMiningResult, bool]:
    """返回 (挖掘结果, 是否降级)。任何异常/降级信封都退回确定性草稿。"""
    try:
        envelope = await model.structured(
            build_oracle_messages(case, skeleton, evidence, spine.mined),
            oracle_json_schema(),
            key=_recording_key(case),
            required_nonempty=("oracles",),
        )
    except AdapterUnavailableError:
        return spine, True
    if envelope.get("degraded") or not envelope.get("data"):
        return spine, True
    try:
        result = apply_model_oracles(
            case, spine, envelope["data"], skeleton, evidence, related
        )
    except OracleMiningValidationError:
        return spine, True
    result.model_version = str(envelope.get("model", ""))
    return result, False


async def run_oracle_mining(container: Any, *, full: bool = False) -> dict[str, Any]:
    store = container.get("store")
    if store is None or store.count_test_cases() == 0:
        raise ValueError(
            "test_cases 为空：先跑 testpilot-ingest-cases（S2.1）产出结构化用例"
        )
    model = container.get("model")  # stub profile 下为 None：纯确定性模式
    skeleton_rows = store.list_requirement_skeleton_rows()  # 空列表合法：AC 源为空

    cases = store.list_test_cases(include_cancelled=True)
    cases_by_urn = {c.urn: c for c in cases}
    doc_rows = store.list_case_doc_rows()
    # 同文档用例共享 content_hash 与增量键：按 doc_urn 分组
    docs: dict[str, dict[str, Any]] = {}
    for row in doc_rows:
        bucket = docs.setdefault(row["doc_urn"], {
            "content_hash": row["content_hash"],
            "branch": row["branch"], "case_urns": [],
        })
        bucket["case_urns"].append(row["urn"])

    report: dict[str, Any] = {
        "status": "succeeded",
        "docs_seen": len(docs),
        "cases_seen": len(cases),
        "cases_skipped_unchanged": 0,
        "cases_rebuilt": 0,
        "cases_cancelled": 0,
        "suggestions_total": 0,
        "deterministic_oracles": 0,
        "model_oracles": 0,
        "model_rejected": 0,
        "model_deduped": 0,
        "model_calls": 0,
        "degraded_cases": 0,
        "exploratory_cases": 0,
        "hallucination_rate": 0.0,
        "evidence_related": 0,
        "evidence_incidents": 0,
        "evidence_symbols": 0,
        "retrieval_degraded": 0,
    }
    rejected_total = 0
    model_denominator = 0

    for doc_urn, bucket in docs.items():
        meta_key = _hash_meta_key(doc_urn)
        if not full and store.get_meta(meta_key) == bucket["content_hash"]:
            report["cases_skipped_unchanged"] += len(bucket["case_urns"])
            continue

        # 文档级整组清扫：被删掉的用例不再出现在 case_urns 里，只有按 doc_urn
        # 清扫才能回收其孤儿草稿（与 test_points 同型的防幽灵纪律）
        store.delete_oracle_suggestions_for_doc(doc_urn)

        for case_urn in bucket["case_urns"]:
            case = cases_by_urn.get(case_urn)
            if case is None:
                continue
            if case.lifecycle == "cancelled":
                # 取消用例不挖掘；历史草稿已在上面的文档级清扫中回收
                report["cases_cancelled"] += 1
                continue

            skeleton = _select_skeleton(case, skeleton_rows)
            evidence, related, retrieval_degraded = await _gather_case_evidence(
                container, case, bucket["branch"], cases_by_urn
            )
            report["retrieval_degraded"] += retrieval_degraded
            spine = mine_deterministic(case, skeleton, evidence, related)

            # 预算守卫：模型只补 oracle_missing 用例（docs/07 §5.4）
            if model is not None and case.oracle_missing:
                report["model_calls"] += 1
                result, degraded = await _mine_with_model(
                    model, case, spine, skeleton, evidence, related
                )
                if degraded:
                    report["degraded_cases"] += 1
            else:
                result = spine

            for rejected in result.rejected:
                store.add_dead_letter(
                    _DEAD_LETTER_SOURCE,
                    rejected.model_dump() | {"case_urn": case.urn},
                    f"{rejected.reason}: {rejected.detail}",
                )
                rejected_total += 1

            # 草稿落库（文档级清扫已在循环外完成，此处只 upsert）
            for oracle in result.mined:
                store.upsert_oracle_suggestion(
                    doc_urn, case.urn, case.external_id, oracle,
                    exploratory=result.exploratory,
                    content_hash=bucket["content_hash"], branch=bucket["branch"],
                )

            # AC3 决策回写正式表（exploratory 是 TestCase 既有字段，全实体幂等重写）
            if case.exploratory != result.exploratory:
                updated = case.model_copy(update={"exploratory": result.exploratory})
                store.upsert_test_case(
                    doc_urn, updated,
                    content_hash=bucket["content_hash"], branch=bucket["branch"],
                )

            report["cases_rebuilt"] += 1
            report["suggestions_total"] += len(result.mined)
            report["deterministic_oracles"] += sum(
                1 for o in result.mined if o.provenance == "deterministic")
            report["model_oracles"] += len(result.model_oracles)
            report["model_rejected"] += len(result.rejected)
            report["model_deduped"] += result.deduped
            report["evidence_related"] += sum(
                1 for e in evidence if e.kind == "test_case")
            report["evidence_incidents"] += sum(
                1 for e in evidence if e.kind == "incident")
            report["evidence_symbols"] += sum(
                1 for e in evidence if e.kind == "code_symbol")
            if result.exploratory:
                report["exploratory_cases"] += 1
            model_denominator += len(result.model_oracles) + len(result.rejected)

        store.set_meta(meta_key, bucket["content_hash"])

    report["hallucination_rate"] = (
        round(rejected_total / model_denominator, 6) if model_denominator else 0.0
    )
    return report
