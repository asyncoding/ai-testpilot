#!/usr/bin/env python3
"""真机链路手动探针（切片一，2026-11-13）：devicectl 盘点 → EnvironmentPort 真机实现
→ os_log 两步采集的**真实设备**验证。

定位与红线（与 hermetic 单测互补，不得挂进 verify_s0.sh / 任何自动门禁）：
- 本探针发起真实 ``xcrun devicectl`` 与（auto 模式下）``/usr/bin/log collect``，
  要求真机已配对、表态 connected；无设备环境必然失败，这是设计而非缺陷；
- 默认 ``--collect-mode manual``：平台**绝不代跑 collect**。归档缺失时探针以
  exit 2 收口并打印可照跑的人工命令（需要提权时人工自行加 sudo），人工采集到
  指定 .logarchive 后带同一 ``--run-id`` 重跑即可接上 show 步骤——对应用户现场
  约束："sudo /usr/bin/log collect 这种命令你无法直接跑，要人工介入"；
- 不跑 xcodebuild（签名/演示分支前置另见 docs/03 runbook），不做任何刺激动作，
  reset 守卫反而作为负向对照验证：真机抹除永远 PolicyDeniedError。

故事线（8 项检查）：
  ① devicectl list 真采集 + core 纯函数解析，清单打印（硬件 UDID/形态/连接态）；
  ② 目标 UDID 必须是 physical + connected，CoreDevice UUID 冒充硬件 UDID 查不到；
  ③ IosDeviceEnvironment.health_check 真跑 devicectl 回 True；清单外假 UDID 不起
     进程直接 False；
  ④ 租约真路径：acquire（含设备独占能力标签）→ env_id_for_lease → release 幂等；
  ⑤ reset 负向：PolicyDeniedError，零子进程，证明无任何抹除自动化；
  ⑥ 日志两步法：manual 缺归档 → 带人工命令的 AdapterUnavailableError（exit 2）；
     归档就位（或 auto 模式）→ log show 真跑，窗口/坏行/回显双 argv 落报告；
  ⑦ 假 UDID 日志负向：报错命令里绑定的就是假 UDID，fail-closed 不串设备；
  ⑧ 纯函数 argv 对账：collect 无 --end、sudo 只在 --use-sudo 时前置 sudo -n。

报告落 out/eval/device-chain-probe.{json,md}（out/ 已 gitignore）。
退出码：0 全过；1 检查失败；2 人工采集待办（manual 模式归档未就位，非失败）。

用法（在 subsystems/ 下）：
  # 首跑：盘点+健康+守卫，打印人工 collect 命令后 exit 2
  .venv/bin/python scripts/device_chain_probe.py
  # 人工在另一终端执行打印出的 collect 命令后，同 run-id 重跑接上 show
  .venv/bin/python scripts/device_chain_probe.py --run-id <首跑给出的 id>
  # 已确认本机免 sudo 可直接采集时：
  .venv/bin/python scripts/device_chain_probe.py --collect-mode auto
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages" / "workers" / "src"))

from testpilot_adapters.device_os_log import DeviceOsLogQueryAdapter  # noqa: E402
from testpilot_adapters.ios_device_env import (  # noqa: E402
    IosDeviceEnvironment,
    PhysicalDevice,
)
from testpilot_adapters.process_executor import SubprocessExecutor  # noqa: E402
from testpilot_core.devicectl_commands import (  # noqa: E402
    build_devicectl_list_argv,
    find_device,
    parse_devicectl_devices,
)
from testpilot_core.errors import (  # noqa: E402
    AdapterUnavailableError,
    PolicyDeniedError,
)
from testpilot_core.models import EnvironmentRequirements  # noqa: E402
from testpilot_core.oslog_commands import (  # noqa: E402
    COLLECT_MODE_AUTO,
    COLLECT_MODE_MANUAL,
    build_device_log_collect_argv,
)

SUBSYSTEMS = Path(__file__).resolve().parents[1]
EVAL_DIR = SUBSYSTEMS / "out" / "eval"
WORK_DIR = SUBSYSTEMS / "out" / "device-probe" / "logarchive"
# 与 config/profiles/ios-local-poc-device.yaml 同一台本机取证设备（可 --udid 覆盖）
DEFAULT_UDID = "00008140-001A39590162801C"
FAKE_UDID = "00000000-0000000000000000"
DEFAULT_PREDICATE = 'subsystem == "com.thames.alarmkitspike"'
_EXIT_HUMAN_ACTION = 2


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="真机环境/日志链路手动探针（勿入自动门禁）")
    parser.add_argument("--udid", default=DEFAULT_UDID, help="目标真机硬件 UDID")
    parser.add_argument(
        "--collect-mode", choices=(COLLECT_MODE_AUTO, COLLECT_MODE_MANUAL),
        default=COLLECT_MODE_MANUAL,
        help="manual（默认）：平台不代跑 collect，人工预采集；auto：平台直接采集",
    )
    parser.add_argument(
        "--use-sudo", action="store_true",
        help="auto 模式下 collect argv 前置 sudo -n（凭据未缓存立即失败，不挂密码提示）",
    )
    parser.add_argument("--window-minutes", type=int, default=5, help="日志窗口（分钟）")
    parser.add_argument("--predicate", default=DEFAULT_PREDICATE, help="NSPredicate 原文")
    parser.add_argument("--work-dir", default=str(WORK_DIR), help=".logarchive 落盘目录")
    parser.add_argument(
        "--run-id", default=None,
        help="采集 run id（默认 probe-<时间戳>）；manual 重跑必须复用首跑 id",
    )
    return parser.parse_args()


async def _run(args: argparse.Namespace) -> tuple[list[dict], list[str], dict, str | None]:
    checks: list[dict] = []
    failures: list[str] = []
    human_command: str | None = None
    evidence: dict = {"inventory": [], "log": None}

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "passed": "✅" if ok else "❌", "detail": detail})
        if not ok:
            failures.append(name)

    run_id = args.run_id or f"probe-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    now = datetime.now()
    start = (now - timedelta(minutes=args.window_minutes)).strftime("%Y-%m-%dT%H:%M:%S")
    end = now.strftime("%Y-%m-%dT%H:%M:%S")
    executor = SubprocessExecutor()

    # ① devicectl list 真采集 + core 纯函数解析
    listing = await executor.run(build_devicectl_list_argv())
    precheck_ok = listing.returncode == 0
    entries = []
    if precheck_ok:
        try:
            entries = parse_devicectl_devices(json.loads(listing.stdout))
        except Exception as exc:  # noqa: BLE001 探针要兜全量异常并落报告
            precheck_ok = False
            parse_error = f"{type(exc).__name__}: {exc}"
        else:
            parse_error = ""
    else:
        parse_error = f"rc={listing.returncode} stderr={listing.stderr[:200]}"
    evidence["inventory"] = [
        {"udid": e.udid, "name": e.name, "reality": e.reality,
         "connection_state": e.connection_state, "physical": e.is_physical,
         "connected": e.is_connected}
        for e in entries
    ]
    check(
        "① devicectl list 真采集且 JSON 经 core 纯函数解析",
        precheck_ok and bool(entries),
        f"rc={listing.returncode} devices={len(entries)} {parse_error}".rstrip(),
    )
    if not precheck_ok:
        # 盘点都不通，后续无意义，直接收口
        return checks, failures, evidence, human_command

    # ② 目标 UDID：physical + connected；CoreDevice UUID 查不到（find_device 只认硬件 UDID）
    target = find_device(entries, args.udid)
    target_ok = (
        target is not None and target.is_physical and target.is_connected
    )
    check(
        "② 目标硬件 UDID 命中 physical+connected 真机",
        target_ok,
        "未接入/非 physical/未 trusted" if target is None
        else f"{target.name} reality={target.reality} state={target.connection_state}",
    )
    uuid_lookup = find_device(entries, "9D3A93E6-73B7-5504-AA20-A4E6C5714D43")
    check(
        "②-负 CoreDevice UUID 不得被当作硬件 UDID 命中",
        uuid_lookup is None,
        "返回 None（双标识防串）" if uuid_lookup is None else "错误命中设备",
    )
    if target is None:
        return checks, failures, evidence, human_command

    # ③ 真机 EnvironmentPort：health_check 真跑 devicectl；假 UDID 不起进程即 False
    env = IosDeviceEnvironment(
        [PhysicalDevice(
            udid=target.udid, name=target.name,
            capabilities=["ios.device", "ui.automation", "background_audio",
                          "codesigning"],
        )],
        executor=executor,
    )
    healthy = await env.health_check(target.udid)
    check("③ health_check 真跑 devicectl 回 True", healthy,
          f"udid={target.udid} connected={target.is_connected}")

    # 清单外 UDID：适配器在查 inventory 阶段即返回 False（"零进程"由 hostless 单测守）
    fake_healthy = await env.health_check(FAKE_UDID)
    check("③-负 清单外假 UDID health_check 回 False",
          fake_healthy is False, f"healthy={fake_healthy}")

    # ④ 租约真路径（纯簿记，不触碰设备状态）
    lease = await env.acquire(EnvironmentRequirements(
        capabilities=["ios.device", "background_audio"],
        labels={"request_id": "device-chain-probe"},
    ))
    leased_udid = env.env_id_for_lease(lease)
    await env.release(lease)
    await env.release(lease)  # 幂等：重复释放不报错
    check(
        "④ acquire→env_id_for_lease→release 幂等（设备独占能力按标签放行）",
        leased_udid == target.udid and env.active_leases() == [],
        f"lease={lease} env_id={leased_udid} 剩余租约={len(env.active_leases())}",
    )

    # ⑤ reset 守卫：真机永远禁止自动抹除
    reset_denied = False
    try:
        await env.reset(target.udid)
    except PolicyDeniedError:
        reset_denied = True
    check("⑤ reset 永远 PolicyDeniedError（零抹除自动化）", reset_denied,
          "PolicyDeniedError 命中" if reset_denied else "reset 未被拒绝！")

    # ⑧ 纯函数 argv 对账（不发起进程）
    plain_argv = build_device_log_collect_argv(
        udid=target.udid, start=now - timedelta(minutes=args.window_minutes),
        output_path=str(Path(args.work_dir) / f"{run_id}.logarchive"),
    )
    sudo_argv = build_device_log_collect_argv(
        udid=target.udid, start=now - timedelta(minutes=args.window_minutes),
        output_path=str(Path(args.work_dir) / f"{run_id}.logarchive"),
        use_sudo=True,
    )
    udid_idx = plain_argv.index("--device-udid")
    argv_ok = (
        plain_argv[:2] == ["/usr/bin/log", "collect"]
        and "--end" not in plain_argv
        and plain_argv[udid_idx + 1] == target.udid
        and sudo_argv[:2] == ["sudo", "-n"]
        and sudo_argv[2:4] == ["/usr/bin/log", "collect"]
    )
    check("⑧ collect argv 对账：无 --end、UDID 显式绑定、sudo -n 仅在 use_sudo 时前置",
          argv_ok, f"plain={plain_argv[:4]}… sudo_head={sudo_argv[:2]}")

    # ⑥/⑦ 日志两步法（适配器真实进程出口）
    log_adapter = DeviceOsLogQueryAdapter(
        target.udid, args.work_dir,
        collect_mode=args.collect_mode, use_sudo=args.use_sudo,
        executor=executor,
    )
    try:
        log_result = await log_adapter.query(
            run_id=run_id, predicate=args.predicate,
            start_time=start, end_time=end,
        )
    except AdapterUnavailableError as exc:
        message = str(exc)
        if args.collect_mode == COLLECT_MODE_MANUAL and "manual 采集模式" in message:
            # 设计内的人工待办：提取命令，exit 2 区别于检查失败
            human_command = message
            check(
                "⑥ manual 缺归档 fail-closed 且携带人工 collect 命令",
                target.udid in message and "/usr/bin/log collect" in message,
                f"run_id={run_id}",
            )
            evidence["log"] = {"status": "awaiting_human_collect", "run_id": run_id,
                               "instruction": message}
        else:
            check(f"⑥ 日志两步采集（{args.collect_mode}）", False, message[:300])
    else:
        evidence["log"] = {
            "status": "ok", "run_id": run_id,
            "entries": len(log_result["entries"]),
            "dropped_out_of_window": log_result["dropped_out_of_window"],
            "bad_lines": log_result["bad_lines"],
            "archive_path": log_result["archive_path"],
            "collect_mode": log_result["query"]["collect_mode"],
            "sample_messages": [
                e.get("message", "")[:120] for e in log_result["entries"][:5]
            ],
        }
        archive_on_disk = Path(log_result["archive_path"]).exists()
        check(
            f"⑥ 日志两步采集（{args.collect_mode}）：show rc=0、归档在盘、窗口过滤生效",
            archive_on_disk
            and log_result["query"]["show_argv"][:2] == ["/usr/bin/log", "show"],
            f"entries={len(log_result['entries'])} "
            f"dropped={log_result['dropped_out_of_window']} "
            f"bad={log_result['bad_lines']} archive_on_disk={archive_on_disk}",
        )

    # ⑦ 假 UDID 负向：manual 模式下报错命令必须绑定假 UDID，不串到真机
    fake_adapter = DeviceOsLogQueryAdapter(
        FAKE_UDID, Path(args.work_dir) / "negative",
        collect_mode=COLLECT_MODE_MANUAL, executor=executor,
    )
    fake_loud = False
    fake_detail = ""
    try:
        await fake_adapter.query(
            run_id=f"neg-{run_id}", predicate=args.predicate,
            start_time=start, end_time=end,
        )
    except AdapterUnavailableError as exc:
        fake_loud = FAKE_UDID in str(exc) and "/usr/bin/log collect" in str(exc)
        fake_detail = str(exc)[:160]
    check("⑦-负 假 UDID 日志查询 fail-closed 且命令绑定假 UDID", fake_loud, fake_detail)

    return checks, failures, evidence, human_command


def _write_report(checks: list[dict], evidence: dict, status: str) -> Path:
    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    report = {
        "probe": "device_chain_probe",
        "generated_at": datetime.now().astimezone().isoformat(),
        "status": status,
        "summary": {
            "passed": sum(1 for c in checks if c["passed"] == "✅"),
            "failed": sum(1 for c in checks if c["passed"] == "❌"),
            "total": len(checks),
        },
        "evidence": evidence,
        "checks": checks,
    }
    out_json = EVAL_DIR / "device-chain-probe.json"
    out_json.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md = [
        "# 真机链路手动探针报告（device_chain_probe，切片一）", "",
        f"生成时间：{report['generated_at']}", f"状态：{status}", "",
        "| # | 检查 | 结果 | 证据 |", "|---|---|---|---|",
    ]
    md += [
        f"| {i} | {c['name']} | {c['passed']} | {c['detail']} |"
        for i, c in enumerate(checks, 1)
    ]
    (EVAL_DIR / "device-chain-probe.md").write_text(
        "\n".join(md) + "\n", encoding="utf-8")
    return out_json


def main() -> int:
    args = _parse_args()
    checks, failures, evidence, human_command = asyncio.run(_run(args))
    for c in checks:
        print(f"{c['passed']} {c['name']} — {c['detail']}")

    if failures:
        report = _write_report(checks, evidence, "failed")
        print(f"\n报告：{report}", file=sys.stderr)
        print("❌ device_chain_probe 存在失败检查", file=sys.stderr)
        return 1

    if human_command is not None:
        report = _write_report(checks, evidence, "awaiting_human_collect")
        print("\n⏳ 人工采集待办（平台不代提权）：", file=sys.stderr)
        print(f"\n{human_command}\n", file=sys.stderr)
        run_id = evidence["log"]["run_id"]
        print(
            "采集完成后带同一 run-id 重跑：\n"
            f"  .venv/bin/python scripts/device_chain_probe.py --run-id {run_id}",
            file=sys.stderr,
        )
        print(f"报告：{report}", file=sys.stderr)
        return _EXIT_HUMAN_ACTION

    report = _write_report(checks, evidence, "passed")
    print(f"\n报告：{report}")
    print("✅ device_chain_probe 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
