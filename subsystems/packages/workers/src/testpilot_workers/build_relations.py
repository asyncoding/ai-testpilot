"""build-relations-worker：用例关系派生 CLI（FR-ING-004，S2.6b）。

读 profile 装配容器，纯确定性派生用例↔需求/符号关系并整组替换独立 scope；
输入指纹未变则 skipped_unchanged。空用例库 fail-closed（exit 2），不造空关系。
"""

from __future__ import annotations

import argparse
import json
import sys

from ._bootstrap import bootstrap
from .relation_building import run_relation_building


def main() -> None:
    parser = argparse.ArgumentParser(
        description="TestPilot 用例↔需求/符号关系构建 worker")
    parser.add_argument("--full", action="store_true",
                        help="忽略指纹对账，整组重建")
    args = parser.parse_args()

    container = bootstrap("build-relations-worker")
    try:
        report = run_relation_building(container, full=args.full)
    except ValueError as exc:
        print(f"[build-relations-worker] 构建被拒: {exc}", file=sys.stderr)
        sys.exit(2)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
