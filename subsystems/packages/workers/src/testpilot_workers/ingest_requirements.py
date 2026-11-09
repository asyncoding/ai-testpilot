"""extract-requirements-worker：需求 Markdown 骨架抽取入库（docs/08 FR-TD-001，S2.2）。

默认增量（content_hash 对账）；--full 强制整组重建。partial（有死信/读取错误）
显式非零退出，手工/CI 不得把带解析失败的批次误读为成功。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from ._bootstrap import bootstrap
from .requirement_extraction import run_requirement_extraction


def main() -> None:
    parser = argparse.ArgumentParser(description="TestPilot 需求骨架抽取 worker")
    parser.add_argument("--full", action="store_true", help="忽略 hash 对账，全部整组重建")
    args = parser.parse_args()

    container = bootstrap("extract-requirements-worker")
    report = asyncio.run(run_requirement_extraction(container, full=args.full))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["status"] != "succeeded":
        print(
            f"[extract-requirements-worker] 批次 {report['status']}："
            f"{report['parse_dead_letters']} 条解析死信、{report['io_errors']} 个读取错误"
            "（均进 dead_letters，可在 UI 查看）",
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
