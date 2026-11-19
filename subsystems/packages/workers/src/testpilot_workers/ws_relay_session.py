"""ws-run 全链路编排：USB 反向监听单用例会话（2026-11-18 转正切片，CLI-first）。

职责链（单前台 async 进程，driver-first 双进程生命周期）：

1. 读 profile ``usb_relay`` 松散块 + 闭集校验（retry 窗禁止调小、端口 1–65535）；
2. xctestrun 同目录副本注入 TP_* 线协议环境（token 走 TESTPILOT_RELAY_TOKEN，
   缺省 tok-p0 并 stderr 警告，token 永不进 yaml/转录）；
3. target=device 才起 iproxy（模拟器 profile 关闭；device profile 2026-11-18 起为
   S9 启用，正式 runbook 见 docs/03 §3.9；关闭态硬错误文案同指该节）；
4. 先起闸门驱动（spawn_attached 继承 TTY，retry 窗等 Runner listener），
   再起 xcodebuild test-without-building（argv 只许来自 core 纯函数）；
5. first-exit 对账：驱动先非零退 → 杀 xcodebuild；任一先正常退 → 有界宽限
   等另一方收口，超时再杀；iproxy 最后收；
6. 读驱动转录 → 证据 fail-loud → LocalFsArtifact URN 回填 → 原子写正式转录；
7. 失败分型：WsRelayHardError→exit 2，WsRelayProductFailure→exit 3（含 reject）。

hermetic 纪律：所有外部动作走 executor/artifact Protocol 注入，测试用全假件
零真机零端口；注入与 lsof 各包一层薄函数，测试 monkeypatch 后跨平台 hostless。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from testpilot_adapters.process_executor import SubprocessExecutor
from testpilot_core.errors import AdapterUnavailableError
from testpilot_core.xcode_commands import (
    DESTINATION_PLATFORM_DEVICE,
    DESTINATION_PLATFORM_SIMULATOR,
    build_xcodebuild_xctestrun_argv,
)

from testpilot_workers import ws_gate_transcript as wt
from testpilot_workers.ws_gate_driver import (
    OUTCOME_CONNECT_TIMEOUT,
    OUTCOME_PEER_CLOSED,
    load_gates,
)
from testpilot_workers.ws_gate_transcript import (
    EvidenceRecord,
    GateRecord,
    InjectionRecord,
    ProcessRecord,
    TranscriptError,
    WsGateTranscript,
    assert_evidence_satisfied,
    backfill_evidence_urns,
    new_relay_session_id,
    parse_driver_transcript,
    write_transcript,
)
from testpilot_workers.xctestrun_inject import InjectionError, inject_file

MIN_RETRY_SECONDS = 300
DEFAULT_TOKEN = "tok-p0"
TOKEN_ENV_VAR = "TESTPILOT_RELAY_TOKEN"
DRIVER_MODULE = "testpilot_workers.ws_gate_driver"
_DRIVER_TRANSCRIPT_NAME = "driver-transcript.json"

_DEFAULTS = {
    "relay_url_template": "ws://127.0.0.1:{port}/ws/hybrid/{session}",
    "listen_port": 9876,
    "session": "hsess-p0",
    "retry_seconds": 300,
    "grace_seconds": 30,
    "auto_delay": 1.0,
    "structural_default": False,
    "time_allowance_s": 600,
    "evidence_subdir": wt.RELAY_DIRNAME,
    "iproxy": {"enabled": False, "local_port": 9876, "device_port": 9876},
    "xctestrun": {"output_suffix": ".injected.xctestrun"},
}


class WsRelayHardError(Exception):
    """硬错误（协议/工具链/证据/重试窗）：CLI 映射 exit 2。"""


class WsRelayProductFailure(Exception):
    """产品侧响亮失败（xcodebuild 非零且有产物，含 reject）：CLI 映射 exit 3。"""

    def __init__(self, message: str, *, snapshot: dict[str, Any]) -> None:
        super().__init__(message)
        self.snapshot = snapshot


@dataclass
class WsRelayRequest:
    gates_path: Path
    xctestrun_path: Path
    udid: str
    only_testing: list[str]
    structural: bool
    target: str  # "sim" | "device"
    transcript_note: str = ""


@dataclass
class _ProcessLedger:
    """进程对账累积器：先于任何外部副作用创建，硬错误路径也能产出完整转录。"""

    records: list[ProcessRecord] = field(default_factory=list)
    started: dict[str, float] = field(default_factory=dict)

    def mark_start(self, name: str) -> None:
        self.started[name] = time.monotonic()

    def record(self, name: str, rc: int | None, *, close_code: int | None = None) -> None:
        duration = time.monotonic() - self.started.get(name, time.monotonic())
        self.records.append(ProcessRecord(
            name=name, rc=rc, duration_s=round(duration, 3), close_code=close_code))


# ── 配置与入参校验（纯函数侧） ───────────────────────────────────────────────


def resolve_settings(config_usb_relay: dict[str, Any]) -> dict[str, Any]:
    """默认值 + profile 覆盖 + 闭集校验；老 profile 空字典得到全套默认。"""
    raw = config_usb_relay or {}
    settings: dict[str, Any] = json.loads(json.dumps(_DEFAULTS))  # 深拷贝
    settings.update(raw)
    settings["iproxy"] = {**_DEFAULTS["iproxy"], **raw.get("iproxy", {})}
    settings["xctestrun"] = {**_DEFAULTS["xctestrun"], **raw.get("xctestrun", {})}

    if not isinstance(settings["retry_seconds"], (int, float)) \
            or settings["retry_seconds"] < MIN_RETRY_SECONDS:
        raise WsRelayHardError(
            f"usb_relay.retry_seconds 不得小于 {MIN_RETRY_SECONDS}s（driver-first 纪律）")
    if not isinstance(settings["grace_seconds"], (int, float)) \
            or settings["grace_seconds"] <= 0:
        raise WsRelayHardError("usb_relay.grace_seconds 必须是正数")
    _require_port(settings["listen_port"], "usb_relay.listen_port")
    _require_port(settings["iproxy"]["local_port"], "usb_relay.iproxy.local_port")
    _require_port(settings["iproxy"]["device_port"], "usb_relay.iproxy.device_port")
    # bool 是 int 子类，显式挡掉 True/False 手滑值（同 T1 allowance 校验纪律）
    if isinstance(settings["time_allowance_s"], bool) \
            or not isinstance(settings["time_allowance_s"], int) \
            or settings["time_allowance_s"] <= 0:
        raise WsRelayHardError("usb_relay.time_allowance_s 必须是正整数")
    return settings


def _require_port(value: Any, label: str) -> None:
    if not isinstance(value, int) or not 1 <= value <= 65535:
        raise WsRelayHardError(f"{label} 必须是 1–65535 的整数：{value!r}")


def validate_request(req: WsRelayRequest) -> None:
    if req.target not in ("sim", "device"):
        raise WsRelayHardError(f"--target 只允许 sim|device：{req.target!r}")
    if not req.only_testing or not all(isinstance(x, str) and x for x in req.only_testing):
        raise WsRelayHardError("--only-testing 至少声明一个非空标识")
    if not req.gates_path.is_file():
        raise WsRelayHardError(f"闸门声明文件不存在：{req.gates_path}")
    if not req.xctestrun_path.is_file():
        raise WsRelayHardError(
            f"xctestrun 不存在（一期不做 DerivedData 自动发现）：{req.xctestrun_path}")


def resolve_token() -> tuple[str, bool]:
    """返回 (token, is_default)；缺省回退线协议默认并要求调用方打警告。"""
    token = os.environ.get(TOKEN_ENV_VAR)
    if token:
        if any(c.isspace() for c in token):
            raise WsRelayHardError(f"{TOKEN_ENV_VAR} 含空白字符，拒绝注入")
        return token, False
    return DEFAULT_TOKEN, True


# ── 薄封装接缝（测试 monkeypatch 这两个函数实现跨平台 hostless） ──────────────


def prepare_injection(
    source: Path, env: dict[str, str], suffix: str
) -> tuple[Path, str, list[str]]:
    """同目录副本注入，返回 (副本路径, sha256, 键名清单)；失败统一硬错误。"""
    injected = source.with_name(f"{source.stem}{suffix}")
    try:
        inject_file(source, injected, env)
    except InjectionError as exc:
        raise WsRelayHardError(f"xctestrun 注入失败：{exc}") from exc
    digest = hashlib.sha256(injected.read_bytes()).hexdigest()
    return injected, digest, sorted(env)


async def check_port_listener(executor, port: int) -> tuple[bool, str]:
    """lsof 对账：返回 (是否已无 LISTEN, 详情)。工具缺失/执行失败只转录不致命。"""
    try:
        result = await executor.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"])
    except AdapterUnavailableError as exc:
        return False, f"lsof 不可用：{exc}"
    if result.returncode == 0:
        return False, result.stdout.strip()[:500]
    return True, "no listener"


def build_driver_argv(
    *, url: str, wire_session: str, token: str, gates_path: Path,
    structural: bool, auto_delay: float, evidence_dir: Path,
    retry_seconds: float, transcript_path: Path,
) -> list[str]:
    argv = [
        sys.executable, "-m", DRIVER_MODULE,
        "--url", url,
        "--session", wire_session,
        "--token", token,
        "--gates", str(gates_path),
        "--auto-delay", str(auto_delay),
        "--evidence-dir", str(evidence_dir),
        "--retry-seconds", str(retry_seconds),
        "--transcript-out", str(transcript_path),
    ]
    if structural:
        argv.append("--structural")
    return argv


# ── first-exit 对账 ──────────────────────────────────────────────────────────


async def reconcile(
    driver_proc, xcode_proc, *, grace_seconds: float
) -> tuple[int, int, set[str]]:
    """双进程 first-exit 对账，返回 (driver_rc, xcode_rc, 被编排器杀掉的进程名)。

    - 驱动先**非零**退（协议红证）：xcodebuild 立即 terminate，不浪费宽限；
    - 任一先正常退：有界宽限等另一方自然收口（绿路常有毫秒级先后竞态），
      超时才 terminate；
    - 被杀进程在转录里 rc=None，保留"编排器收口"语义而非信号负数。
    """
    killed: set[str] = set()
    driver_task = asyncio.create_task(driver_proc.wait())
    xcode_task = asyncio.create_task(xcode_proc.wait())
    table = {
        id(driver_task): ("driver", xcode_task, xcode_proc),
        id(xcode_task): ("xcodebuild", driver_task, driver_proc),
    }

    done, _ = await asyncio.wait(
        {driver_task, xcode_task}, return_when=asyncio.FIRST_COMPLETED)
    first_task = next(iter(done))
    first_name, other_task, other_proc = table[id(first_task)]
    first_rc = first_task.result()

    if first_name == "driver" and first_rc != 0:
        other_proc.terminate()
        killed.add("xcodebuild")
        other_rc = await other_task
        return first_rc, other_rc, killed

    try:
        # shield：超时只放弃等待，不取消底层 wait 任务，terminate 后由它自己
        # 收口返回 rc；裸 wait_for 会把 CancelledError 灌进子进程 wait。
        other_rc = await asyncio.wait_for(
            asyncio.shield(other_task), grace_seconds)
    except asyncio.TimeoutError:
        other_proc.terminate()
        killed.add("driver" if other_task is driver_task else "xcodebuild")
        other_rc = await other_task

    if first_name == "driver":
        return first_rc, other_rc, killed
    return other_rc, first_rc, killed


# ── 主入口 ───────────────────────────────────────────────────────────────────


def _utc_stamp(now: datetime) -> str:
    return now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def _best_effort_commit_sha(container) -> str:
    scm = container.adapters.get("scm")
    if scm is None or not hasattr(scm, "head_sha"):
        return "unknown"
    try:
        return await scm.head_sha(None)
    except Exception:
        return "unknown"


async def run_ws_relay(
    container,
    req: WsRelayRequest,
    *,
    executor=None,
    now: datetime | None = None,
) -> dict[str, Any]:
    validate_request(req)
    settings = resolve_settings(container.config.usb_relay)
    executor = executor or SubprocessExecutor()
    now = now or datetime.now(timezone.utc)
    ledger = _ProcessLedger()

    artifact = container.adapters.get("artifact")
    if artifact is None:
        raise WsRelayHardError(
            "ws-run 要求 artifact=localfs 适配器（证据 URN 回填），当前 profile 未装配")

    token, token_default = resolve_token()
    if token_default:
        print(f"[ws-run] 警告：未设置 {TOKEN_ENV_VAR}，回退线协议默认 token，"
              "仅限本地模拟器演示", file=sys.stderr)

    gates = load_gates(req.gates_path, structural=req.structural)
    require_evidence_count = sum(1 for g in gates if g.require_evidence)

    wire_session = settings["session"]
    port = settings["listen_port"]
    env = {
        "TP_LISTEN_PORT": str(port),
        "TP_SESSION": wire_session,
        "TP_TOKEN": token,
    }
    if req.structural:
        env["TP_TCA1_STRUCTURAL"] = "1"

    sid = new_relay_session_id(
        now, f"{wire_session}|{req.target}|{req.udid}|{'|'.join(req.only_testing)}")
    session_dir = wt.session_dir(container.data_dir, sid)
    evidence_dir = session_dir / wt.EVIDENCE_DIRNAME
    evidence_dir.mkdir(parents=True, exist_ok=True)
    driver_transcript_path = session_dir / _DRIVER_TRANSCRIPT_NAME
    result_bundle = container.data_dir / "xcresult-ws" / f"{sid}.xcresult"
    result_bundle.parent.mkdir(parents=True, exist_ok=True)
    if result_bundle.exists():
        shutil.rmtree(result_bundle)

    # 1) 注入：会话目录先于外部副作用建好，崩溃也留可对账现场
    injected_path, injected_sha, env_keys = prepare_injection(
        req.xctestrun_path, env, settings["xctestrun"]["output_suffix"])

    # 2) iproxy（仅 device）：模拟器 profile 保持关闭（sim 共享 Mac loopback 不需要），
    #    ios-local-poc-device 已为 S9 真机验收启用（2026-11-18 用户指令重启 S9）。
    iproxy_proc = None
    if req.target == "device":
        if not settings["iproxy"]["enabled"]:
            raise WsRelayHardError(
                "device 路径未启用：当前 profile usb_relay.iproxy.enabled=false；"
                "真机验收改用 config/profiles/ios-local-poc-device.yaml"
                "（iproxy 已启用），正式 runbook 见 docs/03 §3.9")
        iproxy_argv = [
            "iproxy", str(settings["iproxy"]["local_port"]),
            str(settings["iproxy"]["device_port"]),
        ]
        try:
            ledger.mark_start("iproxy")
            iproxy_proc = await executor.spawn(iproxy_argv)
        except AdapterUnavailableError as exc:
            raise WsRelayHardError(
                f"iproxy 起不来（brew install libimobiledevice？）：{exc}") from exc

    url_port = (settings["iproxy"]["local_port"] if req.target == "device"
                else port)
    url = settings["relay_url_template"].format(port=url_port, session=wire_session)
    driver_argv = build_driver_argv(
        url=url, wire_session=wire_session, token=token,
        gates_path=req.gates_path, structural=req.structural,
        auto_delay=float(settings["auto_delay"]), evidence_dir=evidence_dir,
        retry_seconds=float(settings["retry_seconds"]),
        transcript_path=driver_transcript_path,
    )
    xcode_argv = build_xcodebuild_xctestrun_argv(
        xctestrun_path=str(injected_path),
        udid=req.udid,
        only_testing=list(req.only_testing),
        result_bundle_path=str(result_bundle),
        destination_platform=(DESTINATION_PLATFORM_DEVICE if req.target == "device"
                              else DESTINATION_PLATFORM_SIMULATOR),
        maximum_test_execution_time_allowance=settings["time_allowance_s"],
    )

    # 3) driver-first：驱动先起（retry 窗兜住 xcodebuild 启动墙钟），xcodebuild 后起
    try:
        ledger.mark_start("driver")
        driver_proc = await executor.spawn_attached(driver_argv)
        ledger.mark_start("xcodebuild")
        xcode_proc = await executor.spawn(xcode_argv)
        driver_rc, xcode_rc, killed = await reconcile(
            driver_proc, xcode_proc,
            grace_seconds=float(settings["grace_seconds"]))
    finally:
        if iproxy_proc is not None:
            iproxy_proc.terminate()
            ledger.record("iproxy", None)

    # 4) 驱动转录是对账事实源；缺失即硬错误
    if not driver_transcript_path.is_file():
        raise WsRelayHardError(
            f"驱动转录缺失（驱动可能被强杀）：{driver_transcript_path}")
    driver_doc = parse_driver_transcript(
        json.loads(driver_transcript_path.read_text(encoding="utf-8")))

    ledger.record("driver", None if "driver" in killed else driver_rc,
                  close_code=driver_doc.get("close_code"))
    ledger.record("xcodebuild", None if "xcodebuild" in killed else xcode_rc)

    rejected = any(bool(r.get("rejected")) for r in driver_doc["gate_records"])
    try:
        assert_evidence_satisfied(
            require_evidence_gate_count=require_evidence_count,
            driver_transcript=driver_doc, evidence_dir=evidence_dir)
    except TranscriptError as exc:
        raise WsRelayHardError(f"证据校验失败：{exc}") from exc

    evidence_records = [
        EvidenceRecord(
            seq=int(item["seq"]), kind=str(item["kind"]),
            saved_name=str(item["saved_name"]),
            size_bytes=int(item["bytes"]))
        for item in driver_doc["evidence"]
    ]
    gate_records = [
        GateRecord(
            gate_id=str(item["gate_id"]),
            verdict="reject" if item.get("rejected") else "approve",
            values=dict(item.get("values") or {}),
            rejected=bool(item.get("rejected")),
        )
        for item in driver_doc["gate_records"]
    ]
    transcript = WsGateTranscript(
        relay_session_id=sid,
        session=wire_session,
        profile=container.config.profile,
        commit_sha=await _best_effort_commit_sha(container),
        target=req.target,
        channel="usb" if req.target == "device" else "loopback",
        structural=req.structural,
        injection=InjectionRecord(
            source_xctestrun=str(req.xctestrun_path),
            injected_xctestrun=str(injected_path),
            injected_sha256=injected_sha,
            env_keys=env_keys,
        ),
        gates=gate_records,
        evidence=evidence_records,
        stats=dict(driver_doc.get("stats") or {}),
        processes=list(ledger.records),
        outcome="passed",  # 占位，下面分型后覆写
        reason=req.transcript_note,
        started_at=_utc_stamp(now),
        finished_at=_utc_stamp(datetime.now(timezone.utc)),
    )

    if evidence_records:
        transcript = await backfill_evidence_urns(
            transcript, evidence_dir=evidence_dir, artifacts=artifact)

    port_clean, port_detail = await check_port_listener(executor, url_port)

    outcome, reason = _classify_outcome(
        driver_doc=driver_doc, driver_rc=driver_rc, xcode_rc=xcode_rc,
        rejected=rejected, bundle_exists=result_bundle.exists())
    transcript = transcript.model_copy(update={
        "outcome": outcome, "reason": reason,
        "port_cleanup_verified": port_clean})
    if not port_clean:
        transcript.stats.setdefault("port_cleanup_detail", port_detail)

    write_transcript(container.data_dir, sid, transcript)
    snapshot = transcript.model_dump(mode="json")

    if outcome == "passed":
        return snapshot
    if outcome == "product_failure":
        raise WsRelayProductFailure(reason, snapshot=snapshot)
    raise WsRelayHardError(reason)


def _classify_outcome(
    *, driver_doc: dict, driver_rc: int, xcode_rc: int,
    rejected: bool, bundle_exists: bool,
) -> tuple[str, str]:
    driver_outcome = driver_doc.get("outcome")
    if driver_outcome == OUTCOME_CONNECT_TIMEOUT:
        return "hard_error", "驱动重试窗耗尽，Runner listener 从未就位"
    if driver_outcome == OUTCOME_PEER_CLOSED:
        # reject 的真实链路：XCTAssert 抛错 → XCTest 拆连接，驱动等不到
        # step_finished，peer_closed 转录里保留 rejected 闸门留痕。
        if rejected:
            if xcode_rc != 0 and bundle_exists:
                return "product_failure", \
                    "人工 reject 被 Runner 响亮失败（XCTAssert 红、连接拆断），reject 不自动冲绿"
            return "hard_error", "转录含 reject 但 xcodebuild 未报产品失败，现场矛盾拒绝收口"
        return "hard_error", "Runner 在 step_finished 前断链且无 reject 留痕，疑似进程崩溃/环境故障"
    if driver_rc == 2:
        return "protocol_error", \
            f"驱动 fail-loud（rc=2）：{str(driver_doc.get('reason', ''))[:300]}"
    if driver_rc != 0:
        return "hard_error", f"驱动未知退出码 rc={driver_rc}"
    if rejected:
        # 理论兜底：reject 后对端若仍礼貌走完 step_finished（hermetic 假件形态），
        # 同样按产品失败分型，reject 在任何链路下都不存在冲绿路径。
        if xcode_rc != 0 and bundle_exists:
            return "product_failure", \
                "人工 reject 被 Runner 响亮失败（serverFail），reject 不自动冲绿"
        return "hard_error", "转录含 reject 但 xcodebuild 未报产品失败，现场矛盾拒绝收口"
    if xcode_rc != 0:
        if bundle_exists:
            return "product_failure", f"xcodebuild rc={xcode_rc} 且有产物，产品侧失败"
        return "hard_error", f"xcodebuild rc={xcode_rc} 且无产物，工具链/环境硬错误"
    return "passed", ""
