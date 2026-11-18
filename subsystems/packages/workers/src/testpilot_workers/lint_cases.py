"""lint-cases-worker：用例文档结构 lint CLI（docs/17 §5，M1，2026-11-16）。

只读 worker：复用 profile 装配的 ``cases`` 适配器扫描语料，对每份文档跑
core 零依赖 ``lint_case_doc``，输出分级发现项与聚合指标 JSON。不写语料仓、
不碰 store、不调外部系统。

退出码（对齐 case_export 约定）：
- 0：无 error 级发现、无 IO 错误（warning/info 仅报告，M1 不阻断）；
- 1：存在 error 级发现或文档读取错误；
- 2：环境错误（容器未装配 cases 适配器等）。
``--strict`` 下任何级别发现都按 1 退出，留给 M3 阈值门禁启用。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from typing import Any

from testpilot_core.case_lint import aggregate_reports, lint_case_doc

from ._bootstrap import bootstrap


def run_lint(container: Any) -> dict[str, Any]:
    adapter = container.get("cases")
    if adapter is None:
        raise ValueError("容器未装配 cases 适配器（profile 的 adapters.cases.type=stub？）")

    docs, io_errors = adapter.load()
    reports = [
        lint_case_doc(doc.text, doc.result, doc.external_id) for doc in docs
    ]
    aggregate = aggregate_reports(reports)
    counts = aggregate["finding_counts"]
    # 状态判定在 service 层（与 run_case_ingestion 同构），main 只做退出码映射
    if io_errors or counts["error"]:
        status = "failed"
    elif counts["warning"] or counts["info"]:
        status = "findings"
    else:
        status = "succeeded"
    return {
        "status": status,
        "docs_seen": len(docs),
        "io_errors": io_errors,
        "docs": [
            {
                "external_id": report.doc_external_id,
                "metrics": report.metrics,
                "findings": [asdict(f) for f in report.findings],
            }
            for report in reports
        ],
        "aggregate": aggregate,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="TestPilot 用例文档 lint 探针（M1 报告模式）")
    parser.add_argument(
        "--strict", action="store_true",
        help="任何级别发现都按退出码 1 失败（默认仅 error/IO 错误失败）",
    )
    args = parser.parse_args()

    try:
        container = bootstrap("lint-cases-worker")
        report = run_lint(container)
    except Exception as exc:  # noqa: BLE001 - 环境错误统一映射 exit 2
        print(f"[lint-cases-worker] 环境错误：{type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(2)

    counts = report["aggregate"]["finding_counts"]
    print(json.dumps(report, ensure_ascii=False, indent=2))

    if report["status"] == "failed" or (
        args.strict and (counts["warning"] or counts["info"])
    ):
        sys.exit(1)


if __name__ == "__main__":
    main()
