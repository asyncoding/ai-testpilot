"""build-relations-worker：用例关系派生 CLI（FR-ING-004，S2.6b）。

读 profile 装配容器，纯确定性派生用例↔需求/符号关系并整组替换独立 scope；
输入指纹未变则 skipped_unchanged。空用例库 fail-closed（exit 2），不造空关系。

路径⑤检索挂链需要异步检索适配器：先 :func:`collect_relation_inputs` 完成
IO 采集，再把投影结果交给同步的 :func:`run_relation_building`（既有 hostless
测试直调同步入口，无需事件循环）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from ._bootstrap import bootstrap
from .relation_building import collect_relation_inputs, run_relation_building


async def _run(container, *, full: bool) -> dict:
    store = container.get("store")
    cases = store.list_test_cases()
    # 检索故障 fail-loud：异常直接冒泡到 main，非零退出且无任何写入/指纹落盘
    retrieval_refs, calls_edges = await collect_relation_inputs(container, cases)
    return run_relation_building(
        container,
        full=full,
        retrieval_refs=retrieval_refs,
        calls_edges=calls_edges,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="TestPilot 用例↔需求/符号关系构建 worker")
    parser.add_argument("--full", action="store_true",
                        help="忽略指纹对账，整组重建")
    args = parser.parse_args()

    container = bootstrap("build-relations-worker")
    try:
        report = asyncio.run(_run(container, full=args.full))
    except ValueError as exc:
        print(f"[build-relations-worker] 构建被拒: {exc}", file=sys.stderr)
        sys.exit(2)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
