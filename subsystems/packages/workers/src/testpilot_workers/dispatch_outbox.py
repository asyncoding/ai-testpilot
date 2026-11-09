"""dispatch-outbox-worker：派发待写 outbox（docs/08 FR-WR-003，S2.5）。

读取 profile 装配容器，扫 pending/failed outbox 并按 target_system 路由。
kill switch 打开时条目保持 pending（报告 blocked），不会产生任何外部写；
有 failed 条目时进程非零退出，便于 cron/编排器重试告警。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from ._bootstrap import bootstrap
from .outbox_dispatch import dispatch_pending_outbox


def main() -> None:
    argparse.ArgumentParser(description="TestPilot outbox 派发 worker").parse_args()
    container = bootstrap("dispatch-outbox-worker")
    report = asyncio.run(dispatch_pending_outbox(container))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["status"] != "succeeded":
        print(
            f"[dispatch-outbox-worker] {report['failed']} 条派发失败（failed，可重试），"
            f"{report['blocked']} 条被停用开关拦截",
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
