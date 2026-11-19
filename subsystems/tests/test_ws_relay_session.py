"""ws_relay_session 全链路编排器的 hermetic 测试（零真机/零端口/零 PlistBuddy）。

全假件纪律（同 test_xcode_runner 的 FakeProcess/FakeSpawner 形态）：
- 子进程层 FakeExecutor：spawn/spawn_attached 返回脚本化 FakeProcess，在 wait()
  时刻写驱动转录/证据/xcresult 目录，精确复现进程时序与产物；
- 注入薄接缝 prepare_injection 被 monkeypatch（真注入依赖 macOS PlistBuddy）；
- lsof 走 FakeExecutor.run 假返回；artifact/scm 走内存假件。

计划 §4 T6 五条强制红证在此永久钉死：
① 驱动 rc2 → xcodebuild 被 terminate 且 exit 2（protocol_error）；
② xcodebuild 无产物 → exit 2（hard_error）；
③ reject → exit 3 且转录 rejected 留痕，不存在任何冲绿路径；
④ iproxy 缺失 → exit 2 带 runbook 指引；
⑤ driver-first：spawn_attached(driver) 严格先于 spawn(xcodebuild)。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from testpilot_adapters.process_executor import CommandResult
from testpilot_core.errors import AdapterUnavailableError
from testpilot_workers import ws_gate_transcript as wt
from testpilot_workers import ws_relay_session as wrs
from testpilot_workers.hybrid_cli import main as cli_main
from testpilot_workers.ws_gate_driver import (
    DRIVER_TRANSCRIPT_SCHEMA,
    PROTOCOL_ID,
)
from testpilot_workers.ws_relay_session import (
    DEFAULT_TOKEN,
    DRIVER_MODULE,
    MIN_RETRY_SECONDS,
    TOKEN_ENV_VAR,
    WsRelayHardError,
    WsRelayProductFailure,
    WsRelayRequest,
    build_driver_argv,
    reconcile,
    resolve_settings,
    resolve_token,
    run_ws_relay,
    validate_request,
)

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "ws-gates"
GATES_STRUCTURAL = FIXTURE_DIR / "ws-gates.structural.json"
GATES_HUMAN = FIXTURE_DIR / "ws-gates.human.json"
_UDID = "5411787C-8AFF-4EBF-9000-EB5EFD1457D5"
_OID = "AlarmKitSpikeUITests/AgentQuickAlarmUITests/test_TCA1_lockRelayFullFlow"
_PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


# ── 假件 ────────────────────────────────────────────────────────────────────


class FakeProcess:
    """脚本化子进程：wait 时刻执行副作用（写转录/产物），terminate 只记不强杀。

    hang=True 建模"驱动先退时 xcodebuild 仍在运行"的真实时序：wait 阻塞到
    terminate 才返回。否则两个立即完成的假任务同 tick 进入 FIRST_COMPLETED
    集合，谁排第一只取决于 Task 对象哈希（随进程内任务计数奇偶翻转），
    driver-first 红证在单测隔离跑时会概率性翻车。
    """

    def __init__(self, kind: str, argv: list[str], rc: int, on_wait=None,
                 *, hang: bool = False) -> None:
        self.kind = kind
        self.argv = list(argv)
        self.pid = 99000 + hash(kind) % 1000
        self.terminated = False
        self._rc = rc
        self._on_wait = on_wait
        self._release = asyncio.Event() if hang else None

    async def wait(self) -> int:
        if self._release is not None:
            await self._release.wait()
        if self._on_wait:
            self._on_wait()
        return self._rc

    def terminate(self) -> None:
        self.terminated = True
        if self._release is not None:
            self._release.set()

    def interrupt(self) -> None:
        # 与 terminate 同语义：挂起进程也必须被唤醒，否则 wait 永久泄漏。
        self.terminate()


class _WaitProc:
    """reconcile 宽限超时用：不 terminate 就永不返回的进程。"""

    def __init__(self, rc: int) -> None:
        self._rc = rc
        self.terminated = False
        self._ev = asyncio.Event()

    async def wait(self) -> int:
        await self._ev.wait()
        return self._rc

    def terminate(self) -> None:
        self.terminated = True
        self._ev.set()


class FakeExecutor:
    """记录 spawn 时序；按进程种类分发脚本化场景。"""

    def __init__(
        self,
        *,
        driver_scenario: dict,
        xcode_scenario: dict,
        lsof_rc: int = 1,
        unavailable: frozenset = frozenset(),
    ) -> None:
        self._driver = driver_scenario
        self._xcode = xcode_scenario
        self.lsof_rc = lsof_rc
        self.unavailable = set(unavailable)
        self.order: list[str] = []
        self.processes: dict[str, FakeProcess] = {}
        self.driver_argv: list[str] | None = None
        self.xcode_argv: list[str] | None = None

    async def run(self, argv: list[str], *, cwd: str | None = None) -> CommandResult:
        assert argv[0] == "lsof"
        stdout = "COMMAND PID USER\nlsof 123" if self.lsof_rc == 0 else ""
        return CommandResult(
            returncode=self.lsof_rc, stdout=stdout, stderr="", argv=list(argv))

    async def spawn(self, argv: list[str], *, cwd: str | None = None) -> FakeProcess:
        kind = "iproxy" if argv[0] == "iproxy" else "xcodebuild"
        # 先记账再抛错：调用方要断言"确实尝试过 spawn iproxy"，
        # 工具缺失是尝试之后的结果，不能让时序证据被异常吞掉。
        self.order.append(f"spawn:{kind}")
        if kind in self.unavailable:
            raise AdapterUnavailableError(f"命令不存在（工具链未安装？）: {argv[0]}")
        proc = self._make(kind, argv)
        if kind == "xcodebuild":
            self.xcode_argv = list(argv)
        return proc

    async def spawn_attached(
        self, argv: list[str], *, cwd: str | None = None
    ) -> FakeProcess:
        assert argv[:3] == [sys.executable, "-m", DRIVER_MODULE]
        self.order.append("spawn_attached:driver")
        self.driver_argv = list(argv)
        return self._make("driver", argv)

    def _make(self, kind: str, argv: list[str]) -> FakeProcess:
        scenario = self._driver if kind == "driver" else self._xcode

        def _side_effect() -> None:
            if kind == "driver":
                self._driver_side_effect(argv, scenario)
            else:
                self._xcode_side_effect(argv, scenario)

        hang = bool(scenario.get("hang"))
        if hang:
            # 挂起进程的副作用提前到 spawn 时刻：真实 xcodebuild 的 result
            # bundle 本就早建、驱动转录在断链瞬间已落盘；wait 只建模"未退出"。
            _side_effect()
        proc = FakeProcess(kind, argv, scenario.get("rc", 0),
                           None if hang else _side_effect, hang=hang)
        self.processes[kind] = proc
        return proc

    @staticmethod
    def _opt(argv: list[str], flag: str) -> str:
        return argv[argv.index(flag) + 1]

    def _driver_side_effect(self, argv: list[str], scenario: dict) -> None:
        if scenario.get("skip_transcript"):
            return
        transcript = Path(self._opt(argv, "--transcript-out"))
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.write_text(
            json.dumps(_driver_doc(scenario), ensure_ascii=False), encoding="utf-8")
        if scenario.get("evidence_written", True):
            ev_dir = Path(self._opt(argv, "--evidence-dir"))
            ev_dir.mkdir(parents=True, exist_ok=True)
            for item in _driver_doc(scenario)["evidence"]:
                (ev_dir / item["saved_name"]).write_bytes(_PNG)

    def _xcode_side_effect(self, argv: list[str], scenario: dict) -> None:
        if scenario.get("create_bundle"):
            bundle = Path(self._opt(argv, "-resultBundlePath"))
            bundle.mkdir(parents=True, exist_ok=True)
            (bundle / "Info.plist").write_bytes(b"<plist/>")


def _driver_doc(scenario: dict) -> dict:
    outcome = scenario["outcome"]
    rejected = scenario.get("rejected", False)
    if rejected:
        gates = [{"gate_id": "g-lock", "verdict": "reject",
                  "values": {}, "rejected": True}]
    elif outcome == "fail_loud" or outcome == "connect_timeout":
        gates = []
    else:
        gates = [
            {"gate_id": "g-lock", "verdict": "approve", "values": {}, "rejected": False},
            {"gate_id": "g-sidebutton", "verdict": "approve",
             "values": {}, "rejected": False},
            {"gate_id": "g-sensory", "verdict": "approve",
             "values": {"confidence": "high",
                        "note": "AUTO structural fixture"}, "rejected": False},
        ]
    evidence = []
    if scenario.get("evidence_record", False):
        evidence = [{
            "seq": 1, "kind": "screenshot",
            "saved_name": "evidence-01-screenshot.png", "bytes": len(_PNG),
        }]
    close_code = scenario.get("close_code")
    return {
        "schema_version": DRIVER_TRANSCRIPT_SCHEMA,
        "protocol": PROTOCOL_ID,
        "session": "hsess-p0",
        "structural": True,
        "outcome": outcome,
        "reason": scenario.get("reason", ""),
        "stats": {"hb": 3, "gates": [g["gate_id"] for g in gates],
                  "rejected": [g["gate_id"] for g in gates if g["rejected"]]},
        "gate_records": gates,
        "evidence": evidence,
        "close_code": close_code,
        "finished_at": "2026-11-18T10:00:00Z",
    }


class FakeConfig:
    def __init__(self, usb_relay: dict) -> None:
        self.usb_relay = usb_relay
        self.profile = "ios-local-poc-xcode"


class FakeArtifact:
    def __init__(self) -> None:
        self.puts: list[tuple[str, dict]] = []

    async def put(self, local_path: str, metadata: dict) -> str:
        self.puts.append((local_path, dict(metadata)))
        return f"urn:testpilot:localfs:{Path(local_path).name}"


class FakeScm:
    async def head_sha(self, repo) -> str:
        return "0123456789abcdef0123456789abcdef01234567"


class FakeContainer:
    def __init__(self, tmp_path: Path, usb_relay: dict | None = None,
                 *, with_artifact: bool = True, with_scm: bool = True) -> None:
        self.config = FakeConfig(usb_relay or {})
        self.data_dir = tmp_path / "data"
        self.data_dir.mkdir()
        self.artifact = FakeArtifact()
        adapters: dict = {}
        if with_artifact:
            adapters["artifact"] = self.artifact
        if with_scm:
            adapters["scm"] = FakeScm()
        self.adapters = adapters


# ── 夹具 ────────────────────────────────────────────────────────────────────


@pytest.fixture
def fake_injection(monkeypatch):
    """替换 PlistBuddy 实写接缝：hermetic 宿主不依赖 macOS 工具链。"""
    def _inject(source: Path, env: dict, suffix: str):
        injected = source.with_name(f"{source.stem}{suffix}")
        injected.write_bytes(b"FAKE-INJECTED-XCTESTRUN")
        return injected, hashlib.sha256(injected.read_bytes()).hexdigest(), sorted(env)
    monkeypatch.setattr(wrs, "prepare_injection", _inject)


_XCTESTRUN_NAME = "AlarmKitSpike_iphonesimulator27.0-arm64.xctestrun"


def _ensure_xctestrun(tmp_path: Path) -> Path:
    xctestrun = tmp_path / _XCTESTRUN_NAME
    xctestrun.write_bytes(b"<?xml version='1.0' encoding='UTF-8'?>")
    return xctestrun


def _req(tmp_path: Path, **overrides) -> WsRelayRequest:
    xctestrun = _ensure_xctestrun(tmp_path)
    params = dict(
        gates_path=GATES_STRUCTURAL,
        xctestrun_path=xctestrun,
        udid=_UDID,
        only_testing=[_OID],
        structural=True,
        target="sim",
    )
    params.update(overrides)
    return WsRelayRequest(**params)


def _executor(*, driver: dict, xcode: dict, **kw) -> FakeExecutor:
    return FakeExecutor(driver_scenario=driver, xcode_scenario=xcode, **kw)


_GREEN_DRIVER = {"outcome": "complete", "rc": 0, "close_code": 1000,
                 "evidence_record": True, "evidence_written": True}
_GREEN_XCODE = {"rc": 0, "create_bundle": True}


def _relay_sessions(data_dir: Path) -> list[Path]:
    root = data_dir / wt.RELAY_DIRNAME
    return sorted(p for p in root.glob("wssess-*")) if root.exists() else []


def _formal_transcript(container: FakeContainer):
    sessions = _relay_sessions(container.data_dir)
    assert len(sessions) == 1
    path = sessions[0] / wt.TRANSCRIPT_FILENAME
    return wt.load_transcript(path), path


# ── 1. 纯函数侧：settings/request/token/argv ─────────────────────────────────


def test_resolve_settings_defaults_merge_and_deep_copy():
    s = resolve_settings({})
    assert s["listen_port"] == 9876 and s["retry_seconds"] == 300
    assert s["iproxy"] == {"enabled": False, "local_port": 9876, "device_port": 9876}
    custom = resolve_settings({"listen_port": 11111,
                               "iproxy": {"enabled": True, "local_port": 22222}})
    assert custom["listen_port"] == 11111
    assert custom["iproxy"] == {"enabled": True, "local_port": 22222,
                                "device_port": 9876}
    # 深拷贝：默认常量不被入参污染
    again = resolve_settings({})
    assert again["iproxy"]["enabled"] is False


@pytest.mark.parametrize("bad", [
    {"retry_seconds": 299},
    {"grace_seconds": 0},
    {"listen_port": 0},
    {"listen_port": 70000},
    {"iproxy": {"local_port": "9876"}},
    {"time_allowance_s": 0},
    {"time_allowance_s": True},
])
def test_resolve_settings_closed_set_rejects(bad):
    with pytest.raises(WsRelayHardError):
        resolve_settings(bad)


def test_resolve_settings_retry_floor_uses_named_constant():
    with pytest.raises(WsRelayHardError, match=str(MIN_RETRY_SECONDS)):
        resolve_settings({"retry_seconds": MIN_RETRY_SECONDS - 1})


def test_validate_request_target_and_only_testing_closed(tmp_path):
    req = _req(tmp_path, target="device")
    bad_target = WsRelayRequest(
        gates_path=req.gates_path, xctestrun_path=req.xctestrun_path,
        udid=req.udid, only_testing=req.only_testing, structural=True,
        target="watch")
    with pytest.raises(WsRelayHardError, match=r"sim\|device"):
        validate_request(bad_target)
    with pytest.raises(WsRelayHardError, match="only-testing"):
        validate_request(WsRelayRequest(
            gates_path=req.gates_path, xctestrun_path=req.xctestrun_path,
            udid=req.udid, only_testing=[], structural=True, target="sim"))
    with pytest.raises(WsRelayHardError, match="xctestrun 不存在"):
        validate_request(WsRelayRequest(
            gates_path=req.gates_path,
            xctestrun_path=tmp_path / "missing.xctestrun",
            udid=req.udid, only_testing=[_OID], structural=True, target="sim"))


def test_resolve_token_env_default_and_whitespace(monkeypatch):
    monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)
    token, is_default = resolve_token()
    assert token == DEFAULT_TOKEN and is_default is True
    monkeypatch.setenv(TOKEN_ENV_VAR, "secret-token-xyz")
    token, is_default = resolve_token()
    assert token == "secret-token-xyz" and is_default is False
    monkeypatch.setenv(TOKEN_ENV_VAR, "has space")
    with pytest.raises(WsRelayHardError, match="空白"):
        resolve_token()


def test_build_driver_argv_carries_frozen_wire_literals(tmp_path):
    argv = build_driver_argv(
        url="ws://127.0.0.1:9876/ws/hybrid/hsess-p0",
        wire_session="hsess-p0", token="tok-p0", gates_path=GATES_STRUCTURAL,
        structural=True, auto_delay=1.0, evidence_dir=tmp_path / "ev",
        retry_seconds=300, transcript_path=tmp_path / "dt.json")
    assert argv[:3] == [sys.executable, "-m", DRIVER_MODULE]
    joined = " ".join(argv)
    for literal in ("--url", "--session", "hsess-p0", "--token", "--gates",
                    "--structural", "--retry-seconds", "300", "--transcript-out"):
        assert literal in joined


# ── 2. reconcile first-exit 对账（纯假进程） ─────────────────────────────────


async def test_reconcile_driver_nonzero_first_terminates_xcode_immediately():
    # 红证 ① 的进程语义：驱动先非零退，xcodebuild 不等宽限直接 terminate。
    xcode = _WaitProc(rc=0)
    driver = FakeProcess("driver", [], rc=2)
    d_rc, x_rc, killed = await reconcile(
        driver, xcode, grace_seconds=30)
    assert (d_rc, x_rc) == (2, 0)
    assert killed == {"xcodebuild"} and xcode.terminated is True


async def test_reconcile_xcode_first_grace_timeout_kills_driver():
    driver = _WaitProc(rc=0)
    xcode = FakeProcess("xcodebuild", [], rc=66)
    d_rc, x_rc, killed = await reconcile(
        driver, xcode, grace_seconds=0.05)
    assert x_rc == 66 and killed == {"driver"} and driver.terminated is True


async def test_reconcile_green_race_both_natural_exit_no_kill():
    # 绿路毫秒级竞态：驱动先 rc0，xcode 在宽限内自然收口，不得误杀。
    xcode = FakeProcess("xcodebuild", [], rc=0)
    driver = FakeProcess("driver", [], rc=0)
    d_rc, x_rc, killed = await reconcile(
        driver, xcode, grace_seconds=5)
    assert (d_rc, x_rc) == (0, 0) and killed == set()
    assert xcode.terminated is False


# ── 3. 全链路：绿路 ──────────────────────────────────────────────────────────


async def test_green_flow_passed_with_transcript_urn_and_ordering(
        tmp_path, fake_injection):
    container = FakeContainer(tmp_path)
    executor = _executor(driver=dict(_GREEN_DRIVER), xcode=dict(_GREEN_XCODE))

    snapshot = await run_ws_relay(
        container, _req(tmp_path), executor=executor,
        now=datetime(2026, 11, 18, 10, 0, tzinfo=timezone.utc))

    assert snapshot["outcome"] == "passed" and snapshot["reason"] == ""
    # 红证 ⑤：driver-first 时序铁律
    assert executor.order == ["spawn_attached:driver", "spawn:xcodebuild"]
    # xcodebuild argv 来自 core 纯函数且只做选择执行
    xargv = executor.xcode_argv
    assert xargv[:2] == ["xcodebuild", "test-without-building"]
    assert "-xctestrun" in xargv and "-only-testing" in xargv
    assert xargv[xargv.index("-only-testing") + 1] == _OID

    transcript, path = _formal_transcript(container)
    assert transcript.outcome == "passed"
    assert transcript.protocol == "tp-hybrid-ws-p0"
    assert transcript.target == "sim" and transcript.channel == "loopback"
    assert transcript.structural is True
    assert transcript.commit_sha == "0123456789abcdef0123456789abcdef01234567"
    assert transcript.port_cleanup_verified is True
    # 三闸时序 + 证据 URN 真实回填
    assert [g.gate_id for g in transcript.gates] == [
        "g-lock", "g-sidebutton", "g-sensory"]
    assert len(transcript.evidence) == 1
    assert transcript.evidence[0].urn.startswith("urn:testpilot:localfs:")
    assert container.artifact.puts and container.artifact.puts[0][1] == {
        "type": "screenshot"}
    # 注入台账只记键名不记值；进程账本双 rc=0
    assert transcript.injection.env_keys == sorted([
        "TP_LISTEN_PORT", "TP_SESSION", "TP_TOKEN", "TP_TCA1_STRUCTURAL"])
    assert {(p.name, p.rc) for p in transcript.processes} == {
        ("driver", 0), ("xcodebuild", 0)}
    # 密钥红线：默认 token 字面量不得出现在正式转录任何位置
    assert DEFAULT_TOKEN not in path.read_text(encoding="utf-8")
    # 会话 id 闭集形态
    assert transcript.relay_session_id.startswith("wssess-20261118T100000Z-")
    # 证据文件真实非空
    ev_dir = path.parent / wt.EVIDENCE_DIRNAME
    assert (ev_dir / "evidence-01-screenshot.png").stat().st_size == len(_PNG)


async def test_port_residue_is_recorded_but_not_fatal(tmp_path, fake_injection):
    container = FakeContainer(tmp_path)
    executor = _executor(driver=dict(_GREEN_DRIVER), xcode=dict(_GREEN_XCODE),
                         lsof_rc=0)  # 仍有 LISTEN 残留
    snapshot = await run_ws_relay(container, _req(tmp_path), executor=executor)
    assert snapshot["outcome"] == "passed"
    assert snapshot["port_cleanup_verified"] is False
    assert "port_cleanup_detail" in snapshot["stats"]


# ── 4. 红证 ①：驱动 fail-loud → xcodebuild 被 terminate，exit 2 ──────────────


async def test_driver_fail_loud_terminates_xcode_and_hard_errors(
        tmp_path, fake_injection):
    container = FakeContainer(tmp_path)
    executor = _executor(
        driver={"outcome": "fail_loud", "rc": 2, "close_code": 1008,
                "reason": "auth rejected, hello={...}",
                "evidence_record": False, "evidence_written": False},
        # 驱动 fail-loud 时真实 xcodebuild 必然仍在跑：hang 住等 terminate，
        # 不让同 tick 集合顺序把 first-exit 撞成 xcodebuild。
        xcode={**_GREEN_XCODE, "hang": True})

    with pytest.raises(WsRelayHardError, match="fail-loud"):
        await run_ws_relay(container, _req(tmp_path), executor=executor)

    assert executor.processes["xcodebuild"].terminated is True
    assert executor.order == ["spawn_attached:driver", "spawn:xcodebuild"]
    transcript, _ = _formal_transcript(container)
    assert transcript.outcome == "protocol_error"
    # 被杀的 xcodebuild 在账本里 rc=None（编排器收口语义，不是信号负数）
    xcode_record = next(p for p in transcript.processes if p.name == "xcodebuild")
    assert xcode_record.rc is None
    driver_record = next(p for p in transcript.processes if p.name == "driver")
    assert driver_record.rc == 2 and driver_record.close_code == 1008


# ── 5. 红证 ②：xcodebuild 无产物 → hard_error exit 2 ─────────────────────────


async def test_xcode_failure_without_bundle_is_hard_error(tmp_path, fake_injection):
    container = FakeContainer(tmp_path)
    executor = _executor(
        driver=dict(_GREEN_DRIVER),
        xcode={"rc": 66, "create_bundle": False})

    with pytest.raises(WsRelayHardError, match="无产物"):
        await run_ws_relay(container, _req(tmp_path), executor=executor)

    transcript, _ = _formal_transcript(container)
    assert transcript.outcome == "hard_error"
    assert transcript.evidence[0].urn  # 证据链本身完整，失败分型在执行层


# ── 6. 红证 ③：reject → 产品失败 exit 3，rejected 留痕无冲绿路径 ─────────────


async def test_reject_is_product_failure_with_rejected_ledger(tmp_path,
                                                               fake_injection):
    container = FakeContainer(tmp_path)
    executor = _executor(
        driver={"outcome": "peer_closed", "rc": 1, "close_code": None,
                "rejected": True, "evidence_record": False,
                "evidence_written": False},
        xcode={"rc": 66, "create_bundle": True, "hang": True})

    with pytest.raises(WsRelayProductFailure) as exc_info:
        await run_ws_relay(container, _req(tmp_path), executor=executor)

    snapshot = exc_info.value.snapshot
    assert snapshot["outcome"] == "product_failure"
    assert "reject" in snapshot["reason"]
    transcript, path = _formal_transcript(container)
    assert transcript.outcome == "product_failure"
    assert transcript.gates[0].gate_id == "g-lock"
    assert transcript.gates[0].rejected is True
    assert transcript.gates[0].verdict == "reject"
    # reject 早夭本就无证据：豁免不报错，也不凭空造 URN
    assert transcript.evidence == [] and container.artifact.puts == []
    # 不存在冲绿路径：磁盘转录与返回快照双双非 passed
    assert json.loads(path.read_text(encoding="utf-8"))["outcome"] != "passed"
    assert driver_process_rc(transcript) == 1


async def test_peer_closed_without_reject_is_hard_error(tmp_path, fake_injection):
    # 无 reject 留痕的提前断链 = Runner 崩溃/环境故障，不能算产品失败。
    container = FakeContainer(tmp_path)
    executor = _executor(
        driver={"outcome": "peer_closed", "rc": 1, "close_code": None,
                "evidence_record": False, "evidence_written": False},
        xcode={"rc": 66, "create_bundle": True, "hang": True})
    with pytest.raises(WsRelayHardError, match="断链"):
        await run_ws_relay(container, _req(tmp_path), executor=executor)
    transcript, _ = _formal_transcript(container)
    assert transcript.outcome == "hard_error"


def driver_process_rc(transcript) -> int | None:
    return next(p.rc for p in transcript.processes if p.name == "driver")


# ── 7. 红证 ④：iproxy 缺失/关闭 → exit 2 带指引 ──────────────────────────────


async def test_iproxy_missing_has_runbook_guidance(tmp_path, fake_injection):
    container = FakeContainer(tmp_path, usb_relay={
        "iproxy": {"enabled": True, "local_port": 9876, "device_port": 9876}})
    executor = _executor(
        driver=dict(_GREEN_DRIVER), xcode=dict(_GREEN_XCODE),
        unavailable=frozenset({"iproxy"}))

    with pytest.raises(WsRelayHardError, match="iproxy"):
        await run_ws_relay(
            container, _req(tmp_path, target="device"), executor=executor)

    # 驱动/xcodebuild 一个都不许起：iproxy 是 device 路径的前置闸门
    assert executor.order == ["spawn:iproxy"]


async def test_device_target_blocked_when_iproxy_disabled(tmp_path,
                                                           fake_injection):
    container = FakeContainer(tmp_path)  # 默认 iproxy.enabled=false
    executor = _executor(driver=dict(_GREEN_DRIVER), xcode=dict(_GREEN_XCODE))
    # 文案必须指向正式 docs/03 §3.9（S9 重启后 runbook 03 已降级为批次历史叙事）
    with pytest.raises(
            WsRelayHardError,
            match=r"iproxy\.enabled=false.*docs/03 §3\.9"):
        await run_ws_relay(
            container, _req(tmp_path, target="device"), executor=executor)
    assert executor.order == []


# ── 8. 其余硬错误分支 ────────────────────────────────────────────────────────


async def test_connect_timeout_is_hard_error(tmp_path, fake_injection):
    container = FakeContainer(tmp_path)
    executor = _executor(
        driver={"outcome": "connect_timeout", "rc": 2, "close_code": None,
                "reason": "连不上 ws://127.0.0.1:9876/",
                "evidence_record": False, "evidence_written": False},
        xcode={**_GREEN_XCODE, "hang": True})
    with pytest.raises(WsRelayHardError, match="重试窗"):
        await run_ws_relay(container, _req(tmp_path), executor=executor)
    transcript, _ = _formal_transcript(container)
    assert transcript.outcome == "hard_error"


async def test_evidence_claim_without_file_fails_loud(tmp_path, fake_injection):
    # 驱动转录声称有证据但磁盘空壳：fail-loud，拒绝产出 passed。
    container = FakeContainer(tmp_path)
    executor = _executor(
        driver={"outcome": "complete", "rc": 0, "close_code": 1000,
                "evidence_record": True, "evidence_written": False},
        xcode=dict(_GREEN_XCODE))
    with pytest.raises(WsRelayHardError, match="证据"):
        await run_ws_relay(container, _req(tmp_path), executor=executor)


async def test_missing_driver_transcript_is_hard_error(tmp_path, fake_injection):
    container = FakeContainer(tmp_path)
    executor = _executor(
        driver={"outcome": "fail_loud", "rc": 2, "skip_transcript": True},
        xcode={**_GREEN_XCODE, "hang": True})
    with pytest.raises(WsRelayHardError, match="驱动转录缺失"):
        await run_ws_relay(container, _req(tmp_path), executor=executor)
    # 硬错误发生在正式转录写出之前：只留会话目录现场，不产半截 transcript.json
    sessions = _relay_sessions(container.data_dir)
    assert sessions and not (sessions[0] / wt.TRANSCRIPT_FILENAME).exists()


async def test_artifact_adapter_is_required(tmp_path, fake_injection):
    container = FakeContainer(tmp_path, with_artifact=False)
    executor = _executor(driver=dict(_GREEN_DRIVER), xcode=dict(_GREEN_XCODE))
    with pytest.raises(WsRelayHardError, match="artifact=localfs"):
        await run_ws_relay(container, _req(tmp_path), executor=executor)
    assert executor.order == []


async def test_env_token_never_serialized(tmp_path, fake_injection, monkeypatch):
    monkeypatch.setenv(TOKEN_ENV_VAR, "secret-token-xyz")
    container = FakeContainer(tmp_path)
    executor = _executor(driver=dict(_GREEN_DRIVER), xcode=dict(_GREEN_XCODE))

    snapshot = await run_ws_relay(container, _req(tmp_path), executor=executor)

    assert snapshot["outcome"] == "passed"
    sessions = _relay_sessions(container.data_dir)
    raw = (sessions[0] / wt.TRANSCRIPT_FILENAME).read_text(encoding="utf-8")
    assert "secret-token-xyz" not in raw
    # 进程 argv 带 token 是机制本身（子进程入参），但转录/夹具/快照都不可见
    assert "secret-token-xyz" not in json.dumps(snapshot)
    assert executor.driver_argv[
        executor.driver_argv.index("--token") + 1] == "secret-token-xyz"


# ── 9. CLI 挂载：0/2/3 退出码与单 JSON 快照纪律 ──────────────────────────────


def _wire_cli(monkeypatch, container: FakeContainer, executor: FakeExecutor):
    monkeypatch.setattr(
        "testpilot_workers.hybrid_cli.bootstrap", lambda _name: container)
    # CLI 走默认构造 SubprocessExecutor()，替换为全假件
    monkeypatch.setattr(wrs, "SubprocessExecutor", lambda: executor)


def _cli_argv(tmp_path: Path) -> list[str]:
    return [
        "ws-run", "--gates", str(GATES_STRUCTURAL), "--structural",
        "--xctestrun", str(_ensure_xctestrun(tmp_path)),
        "--udid", _UDID, "--only-testing", _OID,
    ]


def test_cli_ws_run_green_exit_0_json(tmp_path, fake_injection, monkeypatch,
                                      capsys):
    container = FakeContainer(tmp_path)
    executor = _executor(driver=dict(_GREEN_DRIVER), xcode=dict(_GREEN_XCODE))
    _wire_cli(monkeypatch, container, executor)

    rc = cli_main(_cli_argv(tmp_path))
    assert rc is None  # 成功路径不 sys.exit
    snapshot = json.loads(capsys.readouterr().out)
    assert snapshot["outcome"] == "passed"


def test_cli_ws_run_product_failure_exit_3_keeps_snapshot(
        tmp_path, fake_injection, monkeypatch, capsys):
    container = FakeContainer(tmp_path)
    executor = _executor(
        driver={"outcome": "peer_closed", "rc": 1, "rejected": True,
                "evidence_record": False, "evidence_written": False},
        xcode={"rc": 66, "create_bundle": True, "hang": True})
    _wire_cli(monkeypatch, container, executor)

    with pytest.raises(SystemExit) as exc:
        cli_main(_cli_argv(tmp_path))
    assert exc.value.code == 3
    captured = capsys.readouterr()
    snapshot = json.loads(captured.out)  # 产品失败仍打完整快照
    assert snapshot["outcome"] == "product_failure"
    assert "产品失败" in captured.err


def test_cli_ws_run_hard_error_exit_2_stderr(tmp_path, fake_injection,
                                             monkeypatch, capsys):
    container = FakeContainer(tmp_path)
    executor = _executor(
        driver=dict(_GREEN_DRIVER),
        xcode={"rc": 66, "create_bundle": False})
    _wire_cli(monkeypatch, container, executor)

    with pytest.raises(SystemExit) as exc:
        cli_main(_cli_argv(tmp_path))
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "硬错误" in captured.err


def test_cli_ws_run_gates_fallback_from_profile(tmp_path, fake_injection,
                                                monkeypatch, capsys):
    container = FakeContainer(tmp_path, usb_relay={
        "gates_structural": str(GATES_STRUCTURAL),
        "gates_human": str(GATES_HUMAN)})
    executor = _executor(driver=dict(_GREEN_DRIVER), xcode=dict(_GREEN_XCODE))
    _wire_cli(monkeypatch, container, executor)

    argv = _cli_argv(tmp_path)
    argv = [a for a in argv if a != "--gates" and a != str(GATES_STRUCTURAL)]
    cli_main(argv)
    snapshot = json.loads(capsys.readouterr().out)
    assert snapshot["outcome"] == "passed"
