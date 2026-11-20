"""testpilot-hybrid：混合分段会话驱动 CLI（H4，docs/01 §5/§8）。

把 H3 的 hybrid_runner 函数面收敛成五个可在独立进程调用的子命令，供锁屏 E2E
runbook 人工驱动（自动步连环推进、人工步挂闸门、/gates 轮询裁决后续跑）：

- ``start --plan plan.json``：载入 hybrid-plan-v1 载荷建/重建会话（幂等）；
- ``drive --session hsess-xxx --udid ... --bundle-id ...``：推进到下一个
  awaiting_human/blocked/终态；
- ``resume --session ... --gate gate-... --udid ... --bundle-id ...``：
  人工闸门裁决后续跑（前置闸门/步骤闸门两条路由 hybrid_runner 内部区分）；
- ``unblock --session ... --udid ... --bundle-id ...``：环境阻塞解除后续跑；
- ``get --session ...``：只读打印会话快照（计划/检查点/账本）；
- ``ws-run``：USB 反向监听单用例全链路（xctestrun 注入→驱动→xcodebuild
  test-without-building→闸门转录），退出码 0 全绿 / 2 硬错误 / 3 产品失败
  （含人工 reject，reject 永不自动冲绿）。

设备目标（udid/bundle_id）显式传参，不写死机器信息；缺参 exit 2。
硬错误统一 exit 2、成功 exit 0，stdout 为单个 JSON 快照，stderr 只放装配日志。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from testpilot_core.errors import AdapterUnavailableError, NotFoundError
from testpilot_core.hybrid_plan import HybridPlanError

from testpilot_workers._bootstrap import bootstrap
from testpilot_workers.hybrid_probes import ProbeContext
from testpilot_workers.hybrid_runner import (
    drive_until_wait,
    get_session,
    resume_after_gate,
    start_session,
    unblock,
)
from testpilot_workers.ws_relay_session import (
    WsRelayHardError,
    WsRelayProductFailure,
    WsRelayRequest,
    run_ws_relay,
)

# 与既有 worker CLI 同口径：业务/装配硬错误收口 exit 2，不抛 traceback
_CLI_ERRORS = (
    HybridPlanError, NotFoundError, AdapterUnavailableError,
    ValueError, RuntimeError, FileNotFoundError, json.JSONDecodeError,
)


def _load_plan(plan_path: str) -> dict[str, Any]:
    path = Path(plan_path)
    if not path.is_file():
        raise FileNotFoundError(f"混合计划文件不存在: {plan_path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _ctx(args: argparse.Namespace) -> ProbeContext:
    if not args.udid or not args.bundle_id:
        # 设备目标必须显式：隐式 booted/默认 bundle 在真机上会打错目标
        raise ValueError("drive/resume/unblock 必须同时提供 --udid 与 --bundle-id")
    return ProbeContext(udid=args.udid, bundle_id=args.bundle_id)


def run_start(container, plan_path: str) -> dict[str, Any]:
    """建会话（同计划同内容 hash 重建返回同一 session，幂等）。"""
    payload = _load_plan(plan_path)
    return start_session(container, payload).model_dump(mode="json")


async def run_drive(container, session_id: str, ctx: ProbeContext) -> dict[str, Any]:
    record = await drive_until_wait(container, session_id, ctx)
    return record.model_dump(mode="json")


async def run_resume(
    container, session_id: str, gate_id: str, ctx: ProbeContext
) -> dict[str, Any]:
    record = await resume_after_gate(container, session_id, gate_id, ctx)
    return record.model_dump(mode="json")


async def run_unblock(container, session_id: str, ctx: ProbeContext) -> dict[str, Any]:
    record = await unblock(container, session_id, ctx)
    return record.model_dump(mode="json")


def run_get(container, session_id: str) -> dict[str, Any]:
    return get_session(container, session_id).model_dump(mode="json")


def _print(snapshot: dict[str, Any]) -> None:
    print(json.dumps(snapshot, ensure_ascii=False, indent=2))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="TestPilot 混合分段会话驱动（hybrid-plan-v1 会话生命周期）")
    sub = parser.add_subparsers(dest="command", required=True)

    p_start = sub.add_parser("start", help="从计划 JSON 建/重建混合会话")
    p_start.add_argument("--plan", required=True, help="hybrid-plan-v1 计划文件路径")

    p_drive = sub.add_parser("drive", help="推进自动步到下一个等待点/终态")
    p_drive.add_argument("--session", required=True, help="hsess-<12hex> 会话 id")
    p_drive.add_argument("--udid", required=True, help="目标设备硬件 UDID")
    p_drive.add_argument("--bundle-id", required=True, help="被测 App bundle id")

    p_resume = sub.add_parser("resume", help="人工闸门裁决后续跑")
    p_resume.add_argument("--session", required=True)
    p_resume.add_argument("--gate", required=True, help="gate-<12hex> 闸门 id")
    p_resume.add_argument("--udid", required=True)
    p_resume.add_argument("--bundle-id", required=True)

    p_unblock = sub.add_parser("unblock", help="环境阻塞解除后续跑")
    p_unblock.add_argument("--session", required=True)
    p_unblock.add_argument("--udid", required=True)
    p_unblock.add_argument("--bundle-id", required=True)

    p_get = sub.add_parser("get", help="只读打印会话快照")
    p_get.add_argument("--session", required=True)

    # ws-run：USB 反向监听单用例全链路（注入→iproxy→驱动→xcodebuild→转录）。
    # 一期交互式 CLI 形态，不进 worker 队列（无 TTY 的 worker 进程 input() 不成立）。
    p_ws = sub.add_parser("ws-run", help="USB 反向监听单用例会话（driver-first 全链路）")
    p_ws.add_argument("--gates", default=None,
                      help="闸门声明 JSON；缺省按 --structural 取 profile usb_relay.gates_*")
    p_ws.add_argument("--xctestrun", required=True,
                      help="build-for-testing 产出的 .xctestrun（一期不做 DerivedData 自动发现）")
    p_ws.add_argument("--udid", required=True, help="目标模拟器/真机 UDID，禁止隐式 booted")
    p_ws.add_argument("--only-testing", action="append", default=[],
                      help="Suite[/Class[/method]]，可重复；本系统只做选择执行")
    p_ws.add_argument("--structural", action="store_true",
                      help="结构跑：auto_values 自动裁决，CI/演示唯一可全自动的形态")
    p_ws.add_argument("--target", choices=("sim", "device"), default="sim")
    p_ws.add_argument("--transcript-note", default="", help="转录备注（不参与判定）")
    return parser


def _resolve_gates_path(container, args: argparse.Namespace) -> Path:
    if args.gates:
        return Path(args.gates)
    key = "gates_structural" if args.structural else "gates_human"
    configured = container.config.usb_relay.get(key)
    if not configured:
        raise ValueError(f"未显式 --gates 且 profile usb_relay.{key} 缺省")
    return Path(configured)


async def run_ws_run(container, args: argparse.Namespace) -> dict[str, Any]:
    """ws-run 子命令主体；成功/产品失败都返回快照（产品失败由 main 映射 exit 3）。"""
    req = WsRelayRequest(
        gates_path=_resolve_gates_path(container, args),
        xctestrun_path=Path(args.xctestrun),
        udid=args.udid,
        only_testing=list(args.only_testing),
        structural=bool(args.structural),
        target=args.target,
        transcript_note=args.transcript_note,
    )
    return await run_ws_relay(container, req)


def main(argv: list[str] | None = None) -> None:
    args = _build_parser().parse_args(argv)
    try:
        container = bootstrap("hybrid-worker")
        if args.command == "start":
            _print(run_start(container, args.plan))
            return
        if args.command == "get":
            _print(run_get(container, args.session))
            return

        if args.command == "ws-run":
            try:
                snapshot = asyncio.run(run_ws_run(container, args))
            except WsRelayProductFailure as exc:
                # 产品侧响亮失败（含 reject）：转录已落盘，快照照打，exit 3 与 0/2 区分。
                _print(exc.snapshot)
                print(f"[hybrid-worker] 产品失败: {exc}", file=sys.stderr)
                sys.exit(3)
            _print(snapshot)
            return

        ctx = _ctx(args)
        if args.command == "drive":
            snapshot = asyncio.run(run_drive(container, args.session, ctx))
        elif args.command == "resume":
            snapshot = asyncio.run(
                run_resume(container, args.session, args.gate, ctx))
        elif args.command == "unblock":
            snapshot = asyncio.run(run_unblock(container, args.session, ctx))
        else:  # pragma: no cover - argparse required 已兜住
            raise ValueError(f"未知子命令: {args.command}")
        _print(snapshot)
    except WsRelayHardError as exc:
        # ws-run 硬错误（协议/工具链/证据/重试窗）：exit 2，不产 JSON 快照。
        print(f"[hybrid-worker] 硬错误: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(2)
    except _CLI_ERRORS as exc:
        print(f"[hybrid-worker] 被拒: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
