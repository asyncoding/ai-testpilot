"""impact-worker：影响面分析 + MR 摘要 + 推荐精度回算 CLI（FR-FRS-003，S4.3）。

读 profile 装配容器（与其他 worker 同 bootstrap），对一个版本区间做：
影响链三分类 → diff 反推 MR 摘要 → 可选：以真实 run 回算推荐精度。
纯只读 worker，不写任何外部系统；输入非法/容器缺件 exit 2。
"""

from __future__ import annotations

import argparse
import json
import sys

from ._bootstrap import bootstrap
from .impact_service import (
    analyze_impact,
    build_mr_summary,
    precision_from_run,
)


def _load_model_notes(path: str) -> list[dict]:
    """模型备注 JSON 文件；形状校验在 core.supplement_summary 的白名单关做。"""
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, list):
        raise ValueError("--model-notes 必须是 JSON 数组（每项 {text, symbol_urn}）")
    return data


async def _run(args: argparse.Namespace) -> dict:
    container = bootstrap("impact-worker")
    analysis = await analyze_impact(
        container,
        base_commit=args.base,
        head_commit=args.head,
        requirement_urns=args.requirement or [],
    )
    model_rows = _load_model_notes(args.model_notes) if args.model_notes else None
    summary = await build_mr_summary(
        container,
        base_commit=args.base,
        head_commit=args.head,
        model_rows=model_rows,
        model_version=args.model_version or "",
    )
    payload: dict = {
        "analysis": analysis.model_dump(mode="json"),
        "summary": summary.model_dump(mode="json"),
    }
    if args.run_id:
        payload["precision"] = (
            await precision_from_run(
                container,
                analysis=analysis,
                run_id=args.run_id,
                full_suite_size=args.full_suite_size,
            )
        ).model_dump(mode="json")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(
        description="TestPilot 影响面分析 / MR 摘要 / 推荐精度回算 worker")
    parser.add_argument("--base", required=True, help="基线 commit sha（或空树 sha）")
    parser.add_argument("--head", required=True, help="目标 commit sha")
    parser.add_argument("--requirement", action="append", default=[],
                        help="需求 URN 入口，可重复")
    parser.add_argument("--run-id", default="",
                        help="给定执行 run 时回算推荐精度（FR-FRS-003 AC2）")
    parser.add_argument("--full-suite-size", type=int, default=0,
                        help="全套用例规模，给定时输出回归集缩减比例")
    parser.add_argument("--model-notes", default="",
                        help="模型风险备注 JSON 数组文件（白名单外逐行拒入）")
    parser.add_argument("--model-version", default="",
                        help="模型备注来源版本，留痕用")
    args = parser.parse_args()

    try:
        import asyncio

        payload = asyncio.run(_run(args))
    except (ValueError, RuntimeError, FileNotFoundError) as exc:
        print(f"[impact-worker] 分析被拒: {exc}", file=sys.stderr)
        sys.exit(2)
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
