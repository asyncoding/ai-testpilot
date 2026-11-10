#!/usr/bin/env python3
"""S3.2 iOS xcode 适配器与环境租约端到端探针（docs/08 FR-EX-003/FR-EX-004，A20）。

与单测的分工：test_xcode_* / test_ios_sim_env / test_execution_env_lease 用
tmp 钉死每条分支；本探针走**生产装配路径**（load_profile → build_container →
workers.execution_service），在临时工作目录里把真实 XcodeTestRunner /
SimctlEnvironment 的进程出口全部换成 FakeExecutor/FakeSpawner，验证：
零 xcodebuild、零 simctl、零端口、零网络、零真机状态变更（绝不 erase 用户设备）。

故事线（10 项）：
  ① core argv 逐字对账 docs/03 §3：hostless 套件/单条 XCUITest 两形态、
     simctl shutdown→erase 固定顺序、注入串与空选择 fail-closed；
  ② 工厂装配：xcode + ios-simulator 真容器分支、未知 type 拒绝、空设备清单
     装配期即响、stub 剖面 environment 缺席；
  ③ resultBundlePath 预清理（FR-EX-002 AC3）：陈旧 .xcresult 在 spawn 前删除；
  ④ 能力拒绝不硬跑：真机独占能力 → UnsupportedCapabilityError 含"未覆盖层"，
     零 simctl 调用、零租约幻影；
  ⑤ 服务层"先租后跑"全链：acquire→udid/request_id 透传→收尾 release；
  ⑥ 租约泄漏回收：时钟拨过有效期 → reclaim 触发 shutdown 且设备可再分配；
  ⑦ reset 守卫与顺序：持租约 PolicyDenied 零命令；释放后 shutdown→erase 逐字；
     erase 失败翻译成 AdapterUnavailableError；
  ⑧ cancel 杀子进程：terminate 到位、重复/未知 id 幂等；
  ⑨ 超时出口也释放租约（设备泄漏防线在超时路径同样生效）；
  ⑩ 无 udid 拒跑（无租约不裸跑 xcodebuild）+ 占位 parser 函数自身仍 fail-closed
     （S3.3 起工厂已换真实 XcresultToolResultParser，本项验证库内占位行为不变）。

报告落 out/eval/xcode-adapter-probe.{json,md}（out/ 已 gitignore）。任一断言不符 exit 1。

用法（在 subsystems/ 下）：
  .venv/bin/python scripts/xcode_adapter_probe.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages" / "workers" / "src"))

from testpilot_adapters.ios_simulator_env import (  # noqa: E402
    SimctlEnvironment,
    SimulatorDevice,
)
from testpilot_adapters.store import bootstrap_store  # noqa: E402
from testpilot_adapters.xcode_runner import (  # noqa: E402
    XcodeTestRunner,
    unavailable_xcresult_parser,
)
from testpilot_adapters.xcresult_tool import (  # noqa: E402
    XcresultToolResultParser,
)
from testpilot_core.config_schema import AdapterRef  # noqa: E402
from testpilot_core.enums import CaseStatus, RunStatus  # noqa: E402
from testpilot_core.errors import (  # noqa: E402
    AdapterUnavailableError,
    PolicyDeniedError,
    UnsupportedCapabilityError,
)
from testpilot_core.models import (  # noqa: E402
    CaseResult,
    CaseSelector,
    EnvironmentInfo,
    EnvironmentRequirements,
    TestExecutionRequest,
    TestRun,
)
from testpilot_core.xcode_commands import (  # noqa: E402
    build_xcodebuild_argv,
    simctl_reset_plan,
    validate_only_testing,
)
from testpilot_orchestrator.config_loader import load_profile  # noqa: E402
from testpilot_orchestrator.factory import build_container  # noqa: E402
from testpilot_workers import execution_service as svc  # noqa: E402

SUBSYSTEMS = Path(__file__).resolve().parents[1]
PROFILE = SUBSYSTEMS / "config" / "profiles" / "offline-demo.yaml"
EVAL_DIR = SUBSYSTEMS / "out" / "eval"
_UDID = "5411787C-8AFF-4EBF-9000-EB5EFD1457D5"
_WS = "AlarmKitSpike/AlarmKitSpike.xcworkspace"
_SCHEME = "AlarmKitSpike"


# ── 进程出口假件（与 tests/ 同形，探针内自带，零真实子进程）─────────────────────

class FakeResult:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeProcess:
    def __init__(self, argv):
        self.argv = argv
        self.pid = 4321
        self.terminated = False

    async def wait(self):
        return 0

    def terminate(self):
        self.terminated = True


class FakeExecutor:
    """run 走脚本化结果队列，spawn 产出可 terminate 的假进程；全 argv 留痕。"""

    def __init__(self, results=None):
        self.calls: list[list[str]] = []
        self.spawned: list[list[str]] = []
        self._results = list(results or [])

    async def run(self, argv, *, cwd=None):
        self.calls.append(list(argv))
        return self._results.pop(0) if self._results else FakeResult()

    async def spawn(self, argv, *, cwd=None):
        self.spawned.append(list(argv))
        return FakeProcess(argv)


async def _fake_parser(**kwargs):
    return TestRun(
        run_id="parser-internal",
        idempotency_key=kwargs["request"].idempotency_key,
        project=kwargs["request"].project,
        commit_sha=kwargs["request"].commit_sha,
        status=RunStatus.FAILED if kwargs["returncode"] != 0 else RunStatus.PASSED,
        environment=EnvironmentInfo(
            provider="ios-simulator", env_id=kwargs["env_id"]),
        case_results=[CaseResult(
            external_case_id="AlarmKitSpikeTests/t1",
            status=CaseStatus.PASSED, duration_ms=12)],
    )


class RecordingRunner:
    """服务层故事用假 runner：留证收到的 request labels，产出 passed。"""

    def __init__(self):
        self.requests: list[TestExecutionRequest] = []
        self.canceled: list[str] = []

    async def submit(self, request):
        self.requests.append(request)
        return "adapter-internal"

    async def get_run(self, run_id):
        request = self.requests[-1]
        return TestRun(
            run_id="adapter-internal",
            idempotency_key=request.idempotency_key,
            project=request.project,
            commit_sha=request.commit_sha,
            status=RunStatus.PASSED,
            environment=EnvironmentInfo(
                provider="ios-simulator",
                env_id=request.environment.labels.get("udid", "")),
            case_results=[CaseResult(
                external_case_id="AlarmKitSpikeTests/t1",
                status=CaseStatus.PASSED, duration_ms=5)],
        )

    async def cancel(self, run_id):
        self.canceled.append(run_id)


class SlowRunner:
    def __init__(self, sleep_s):
        self.sleep_s = sleep_s
        self.canceled: list[str] = []

    async def submit(self, request):
        return "slow-1"

    async def get_run(self, run_id):
        await asyncio.sleep(self.sleep_s)
        raise AssertionError("超时路径不应返回结果")

    async def cancel(self, run_id):
        self.canceled.append(run_id)


def _devices():
    return [
        SimulatorDevice(udid="UDID-A", name="iPhone 15",
                        capabilities=["ios.simulator", "ui.automation"]),
        SimulatorDevice(udid="UDID-B", name="iPhone SE",
                        capabilities=["ios.simulator"]),
    ]


def _request(request_id="req-1", *, caps=("ios.simulator",), udid="", test_ids=None):
    labels = {"request_id": request_id}
    if udid:
        labels["udid"] = udid
    return TestExecutionRequest(
        request_id=request_id,
        idempotency_key=f"idem-{request_id}",
        project="probe-xcode",
        selector=CaseSelector(test_ids=test_ids or ["AlarmKitSpikeTests/t1"]),
        commit_sha="deadbeef",
        environment=EnvironmentRequirements(capabilities=list(caps), labels=labels),
    )


async def _main_async() -> tuple[list[dict[str, str]], list[str], Path]:
    work = Path(tempfile.mkdtemp(prefix="xcode-adapter-probe-", dir=EVAL_DIR))
    checks: list[dict[str, str]] = []
    failures: list[str] = []

    def check(name, ok, detail):
        checks.append({"name": name, "passed": "✅" if ok else "❌", "detail": detail})
        if not ok:
            failures.append(f"{name}: {detail}")

    # ① core argv 逐字对账（docs/03 §3.1/3.2 + §3 注意事项）
    suite_argv = build_xcodebuild_argv(
        workspace=_WS, scheme=_SCHEME, udid=_UDID,
        only_testing=["AlarmKitSpikeTests"],
        result_bundle_path="/tmp/hostless.xcresult")
    expected_suite = [
        "xcodebuild", "test", "-workspace", _WS, "-scheme", _SCHEME,
        "-destination", f"platform=iOS Simulator,id={_UDID}",
        "-only-testing", "AlarmKitSpikeTests",
        "-resultBundlePath", "/tmp/hostless.xcresult",
    ]
    single_argv = build_xcodebuild_argv(
        workspace=_WS, scheme=_SCHEME, udid=_UDID,
        only_testing=["AlarmKitSpikeUITests/ProductFlowUITests/"
                      "test10_WakeForm_SoundPickedThenSwipeStillBlocked"],
        result_bundle_path="/tmp/ui.xcresult")
    injection_rejected = False
    try:
        validate_only_testing("AlarmKitSpikeTests; rm -rf /")
    except ValueError:
        injection_rejected = True
    empty_rejected = False
    try:
        build_xcodebuild_argv(
            workspace=_WS, scheme=_SCHEME, udid=_UDID,
            only_testing=[], result_bundle_path="/tmp/x.xcresult")
    except ValueError:
        empty_rejected = True
    reset_plan = simctl_reset_plan("UDID-A")
    check("① argv 对账：两形态逐字 + reset 固定 shutdown→erase + 注入/空选择 fail-closed",
          suite_argv == expected_suite
          and single_argv[single_argv.index("-only-testing") + 1].startswith(
              "AlarmKitSpikeUITests/")
          and injection_rejected and empty_rejected
          and reset_plan == [["xcrun", "simctl", "shutdown", "UDID-A"],
                             ["xcrun", "simctl", "erase", "UDID-A"]],
          f"suite={suite_argv == expected_suite} inject={injection_rejected} "
          f"empty={empty_rejected} reset={reset_plan}")

    # ② 工厂装配（真容器，进程出口未被触发即算 hermetic）
    config = load_profile(PROFILE)
    config.paths["data_dir"] = str(work / "data")
    config.adapters.runner = AdapterRef(type="xcode", settings={
        "workspace": _WS, "scheme": _SCHEME,
        "result_bundle_dir": str(work / "bundles")})
    config.adapters.environment = AdapterRef(type="ios-simulator", settings={
        "devices": [d.model_dump() for d in _devices()], "lease_timeout_s": 120})
    container = build_container(config, SUBSYSTEMS)
    runner = container.get("runner")
    env = container.get("environment")
    # S3.3 起工厂注入真实 xcresulttool parser（与容器共享 localfs 制品端口），
    # 旧 fail-closed 占位函数保留在库内，⑩ 仍直接注入验证其自身行为不变
    wired = isinstance(runner, XcodeTestRunner) and isinstance(env, SimctlEnvironment) \
        and isinstance(runner._parser, XcresultToolResultParser)  # noqa: SLF001
    container.get("store").close()

    bad_runner_cfg = load_profile(PROFILE)
    bad_runner_cfg.paths["data_dir"] = str(work / "bad-runner")
    bad_runner_cfg.adapters.runner = AdapterRef(type="xcode-cloud", settings={})
    unknown_runner = False
    try:
        build_container(bad_runner_cfg, SUBSYSTEMS)
    except ValueError:
        unknown_runner = True
    bad_env_cfg = load_profile(PROFILE)
    bad_env_cfg.paths["data_dir"] = str(work / "bad-env")
    bad_env_cfg.adapters.environment = AdapterRef(
        type="device-farm", settings={})
    unknown_env = False
    try:
        build_container(bad_env_cfg, SUBSYSTEMS)
    except ValueError:
        unknown_env = True
    empty_cfg = load_profile(PROFILE)
    empty_cfg.paths["data_dir"] = str(work / "empty-env")
    empty_cfg.adapters.environment = AdapterRef(
        type="ios-simulator", settings={"devices": []})
    empty_loud = False
    try:
        build_container(empty_cfg, SUBSYSTEMS)
    except AdapterUnavailableError:
        empty_loud = True
    stub_cfg = load_profile(PROFILE)
    stub_cfg.paths["data_dir"] = str(work / "stub")
    stub_container = build_container(stub_cfg, SUBSYSTEMS)
    stub_absent = stub_container.get("environment") is None
    stub_container.get("store").close()
    check("② 工厂：xcode/ios-simulator 装配、未知 type 拒绝、空清单装配期响、stub 缺席",
          wired and unknown_runner and unknown_env and empty_loud and stub_absent,
          f"wired={wired} unknown_runner={unknown_runner} unknown_env={unknown_env} "
          f"empty_loud={empty_loud} stub_absent={stub_absent}")

    # ③ resultBundlePath 预清理（FakeSpawner 在 spawn 时断言旧包已不存在）
    spawner = FakeExecutor()
    xcrunner = XcodeTestRunner(
        workspace=_WS, scheme=_SCHEME, repo_path=work / "repo",
        result_bundle_dir=work / "bundles-3",
        result_parser=_fake_parser, executor=spawner)
    from testpilot_core.execution import run_id_for

    req3 = _request("req-preclean", udid=_UDID)
    rid3 = run_id_for(req3.idempotency_key)
    stale = work / "bundles-3" / f"{rid3}.xcresult"
    stale.mkdir(parents=True)
    (stale / "old.dat").write_text("stale")
    await xcrunner.submit(req3)
    preclean_ok = not (stale / "old.dat").exists() and len(spawner.spawned) == 1 \
        and spawner.spawned[0][spawner.spawned[0].index("-resultBundlePath") + 1] \
        == str(stale)

    # ⑧ cancel 杀子进程（同 runner 继续用）：重复/未知 id 验幂等不抛
    await xcrunner.cancel(rid3)
    await xcrunner.cancel(rid3)
    await xcrunner.cancel("run-ghost")
    # 直接核对假进程 terminate 标记
    terminated_flag = xcrunner._jobs[rid3]["process"].terminated  # noqa: SLF001
    check("③ 预清理：陈旧 xcresult spawn 前删除且 argv 带 resultBundlePath",
          preclean_ok, f"stale_gone={not (stale / 'old.dat').exists()} "
          f"spawned={len(spawner.spawned)}")
    check("⑧ cancel：子进程 terminate 到位，重复/未知 id 幂等不抛",
          terminated_flag, f"terminated={terminated_flag}")

    # ④ 能力拒绝不硬跑
    exec4 = FakeExecutor()
    env4 = SimctlEnvironment(_devices(), executor=exec4)
    rejected = False
    try:
        await env4.acquire(EnvironmentRequirements(
            capabilities=["background_audio"], labels={"request_id": "req-cap"}))
    except UnsupportedCapabilityError as exc:
        rejected = "未覆盖层" in str(exc)
    check("④ 真机独占能力拒绝：含未覆盖层说明、零 simctl、零租约幻影",
          rejected and exec4.calls == [] and env4.active_leases() == [],
          f"rejected={rejected} simctl_calls={len(exec4.calls)} "
          f"leases={len(env4.active_leases())}")

    # ⑤ 服务层先租后跑全链（生产 SimctlEnvironment + 生产 execution_service）
    cfg5 = load_profile(PROFILE)
    cfg5.paths["data_dir"] = str(work / "data-5")
    c5 = build_container(cfg5, SUBSYSTEMS)
    exec5 = FakeExecutor()
    env5 = SimctlEnvironment(_devices(), executor=exec5)
    runner5 = RecordingRunner()
    c5.adapters["environment"] = env5
    c5.adapters["runner"] = runner5
    try:
        await svc.submit_execution(c5, _request("req-lease"))
        summary5 = await svc.run_once(c5)
        sent = runner5.requests[0]
        lease_ok = (
            summary5["outcome"] == RunStatus.PASSED.value
            and sent.environment.labels["udid"] == "UDID-A"
            and sent.environment.labels["request_id"] == "req-lease"
            and env5.active_leases() == []
        )
    finally:
        c5.get("store").close()
    check("⑤ 先租后跑：udid/request_id 透传进 runner，收尾租约必释放",
          lease_ok,
          f"outcome={summary5['outcome']} labels={sent.environment.labels} "
          f"active={len(env5.active_leases())}")

    # ⑥ 租约泄漏回收（拨钟：到期 → reclaim shutdown → 设备可再分配）
    t0 = datetime.now(timezone.utc)
    clock = [t0]
    exec6 = FakeExecutor()
    env6 = SimctlEnvironment(
        _devices(), executor=exec6, lease_timeout_s=300, now=lambda: clock[0])
    lease6 = await env6.acquire(
        EnvironmentRequirements(capabilities=["ios.simulator"],
                                 labels={"request_id": "req-reclaim"}))
    clock[0] = t0 + timedelta(seconds=299)
    early = await env6.reclaim_expired()
    clock[0] = t0 + timedelta(seconds=300)
    reclaimed = await env6.reclaim_expired()
    re_lease = await env6.acquire(
        EnvironmentRequirements(capabilities=["ios.simulator"],
                                 labels={"request_id": "req-reclaim-2"}))
    check("⑥ 过期租约回收：未到期不抢，到期 shutdown 且设备可再分配",
          early == [] and [l.lease_id for l in reclaimed] == [lease6]
          and exec6.calls == [["xcrun", "simctl", "shutdown", "UDID-A"]]
          and re_lease != lease6 and len(env6.active_leases()) == 1,
          f"early={len(early)} reclaimed={len(reclaimed)} calls={exec6.calls}")
    await env6.release(re_lease)

    # ⑦ reset 守卫与顺序
    exec7 = FakeExecutor()
    env7 = SimctlEnvironment(_devices(), executor=exec7)
    lease7 = await env7.acquire(
        EnvironmentRequirements(capabilities=["ios.simulator"],
                                 labels={"request_id": "req-reset"}))
    guarded = False
    try:
        await env7.reset("UDID-A")
    except PolicyDeniedError:
        guarded = True
    guard_zero_calls = exec7.calls == []  # 守卫必须挡在 simctl 之前
    await env7.release(lease7)
    await env7.reset("UDID-A")
    ordered = exec7.calls == [
        ["xcrun", "simctl", "shutdown", "UDID-A"],
        ["xcrun", "simctl", "erase", "UDID-A"],
    ]
    # erase 失败翻译：第二条命令 rc=1
    exec7b = FakeExecutor(results=[FakeResult(), FakeResult(returncode=1, stderr="boom")])
    env7b = SimctlEnvironment(_devices(), executor=exec7b)
    reset_fails_loud = False
    try:
        await env7b.reset("UDID-B")
    except AdapterUnavailableError:
        reset_fails_loud = True
    check("⑦ reset：持租约守卫零命令，释放后 shutdown→erase 逐字，失败显式报错",
          guarded and guard_zero_calls and ordered and reset_fails_loud,
          f"guarded={guarded} zero_calls={guard_zero_calls} ordered={ordered} "
          f"fail_loud={reset_fails_loud} calls={exec7.calls}")

    # ⑨ 超时出口也释放租约
    cfg9 = load_profile(PROFILE)
    cfg9.paths["data_dir"] = str(work / "data-9")
    cfg9.limits.request_timeout_s = 0.05
    c9 = build_container(cfg9, SUBSYSTEMS)
    env9 = SimctlEnvironment(_devices(), executor=FakeExecutor())
    slow9 = SlowRunner(sleep_s=0.3)
    c9.adapters["environment"] = env9
    c9.adapters["runner"] = slow9
    try:
        await svc.submit_execution(c9, _request("req-timeout-lease"))
        summary9 = await svc.run_once(c9)
        timeout_releases = (
            summary9["outcome"] == RunStatus.TIMEOUT.value
            and slow9.canceled == ["slow-1"]
            and env9.active_leases() == []
        )
    finally:
        c9.get("store").close()
    check("⑨ 超时：run=timeout/cancel 回调到位/租约照样释放不留设备泄漏",
          timeout_releases,
          f"outcome={summary9['outcome']} canceled={slow9.canceled} "
          f"active={len(env9.active_leases())}")

    # ⑩ 无 udid 拒跑 + parser 占位 fail-closed
    spawner10 = FakeExecutor()
    runner10 = XcodeTestRunner(
        workspace=_WS, scheme=_SCHEME, repo_path=work / "repo-10",
        result_bundle_dir=work / "bundles-10",
        result_parser=_fake_parser, executor=spawner10)
    no_udid_loud = False
    try:
        await runner10.submit(_request("req-no-udid", udid=""))
    except AdapterUnavailableError:
        no_udid_loud = True
    parser_loud = False
    try:
        await unavailable_xcresult_parser(
            request=_request("req-parser"), result_bundle_path=work / "x.xcresult",
            env_id=_UDID, returncode=0)
    except AdapterUnavailableError:
        parser_loud = True
    check("⑩ 无租约不裸跑（缺 udid 拒且零 spawn）；占位 parser 函数自身仍 fail-closed",
          no_udid_loud and spawner10.spawned == [] and parser_loud,
          f"no_udid={no_udid_loud} spawned={len(spawner10.spawned)} "
          f"parser_loud={parser_loud}")

    return checks, failures, work


def main() -> int:
    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    checks, failures, work = asyncio.run(_main_async())

    report_doc = {
        "probe": "xcode_adapter_probe",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "workdir": str(work),
        "summary": {
            "passed": sum(1 for c in checks if c["passed"] == "✅"),
            "failed": len(failures),
            "total": len(checks),
        },
        "checks": checks,
    }
    (EVAL_DIR / "xcode-adapter-probe.json").write_text(
        json.dumps(report_doc, ensure_ascii=False, indent=2), encoding="utf-8")
    md_lines = [
        "# S3.2 iOS xcode 适配器与环境租约探针报告"
        "（xcode_adapter_probe / FR-EX-003/004）",
        "",
        f"生成时间：{report_doc['generated_at']}",
        f"工作目录：`{work}`",
        "",
        "| # | 检查 | 结果 | 证据 |",
        "|---|---|---|---|",
    ]
    md_lines += [
        f"| {i} | {c['name']} | {c['passed']} | {c['detail']} |"
        for i, c in enumerate(checks, 1)
    ]
    (EVAL_DIR / "xcode-adapter-probe.md").write_text(
        "\n".join(md_lines) + "\n", encoding="utf-8")

    for c in checks:
        print(f"{c['passed']} {c['name']} — {c['detail']}")
    print(f"报告：{EVAL_DIR / 'xcode-adapter-probe.json'}")
    if failures:
        print("❌ xcode_adapter_probe 存在失败检查", file=sys.stderr)
        return 1
    print("✅ xcode_adapter_probe 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
