"""badcase-worker：反馈/bad case 闭环 CLI（FR-EV-005，S2.6c）。

子命令：ingest / list / triage / promote / resolve / stats。
领域校验失败（BadCaseError 等 ValueError 系）与 NotFoundError 一律 exit 2、
信息走 stderr；成功报告走 stdout（JSON），便于 hermetic 探针与脚本判定。
"""

from __future__ import annotations

import argparse
import json
import sys

from testpilot_core.errors import NotFoundError

from . import badcase_service as svc
from ._bootstrap import bootstrap


def _dump(payload) -> str:
    if hasattr(payload, "model_dump"):
        payload = payload.model_dump(mode="json")
    return json.dumps(payload, ensure_ascii=False, indent=2)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="TestPilot 反馈/bad case 闭环 worker")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("ingest", help="扫描反馈表，把 👎 负反馈投影成 bad case")

    p_list = sub.add_parser("list", help="台账盘点")
    p_list.add_argument("--status", default=None)
    p_list.add_argument("--source", default=None)

    p_tri = sub.add_parser("triage", help="分诊 accept/reject")
    p_tri.add_argument("--id", required=True, dest="bad_case_id")
    p_tri.add_argument("--decision", required=True, choices=["accept", "reject"])
    p_tri.add_argument("--actor", required=True)
    p_tri.add_argument("--reason", default="")
    p_tri.add_argument("--note", default="")

    p_pro = sub.add_parser("promote", help="accepted 坏例转 golden 回归题")
    p_pro.add_argument("--id", required=True, dest="bad_case_id")
    p_pro.add_argument("--actor", required=True)
    p_pro.add_argument("--note", default="")

    p_res = sub.add_parser("resolve", help="关联回归评测结果 pass/fail")
    p_res.add_argument("--id", required=True, dest="bad_case_id")
    p_res.add_argument("--eval-run-id", required=True)
    p_res.add_argument("--outcome", required=True, choices=["pass", "fail"])
    p_res.add_argument("--actor", required=True)
    p_res.add_argument("--note", default="")

    sub.add_parser("stats", help="闭环统计（闭环率/中位周期/各态计数）")

    args = parser.parse_args()
    container = bootstrap("badcase-worker")
    try:
        if args.command == "ingest":
            result = svc.ingest_feedback(container)
        elif args.command == "list":
            result = [c.model_dump(mode="json")
                      for c in svc.list_bad_cases(
                          container, status=args.status, source=args.source)]
        elif args.command == "triage":
            result = svc.triage_case(
                container, args.bad_case_id, decision=args.decision,
                actor=args.actor, reason=args.reason, note=args.note)
        elif args.command == "promote":
            result = svc.promote_case(
                container, args.bad_case_id, actor=args.actor, note=args.note)
            result = {"bad_case": result["bad_case"].model_dump(mode="json"),
                      "eval_case": result["eval_case"].model_dump(mode="json")}
        elif args.command == "resolve":
            result = svc.associate_regression_run(
                container, args.bad_case_id, eval_run_id=args.eval_run_id,
                outcome=args.outcome, actor=args.actor, note=args.note)
        else:  # stats
            result = svc.bad_case_stats(container)
    except (ValueError, NotFoundError) as exc:
        print(f"[badcase-worker] 操作被拒: {exc}", file=sys.stderr)
        sys.exit(2)
    print(_dump(result))


if __name__ == "__main__":
    main()
