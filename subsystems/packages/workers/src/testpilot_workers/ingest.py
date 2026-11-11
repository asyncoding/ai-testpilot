"""ingest-worker：知识入库（docs/08 FR-ING-001）。

默认增量（content_hash 对账）；--full 全量重切（幂等）；
--scheduled 定时兜底增量（trigger='scheduled' 留痕，供 cron 挂接，进程内
不引真定时器——docs/04 #14：不用定时器扫描冒充事件）；
--inventory 导出语料盘点（FR-CON-002 AC3）；
--latency-report 输出摄入延迟 P95 报告（FR-FRS-001 AC1，不跑入库）；
--replay-events 消费 JSON 事件目录（FR-CON-005，事件驱动摄入入口）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from testpilot_adapters.json_events import JsonReplayEventSource

from ._bootstrap import bootstrap
from .event_dispatch import dispatch_events
from .handler_wiring import wire_default_handlers
from .ingest_latency import collect_latency_reports, reports_to_dict
from .ingestion import run_ingestion


def main() -> None:
    parser = argparse.ArgumentParser(description="TestPilot 知识入库 worker")
    sweep = parser.add_mutually_exclusive_group()
    sweep.add_argument("--full", action="store_true", help="忽略 hash 对账，全部重切重登记")
    sweep.add_argument(
        "--scheduled", action="store_true",
        help="定时兜底增量（trigger='scheduled'，由外部 cron 挂接，进程内无定时器）",
    )
    parser.add_argument("--inventory", metavar="PATH", help="只导出语料盘点 JSON，不入库")
    parser.add_argument(
        "--latency-report", metavar="PATH", nargs="?", const="",
        help="只输出摄入延迟 P95 报告（intake+end_to_end）；带 PATH 写文件，不带打 stdout",
    )
    parser.add_argument(
        "--replay-events", metavar="DIR",
        help="只消费事件目录（JSON 信封 replay），不跑扫描入库",
    )
    parser.add_argument(
        "--event-source", metavar="SYSTEM", default="",
        help="配合 --replay-events 按 source_system 过滤（默认不过滤）",
    )
    args = parser.parse_args()

    container = bootstrap("ingest-worker")
    docs = container.get("docs")
    print(
        f"[ingest-worker] ready; docs_adapter={type(docs).__name__ if docs else None}",
        file=sys.stderr,
    )

    if args.replay_events:
        source = JsonReplayEventSource(Path(args.replay_events))
        # S4.2：挂接 CommitPushed→保鲜 handler 后再派发（无 handler 的类型走 no-op）
        wire_default_handlers()
        report = asyncio.run(
            dispatch_events(container, source=args.event_source, event_source=source)
        )
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
        if report.dead:
            print(
                f"[ingest-worker] 事件消费 {report.dead} 条进死信"
                f"（dead_letters source 前缀 event-dispatch，可查可补偿）",
                file=sys.stderr,
            )
            sys.exit(1)
        return

    if args.latency_report is not None:
        payload = reports_to_dict(collect_latency_reports(container.get("store")))
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        if args.latency_report:
            out = Path(args.latency_report)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(text, encoding="utf-8")
            print(f"[ingest-worker] latency report -> {out}", file=sys.stderr)
        else:
            print(text)
        return

    if args.inventory:
        rows = docs.inventory()
        out = Path(args.inventory)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[ingest-worker] inventory: {len(rows)} docs -> {out}", file=sys.stderr)
        return

    if args.full:
        trigger, full = "full", True
    elif args.scheduled:
        # 定时全量兜底走增量 hash 对账（FR-FRS-001）；批次留痕 trigger='scheduled'
        trigger, full = "scheduled", False
    else:
        trigger, full = "incremental", False
    report = asyncio.run(run_ingestion(container, trigger=trigger, full=full))
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    if report.failed or report.embed_failed:
        print(
            f"[ingest-worker] 批次 {report.status}：{report.failed} 个文档加工失败、"
            f"{report.embed_failed} 个文档嵌入失败（均进 dead_letters，可重跑补偿）",
            file=sys.stderr,
        )
        # partial 显式非零退出：手工/CI 不能把降级批次误读为成功
        sys.exit(1)


if __name__ == "__main__":
    main()
