"""generate-testpoints-worker：测试点生成入库（docs/08 FR-TD-002，S2.3）。

默认增量（按需求文档 content_hash 对账）；--full 强制整组重建。
前置管线：先跑 testpilot-extract-requirements 产出 requirement_skeletons。
模型幻觉行被白名单拒入 dead_letters（source=tp-gen），不影响批次成功状态，
但 hallucination_rate 会在报告与盲标评测中单列。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from ._bootstrap import bootstrap
from .testpoint_generation import run_testpoint_generation


def main() -> None:
    parser = argparse.ArgumentParser(description="TestPilot 测试点生成 worker")
    parser.add_argument("--full", action="store_true", help="忽略 hash 对账，全部整组重建")
    args = parser.parse_args()

    container = bootstrap("generate-testpoints-worker")
    report = asyncio.run(run_testpoint_generation(container, full=args.full))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["status"] != "succeeded":
        print(
            f"[generate-testpoints-worker] 批次 {report['status']}："
            f"{report['model_rejected']} 条幻觉拒入、{report['retrieval_degraded']} 次检索降级",
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
