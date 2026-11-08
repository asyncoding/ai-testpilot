#!/usr/bin/env python3
"""S1.6 离线评测探针（docs/08 FR-EV-001～003）——recorded / ollama 双形态。

recorded（默认，verify_s0 A9）：offline-demo profile + 录制模型，跑
golden_recorded_smoke 机控题集。全确定性、无网、无真模型，任一裁判失败/泄漏即 exit 1，
守的是评测链路装配（加载→问答→适配→裁判→聚合→落库→报告），不承担质量测量。

ollama（E4 基线/标定手动执行）：先把 profile 的 sqlite 在线备份到 /tmp 隔离目录
（不碰活库与用户终端进程），Chroma 继续 HTTP 共享只读，真 ollama 跑 golden_s1_v1
30 题。质量差距是发现项不是探针失败：本形态只在机械故障时 exit 1，门禁对照写进报告。

用法（在 subsystems/ 下）：
  # A9 机控（离线，确定性）
  TESTPILOT_PROFILE=config/profiles/offline-demo.yaml \\
    .venv/bin/python scripts/eval_probe.py
  # E4 真模型 hermetic 基线（需 docker chroma + ollama serve 在线）
  .venv/bin/python scripts/eval_probe.py --mode ollama \\
    --profile config/profiles/ios-local-poc.yaml [--limit 5]
  # E5 追加隔离语义裁判（裁判模型默认 deepseek-r1:7b，与生成模型不同体）
  .venv/bin/python scripts/eval_probe.py --mode ollama --semantic-judge
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
from pathlib import Path

SUBSYSTEMS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SUBSYSTEMS / "packages" / "workers" / "src"))

SMOKE_GOLDEN = SUBSYSTEMS / "tests" / "fixtures" / "eval" / "golden_recorded_smoke.json"
GOLDEN_S1 = SUBSYSTEMS / "tests" / "fixtures" / "eval" / "golden_s1.json"
DEFAULT_REPORT_DIR = SUBSYSTEMS / "out" / "eval"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="S1.6 评测探针")
    parser.add_argument("--mode", choices=["recorded", "ollama"], default="recorded")
    parser.add_argument("--profile", default=None,
                        help="默认 recorded=offline-demo，ollama=ios-local-poc")
    parser.add_argument("--golden", default=None,
                        help="默认按形态选 smoke / golden_s1 题集")
    parser.add_argument("--report-dir", default=str(DEFAULT_REPORT_DIR))
    parser.add_argument("--limit", type=int, default=0,
                        help="只跑前 N 题（0=全量；hermetic 路径冒烟用）")
    parser.add_argument("--copy-dir", default="",
                        help="ollama 形态的 hermetic 副本目录（默认系统临时目录）")
    parser.add_argument("--compare", nargs=2, metavar=("BEFORE_JSON", "AFTER_JSON"),
                        default=None,
                        help="只做两份评测 JSON 的成对对比（单变量守卫+逐题翻转），输出 MD 路径")
    parser.add_argument("--semantic-judge", action="store_true",
                        help="ollama 形态追加 E5 隔离语义裁判（deepseek-r1:7b ≠ 生成模型）")
    parser.add_argument("--judge-model", default="deepseek-r1:7b",
                        help="语义裁判模型（必须与生成模型不同体）")
    return parser.parse_args()


def _run_compare(before_path: str, after_path: str, report_dir: str) -> int:
    from testpilot_workers.eval_runner import (
        compare_reports,
        load_report_json,
        render_comparison,
    )

    diff = compare_reports(load_report_json(before_path), load_report_json(after_path))
    out_dir = Path(report_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / (
        f"comparison-{diff['before_eval_run_id'][:8]}-vs-"
        f"{diff['after_eval_run_id'][:8]}.md")
    out_path.write_text(render_comparison(diff), encoding="utf-8")
    print(render_comparison(diff))
    print(f"对比报告：{out_path}")
    # 单变量纪律破坏时 exit 1（机械失败语义：数字不可归因就不该进标定结论）
    return 1 if diff["discipline_warning"] else 0


async def _run_recorded(args: argparse.Namespace) -> int:
    from testpilot_workers._bootstrap import bootstrap
    from testpilot_workers.eval_runner import load_golden, run_evaluation
    from testpilot_workers.ingestion import run_ingestion

    golden_path = Path(args.golden) if args.golden else SMOKE_GOLDEN
    # bootstrap 读 TESTPILOT_PROFILE，缺省即 offline-demo（与 qa_probe 同约定）
    container = bootstrap("eval-probe")
    report = None
    try:
        ingest = await run_ingestion(container, trigger="eval-probe", full=True)
        if ingest.failed:
            print(f"❌ 入库失败文档 {ingest.failed} 个")
            return 1
        golden = load_golden(golden_path)
        cases = golden.cases[: args.limit or None]
        report = await run_evaluation(
            container, cases, set_id=golden.set_id, mode="recorded",
            report_dir=args.report_dir,
        )
    finally:
        container.get("store").close()

    _print_summary(report)
    failed = [r.case_id for r in report.records if not r.verdict.passed]
    if failed or report.leakage:
        print(f"❌ 机控题集出现红灯（夹具/链路回归）：failed={failed} leakage={list(report.leakage)}")
        return 1
    print("✅ eval_probe recorded 全部通过")
    return 0


async def _run_ollama(args: argparse.Namespace) -> int:
    from testpilot_orchestrator.config_loader import load_profile
    from testpilot_orchestrator.factory import build_container
    from testpilot_workers.eval_runner import (
        hermetic_copy_data_dir,
        load_golden,
        run_evaluation,
    )

    profile_path = Path(args.profile) if args.profile else (
        SUBSYSTEMS / "config" / "profiles" / "ios-local-poc.yaml")
    golden_path = Path(args.golden) if args.golden else GOLDEN_S1
    config = load_profile(profile_path)

    # 解析源 data_dir（与 factory._p 同口径：相对 profile 所在仓的 subsystems 根）
    src_data_dir = (SUBSYSTEMS / config.paths["data_dir"]).resolve()
    copy_root = Path(args.copy_dir) if args.copy_dir else Path(
        tempfile.mkdtemp(prefix="tp-eval-hermetic-"))
    copy_dir = hermetic_copy_data_dir(src_data_dir, copy_root)
    print(f"[hermetic] {src_data_dir} → {copy_dir}（源库只读，未改动）")

    config.paths["data_dir"] = str(copy_dir)
    container = build_container(config, SUBSYSTEMS)
    report = None
    try:
        golden = load_golden(golden_path)
        cases = golden.cases[: args.limit or None]
        # 生产路径：case_key 留空，模型网关走通用 key（ollama 网关不读录制夹具）
        # E5：可选隔离裁判（复用同一网关、model 覆盖为不同体的 deepseek-r1:7b）
        judge_gateway = container.get("model") if args.semantic_judge else None
        report = await run_evaluation(
            container, cases, set_id=golden.set_id, mode="ollama",
            report_dir=args.report_dir, case_key_fn=lambda _case: "",
            judge_gateway=judge_gateway, judge_model=args.judge_model,
        )
    finally:
        container.get("store").close()

    _print_summary(report)
    print("ℹ️ ollama 形态：质量差距见报告门禁对照，机械故障才计探针失败")
    return 0


def _print_summary(report) -> None:
    m = report.metrics
    print(f"\n=== eval_run {report.eval_run_id}（{report.set_id} / {report.mode}）===")
    print(f"四元组: data={report.version.data_version or '（空）'} "
          f"model={report.version.model_version or '（空）'} "
          f"prompt={report.version.prompt_version or '（空）'} "
          f"params={report.version.params_version or '（空）'} "
          f"degraded_path={report.version.degraded_path}")
    print(f"通过 {m['golden_passed']:.0f}/{m['cases_total']:.0f} "
          f"({m['golden_pass_rate']:.4f})；Recall@K={m['recall_at_k']:.4f} "
          f"拒答={m['refusal_accuracy']:.4f} 引用准确={m['citation_accuracy']:.4f} "
          f"伪造={m['citation_forgery_count']:.0f}")
    print(f"延迟中位={m['latency_median_ms']:.0f}ms p95={m['latency_p95_ms']:.0f}ms "
          f"token in={m['tokens_in_total']:.0f} out={m['tokens_out_total']:.0f}")
    reds = [(r.case_id, r.verdict.labels) for r in report.records if not r.verdict.passed]
    for case_id, labels in reds:
        print(f"  RED {case_id}: {labels}")
    print(f"泄漏检查: {list(report.leakage) or '无'}")
    print(f"报告: {report.report_md}")


async def _amain() -> int:
    args = _parse_args()
    if args.compare:
        return _run_compare(args.compare[0], args.compare[1], args.report_dir)
    if args.mode == "recorded":
        return await _run_recorded(args)
    return await _run_ollama(args)


def main() -> None:
    sys.exit(asyncio.run(_amain()))


if __name__ == "__main__":
    main()
