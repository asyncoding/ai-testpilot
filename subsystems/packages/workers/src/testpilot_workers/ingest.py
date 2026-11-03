"""ingest-worker：知识入库（docs/08 FR-ING-001）。

默认增量（content_hash 对账）；--full 全量重切（幂等）；
--inventory 导出语料盘点（FR-CON-002 AC3）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from ._bootstrap import bootstrap
from .ingestion import run_ingestion


def main() -> None:
    parser = argparse.ArgumentParser(description="TestPilot 知识入库 worker")
    parser.add_argument("--full", action="store_true", help="忽略 hash 对账，全部重切重登记")
    parser.add_argument("--inventory", metavar="PATH", help="只导出语料盘点 JSON，不入库")
    args = parser.parse_args()

    container = bootstrap("ingest-worker")
    docs = container.get("docs")
    print(
        f"[ingest-worker] ready; docs_adapter={type(docs).__name__ if docs else None}",
        file=sys.stderr,
    )

    if args.inventory:
        rows = docs.inventory()
        out = Path(args.inventory)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[ingest-worker] inventory: {len(rows)} docs -> {out}", file=sys.stderr)
        return

    trigger = "full" if args.full else "incremental"
    report = asyncio.run(run_ingestion(container, trigger=trigger, full=args.full))
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    if report.failed:
        print(
            f"[ingest-worker] {report.failed} 个文档加工失败（已进 dead_letters，不阻断整批）",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
