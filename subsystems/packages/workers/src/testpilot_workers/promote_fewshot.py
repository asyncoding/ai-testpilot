"""promote-fewshot-worker：人审偏好样本回流 L3 few-shot 资产（FR-KNO-002，S2.6a）。

读 profile 装配容器，把 preference_samples 全量构建为 few_shot 资产新版本并
不可变落账；同批样本重放内容 hash 不变 → 版本幂等不增生（报告 inserted=false）。
无偏好样本时 core fail-closed，进程非零退出。
"""

from __future__ import annotations

import argparse
import json
import sys

from ._bootstrap import bootstrap
from .asset_service import DEFAULT_FEWSHOT_ASSET_ID, DEFAULT_FEWSHOT_NAME, promote_preferences_to_fewshot


def main() -> None:
    parser = argparse.ArgumentParser(
        description="TestPilot 偏好样本 → L3 few-shot 资产回流 worker")
    parser.add_argument("--asset-id", default=DEFAULT_FEWSHOT_ASSET_ID,
                        help=f"few_shot 资产 ID，默认 {DEFAULT_FEWSHOT_ASSET_ID}")
    parser.add_argument("--name", default=DEFAULT_FEWSHOT_NAME)
    parser.add_argument("--author", default="")
    parser.add_argument("--note", default="")
    args = parser.parse_args()

    container = bootstrap("promote-fewshot-worker")
    try:
        report = promote_preferences_to_fewshot(
            container, asset_id=args.asset_id, name=args.name,
            author=args.author, note=args.note)
    except ValueError as exc:  # AssetValidationError 是 ValueError 系
        print(f"[promote-fewshot-worker] 回流被拒: {exc}", file=sys.stderr)
        sys.exit(2)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
