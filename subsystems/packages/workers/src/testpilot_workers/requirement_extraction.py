"""需求文档骨架抽取管线（docs/08 FR-TD-001，S2.2）。

把 RequirementDocumentAdapter 的抽取批次落进 requirement_skeletons 表：

- 确定性解析永远先跑（core 零依赖），保证无模型环境也能产出骨架与缺口；
- 模型只做 AC↔FR 语义链接，且只能引用源文档存在的 ID（core 白名单校验，
  未知 ID/越界序号整包拒绝）；模型不可用/降级/校验失败 → 保留确定性结果并
  计入 link_degraded_docs，绝不编造链接；
- IO 级与解析级失败都进 dead_letters（source=fs-reqs），partial 显式非零退出。

重灌幂等：content_hash 未变跳过（--full 强制）；重写按 doc_urn 整组替换。
"""

from __future__ import annotations

from typing import Any

from testpilot_adapters.requirement_docs import SOURCE_SYSTEM
from testpilot_core.errors import AdapterUnavailableError
from testpilot_core.requirement import (
    SkeletonValidationError,
    apply_linking,
    build_linking_messages,
    linking_json_schema,
)

_LINK_KEY_PREFIX = "req_link:"


def _hash_meta_key(external_id: str) -> str:
    return f"req-doc-hash:{external_id}"


async def _link_with_model(model: Any, result: Any) -> tuple[Any, bool]:
    """返回 (结果, 是否降级)。任何异常/降级信封都退回确定性结果，不抛出。"""
    try:
        envelope = await model.structured(
            build_linking_messages(result),
            linking_json_schema(),
            key=f"{_LINK_KEY_PREFIX}{result.external_id}",
            required_nonempty=("links",),
        )
    except AdapterUnavailableError:
        return result, True
    if envelope.get("degraded") or not envelope.get("data"):
        return result, True
    try:
        return apply_linking(result, envelope["data"]), False
    except SkeletonValidationError:
        return result, True


async def run_requirement_extraction(
    container: Any, *, full: bool = False
) -> dict[str, Any]:
    adapter = container.get("requirements")
    store = container.get("store")
    if adapter is None:
        raise ValueError(
            "容器未装配 requirements 适配器（profile 的 adapters.requirements.type=stub？）"
        )
    model = container.get("model")  # stub profile 下为 None：纯确定性模式

    docs, io_errors = adapter.load()
    report: dict[str, Any] = {
        "status": "succeeded",
        "docs_seen": len(docs),
        "skipped_unchanged": 0,
        "docs_rebuilt": 0,
        "features_active": 0,
        "features_cancelled": 0,
        "rules": 0,
        "acceptance_criteria": 0,
        "ac_missing": 0,
        "gaps": 0,
        "model_linked_docs": 0,
        "link_degraded_docs": 0,
        "parse_dead_letters": 0,
        "io_errors": len(io_errors),
    }

    for error in io_errors:
        store.add_dead_letter(SOURCE_SYSTEM, error, error["error"])

    for doc in docs:
        meta_key = _hash_meta_key(doc.external_id)
        if not full and store.get_meta(meta_key) == doc.content_hash:
            report["skipped_unchanged"] += 1
            continue

        result = doc.result
        if model is not None and result.acceptance:
            result, degraded = await _link_with_model(model, result)
            if degraded:
                report["link_degraded_docs"] += 1
            else:
                report["model_linked_docs"] += 1

        store.delete_requirement_skeleton(doc.doc_urn)
        for parse_error in doc.result.dead_letters:
            store.add_dead_letter(
                SOURCE_SYSTEM,
                parse_error.model_dump() | {"doc_urn": doc.doc_urn, "uri": doc.uri},
                parse_error.error,
            )
            report["parse_dead_letters"] += 1
        store.upsert_requirement_skeleton(
            doc.doc_urn, result,
            content_hash=doc.content_hash, branch=adapter.branch,
        )
        store.set_meta(meta_key, doc.content_hash)

        report["docs_rebuilt"] += 1
        report["features_active"] += len(result.active_features)
        report["features_cancelled"] += sum(
            1 for f in result.features if f.lifecycle == "cancelled"
        )
        report["rules"] += len(result.rules)
        report["acceptance_criteria"] += len(result.acceptance)
        report["ac_missing"] += sum(
            1 for g in result.gaps if g.kind == "ac_missing_for_feature"
        )
        report["gaps"] += len(result.gaps)

    if report["parse_dead_letters"] or report["io_errors"]:
        report["status"] = "partial"
    return report
