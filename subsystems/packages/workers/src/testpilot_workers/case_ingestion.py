"""用例文档入库管线（docs/08 FR-CON-003，S2.1）。

把 CaseDocumentAdapter 的解析批次落进 test_cases 表，两类失败都进 dead_letters：
- IO 级（文件不可读）：连接器隔离后在此补记；
- 解析级（损坏行/空壳用例）：core 解析器产出的 CaseParseError。

重灌幂等：文档 content_hash 未变则跳过（--full 强制）；重写按 doc_urn 整组
替换，文档内删除的用例不残留。URN 不随内容变化，重跑不产生重复行。
"""

from __future__ import annotations

from typing import Any

from testpilot_adapters.case_docs import SOURCE_SYSTEM


def _hash_meta_key(external_id: str) -> str:
    return f"case-doc-hash:{external_id}"


def run_case_ingestion(container: Any, *, full: bool = False) -> dict[str, Any]:
    adapter = container.get("cases")
    store = container.get("store")
    if adapter is None:
        raise ValueError("容器未装配 cases 适配器（profile 的 adapters.cases.type=stub？）")

    docs, io_errors = adapter.load()
    report: dict[str, Any] = {
        "status": "succeeded",
        "docs_seen": len(docs),
        "skipped_unchanged": 0,
        "docs_rebuilt": 0,
        "cases_upserted": 0,
        "cancelled": 0,
        "oracle_missing": 0,
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

        store.delete_test_cases_for_doc(doc.doc_urn)
        for parse_error in doc.result.dead_letters:
            store.add_dead_letter(
                SOURCE_SYSTEM,
                parse_error.model_dump() | {"doc_urn": doc.doc_urn, "uri": doc.uri},
                parse_error.error,
            )
            report["parse_dead_letters"] += 1
        for case in doc.result.cases:
            store.upsert_test_case(
                doc.doc_urn, case,
                content_hash=doc.content_hash, branch=adapter.branch,
            )
            report["cases_upserted"] += 1
            if case.lifecycle == "cancelled":
                report["cancelled"] += 1
            if case.oracle_missing:
                report["oracle_missing"] += 1
        store.set_meta(meta_key, doc.content_hash)
        report["docs_rebuilt"] += 1

    if report["parse_dead_letters"] or report["io_errors"]:
        report["status"] = "partial"
    return report
