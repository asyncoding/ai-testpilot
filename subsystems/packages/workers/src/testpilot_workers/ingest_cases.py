"""ingest-cases-worker：用例 Markdown 入库（docs/08 FR-CON-003，S2.1）。

默认增量（content_hash 对账）；--full 强制整组重建。partial（有死信）显式
非零退出，手工/CI 不得把带解析失败的批次误读为成功。
"""

from __future__ import annotations

import argparse
import json
import sys

from ._bootstrap import bootstrap
from .case_ingestion import run_case_ingestion


def main() -> None:
    parser = argparse.ArgumentParser(description="TestPilot 用例文档入库 worker")
    parser.add_argument("--full", action="store_true", help="忽略 hash 对账，全部整组重建")
    args = parser.parse_args()

    container = bootstrap("ingest-cases-worker")
    report = run_case_ingestion(container, full=args.full)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["status"] != "succeeded":
        print(
            f"[ingest-cases-worker] 批次 {report['status']}："
            f"{report['parse_dead_letters']} 条解析死信、{report['io_errors']} 个读取错误"
            "（均进 dead_letters，可在 UI 查看）",
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
