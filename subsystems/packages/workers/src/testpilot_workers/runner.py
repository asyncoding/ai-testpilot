"""runner-worker CLI：测试执行的提交、认领循环、取消与崩溃恢复（FR-EX-002，S3.1）。

子命令：
  submit     登记请求并入队（先账本后队列；同幂等键重放返回同一 run_id）
  run-once   单轮认领处理（探针/测试用，队列空即退出）
  worker     长循环（启动先回收过期租约，按 profile 并发上限协程化）
  cancel     对 queued/running 的 run 打协作式取消标记
  list       执行账本盘点（可按 status/project 过滤）
  recover    显式触发过期租约回收（worker 启动时也会自动做一次）

纪律：成功 JSON 走 stdout；ValueError/NotFound/策略拒绝/适配器不可用走 stderr
并 exit 2，对齐 badcase_cli。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from testpilot_core.errors import (
    AdapterUnavailableError,
    NotFoundError,
    PolicyDeniedError,
)
from testpilot_core.execution import run_id_for
from testpilot_core.models import CaseSelector, EnvironmentRequirements, TestExecutionRequest

from . import execution_service as svc
from ._bootstrap import bootstrap

_CLI_ERRORS = (ValueError, NotFoundError, PolicyDeniedError, AdapterUnavailableError)


def _dump(payload) -> str:
    if hasattr(payload, "model_dump"):
        payload = payload.model_dump(mode="json")
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _build_request(args: argparse.Namespace) -> TestExecutionRequest:
    if args.request_json:
        payload = json.loads(Path(args.request_json).read_text(encoding="utf-8"))
        return TestExecutionRequest.model_validate(payload)
    missing = [
        name for name in ("request_id", "idempotency_key", "project", "commit_sha")
        if not getattr(args, name)
    ]
    if missing:
        raise ValueError(f"缺少必要参数（或改用 --request-json）: {', '.join(missing)}")
    return TestExecutionRequest(
        request_id=args.request_id,
        idempotency_key=args.idempotency_key,
        project=args.project,
        commit_sha=args.commit_sha,
        build_id=args.build_id,
        daily_budget_key=args.rerun_key,
        selector=CaseSelector(
            test_ids=args.test_id or [],
            fixture_hint=args.fixture_hint,
        ),
        environment=EnvironmentRequirements(capabilities=args.capability or []),
        triggered_by=args.triggered_by,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="TestPilot runner-worker（测试执行）")
    # 无参 = 装配自检（与 ingest/eval worker 同约定，verify_s0 A5 依赖 ready 输出）
    sub = parser.add_subparsers(dest="command")

    p_submit = sub.add_parser("submit", help="登记执行请求并入队")
    p_submit.add_argument("--request-json", help="TestExecutionRequest JSON 文件")
    p_submit.add_argument("--request-id")
    p_submit.add_argument("--idempotency-key")
    p_submit.add_argument("--project")
    p_submit.add_argument("--commit-sha")
    p_submit.add_argument("--build-id")
    p_submit.add_argument("--test-id", action="append", help="可重复，选择执行的用例 ID")
    p_submit.add_argument("--fixture-hint", help="录制/文件适配器选报告用")
    p_submit.add_argument("--rerun-key", help="带此 key 即重跑，占每日重跑配额")
    p_submit.add_argument("--capability", action="append")
    p_submit.add_argument("--triggered-by", default="agent")

    sub.add_parser("run-once", help="单轮认领处理，队列空即退出")

    p_worker = sub.add_parser("worker", help="长循环 worker（默认；SIGINT/SIGTERM 优雅退出）")
    p_worker.add_argument("--max-iterations", type=int, default=None)
    p_worker.add_argument("--poll-interval", type=float, default=1.0)

    p_cancel = sub.add_parser("cancel", help="打协作式取消标记")
    p_cancel.add_argument("--run-id", required=True)

    p_list = sub.add_parser("list", help="执行账本盘点")
    p_list.add_argument("--status", default=None)
    p_list.add_argument("--project", default=None)

    sub.add_parser("recover", help="回收过期租约（worker 启动也会自动回收）")

    args = parser.parse_args()
    container = bootstrap("runner-worker")
    if args.command is None:
        runner = container.get("runner")
        queue = container.get("queue")
        print(
            "[runner-worker] ready; "
            f"runner={type(runner).__name__ if runner else None} "
            f"queue={type(queue).__name__}"
        )
        return
    try:
        if args.command == "submit":
            request = _build_request(args)
            result = asyncio.run(svc.submit_execution(container, request))
        elif args.command == "run-once":
            result = asyncio.run(svc.run_once(container))
        elif args.command == "worker":
            result = asyncio.run(svc.run_worker(
                container,
                max_iterations=args.max_iterations,
                poll_interval=args.poll_interval,
            ))
        elif args.command == "cancel":
            result = asyncio.run(svc.request_cancel(container, args.run_id))
        elif args.command == "list":
            result = svc.list_runs(
                container, status=args.status, project=args.project)
        else:  # recover
            result = asyncio.run(svc.recover_stale(container))
    except _CLI_ERRORS as exc:
        print(f"[runner-worker] 操作被拒: {exc}", file=sys.stderr)
        sys.exit(2)
    print(_dump(result))


if __name__ == "__main__":
    main()
