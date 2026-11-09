"""mine-oracles-worker：oracle 四源挖掘入库（docs/08 FR-TD-003，S2.4）。

默认增量（按用例文档 content_hash 对账）；--full 强制整组重建。
前置管线：先跑 testpilot-ingest-cases（S2.1）与
testpilot-extract-requirements（S2.2，可选，缺骨架只是 AC 源为空）。
模型幻觉行进 dead_letters（source=oracle-mine），不影响批次成功状态；
草稿恒为 pending，S2.5 HITL 审批后才回写正式用例库。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from ._bootstrap import bootstrap
from .oracle_mining import run_oracle_mining


def main() -> None:
    parser = argparse.ArgumentParser(description="TestPilot oracle 挖掘 worker")
    parser.add_argument("--full", action="store_true", help="忽略 hash 对账，全部整组重建")
    args = parser.parse_args()

    container = bootstrap("mine-oracles-worker")
    report = asyncio.run(run_oracle_mining(container, full=args.full))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["status"] != "succeeded":
        print(
            f"[mine-oracles-worker] 批次 {report['status']}："
            f"{report['model_rejected']} 条幻觉拒入、{report['retrieval_degraded']} 次检索降级",
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
