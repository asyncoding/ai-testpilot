"""testpilot-hybrid 驱动 CLI 的 hostless 测试（H4.2，docs/15 §8）。

零真机/零独立进程：monkeypatch 掉 hybrid_cli.bootstrap 返回 tmp_path 上的
hermetic 容器（真 FileGateStore/FileHybridSessionStore + 假 dh/dev/子 run），
用 monkeypatch sys.argv + SystemExit 的既有 CLI 测试口径覆盖——

① start/drive/resume/unblock/get 五子命令的 JSON 快照与退出码 0；
② 同计划 start 幂等（同 session_id、不重复账本）；
③ 硬错误（计划缺失/JSON 损坏/会话不存在）统一 exit 2 走 stderr；
④ argparse 缺参自身 exit 2；设备目标必须显式传参。
"""

from __future__ import annotations

import json
import struct
from datetime import datetime, timedelta, timezone

import pytest

from testpilot_adapters.gate_store import FileGateStore
from testpilot_adapters.hybrid_session_store import FileHybridSessionStore
from testpilot_core.enums import GateStatus, SessionStatus, StepActor, StepStatus
from testpilot_core.errors import AdapterUnavailableError
from testpilot_core.hybrid_plan import (
    HumanVerdict,
    PlanRepair,
    PlanStep,
    Precondition,
    VerdictField,
    VerdictForm,
    build_hybrid_plan,
)
from testpilot_core.models import Oracle, OracleType, TestCase
from testpilot_workers import gate_service
from testpilot_workers import hybrid_cli
from testpilot_workers.hybrid_runner import ChildRunRequest

_T0 = datetime.now(timezone.utc)
_UDID = "00008140-001A39590162801C"
_BUNDLE = "com.example.AlarmKitSpike"
_PNG_SIG = b"\x89PNG\r\n\x1a\n"


class FakeDhAx:
    def __init__(self, *, readiness_error=None) -> None:
        self._readiness_error = readiness_error
        self.actions: list[str] = []

    async def readiness(self):
        self.actions.append("readiness")
        if self._readiness_error:
            raise self._readiness_error
        return {"ok": True, "verb": "readiness", "screen_locked": False,
                "buttons": {"home": True, "screenshot": True,
                            "rotate_left": True},
                "windows": [{"width": 320, "height": 480}]}

    async def home(self):
        self.actions.append("home")
        return {"ok": True, "verb": "home"}

    async def rotate_left(self, times=1):
        self.actions.append(f"rotate_leftx{times}")
        return {"ok": True, "verb": "rotate-left", "times": times}


class FakeDevicectl:
    def __init__(self) -> None:
        self.shots = 0

    async def capture_screenshot(self, udid):
        self.shots += 1
        # 竖屏截图要过 core.png_probe 的 IHDR 校验，给结构合法的最小 PNG
        path = self._root / f"shot-{self.shots}.png"
        path.write_bytes(
            _PNG_SIG + struct.pack(">I4s", 13, b"IHDR")
            + struct.pack(">II", 1206, 2622) + b"\x00" * 5)
        return path

    async def launch_app(self, udid, bundle_id, *, arguments=None,
                         terminate_existing=False):
        return None

    async def terminate_pid(self, udid, pid, *, kill=False):
        return None

    def bind_root(self, path):
        self._root = path
        path.mkdir(parents=True, exist_ok=True)


class FakeChildRun:
    def __init__(self) -> None:
        self.requests: list[ChildRunRequest] = []

    async def submit(self, request: ChildRunRequest) -> str:
        self.requests.append(request)
        return f"run-{len(self.requests)}"


class _Box:
    def __init__(self, tmp_path, *, dh=None, dev=None, child_run=None):
        dev = dev if dev is not None else FakeDevicectl()
        dev.bind_root(tmp_path / "shots")
        self._adapters = {
            "dh_ax": dh if dh is not None else FakeDhAx(),
            "devicectl": dev,
            "hybrid_child_run": child_run if child_run is not None
            else FakeChildRun(),
            "gate_store": FileGateStore(tmp_path / "data"),
            "hybrid_session_store": FileHybridSessionStore(tmp_path / "data"),
        }

    def get(self, name):
        return self._adapters.get(name)


def _case() -> TestCase:
    return TestCase(
        urn="urn:testpilot:fs-cases:test_case:cases.md#TC-A1",
        external_id="TC-A1", title="锁屏响铃侧键接力",
        module="RoutineAlarm", priority="P0",
        steps=["清场排钟", "人工锁屏", "到点响铃", "侧键进接力页"],
        oracles=[Oracle(type=OracleType.LOG_ASSERTION,
                        expectation="os_log 响铃事件链", confidence=0.9)],
        lifecycle="active", exploratory=False)


def _verdict_form() -> VerdictForm:
    return VerdictForm(fields=[
        VerdictField(key="ringtone_heard", label="铃声", kind="boolean"),
        VerdictField(key="vibration_felt", label="震动", kind="boolean"),
        VerdictField(key="confidence", label="把握", kind="choice",
                     options=["high", "medium", "low"]),
    ])


def _six_steps() -> list[PlanStep]:
    return [
        PlanStep(
            step_id="s1-purge-and-schedule",
            actor=StepActor.AUTO_XCODEBUILD, action="run_selector",
            params={"only_testing":
                    ["AlarmKitSpikeUITests/testLockRelay_schedule"]},
            preconditions=[
                Precondition(kind="portrait_orientation",
                             repair=PlanRepair(verb="dh_rotate_left")),
                Precondition(kind="no_active_alarms",
                             repair=PlanRepair(verb="app_launch_purge_args")),
            ],
            evidence_bindings=["urn:testpilot:store:artifact:segA-xcresult"],
            idempotency_key="s1|TC-A1|schedule"),
        PlanStep(
            step_id="s2-baseline-shot",
            actor=StepActor.AUTO_DEVICECTL, action="screenshot",
            params={"bundle_id": _BUNDLE},
            idempotency_key="s2|TC-A1|baseline"),
        PlanStep(
            step_id="s3-human-lock",
            actor=StepActor.HUMAN_ACTION, action="lock_screen",
            prompt="请在 60 秒内按电源键锁屏，并确认屏幕熄灭",
            preconditions=[Precondition(
                kind="notification_permission", on_failure="blocked",
                repair=PlanRepair(verb="human_gate"))],
            idempotency_key="s3|TC-A1|lock"),
        PlanStep(
            step_id="s4-human-sidebutton",
            actor=StepActor.HUMAN_ACTION, action="press_side_button",
            prompt="闹钟响铃后按一次物理侧边键，然后解锁进入接力页",
            idempotency_key="s4|TC-A1|sidebutton"),
        PlanStep(
            step_id="s5-sensory-verdict",
            actor=StepActor.HUMAN_VERDICT, action="sensory_check",
            prompt="请确认刚才锁屏期间的铃声与震动表现",
            verdict_form=_verdict_form(),
            evidence_bindings=[
                "urn:testpilot:store:artifact:oslog-ring-chain",
                "urn:testpilot:store:artifact:relay-video"],
            idempotency_key="s5|TC-A1|verdict"),
        PlanStep(
            step_id="s6-relay-assert",
            actor=StepActor.AUTO_XCODEBUILD, action="run_selector",
            params={"only_testing":
                    ["AlarmKitSpikeUITests/testLockRelay_relayPage"]},
            evidence_bindings=["urn:testpilot:store:artifact:segB-xcresult"],
            idempotency_key="s6|TC-A1|relay"),
    ]


def _plan_file(tmp_path, *, steps=None) -> str:
    payload = build_hybrid_plan(
        case=_case(), profile="ios-local-poc-device", project="AlarmKitSpike",
        steps=steps if steps is not None else _six_steps(), now=_T0)
    path = tmp_path / "tca1-plan.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return str(path)


def _wire(monkeypatch, box: _Box) -> None:
    monkeypatch.setattr(hybrid_cli, "bootstrap", lambda _name: box)


def _run_cli(argv: list[str], capsys) -> dict:
    hybrid_cli.main(argv)
    out = capsys.readouterr()
    return json.loads(out.out)


# ── 1. start：建会话 + 幂等 ───────────────────────────────────────────────────


def test_cli_start_outputs_session_snapshot(tmp_path, monkeypatch, capsys):
    box = _Box(tmp_path)
    _wire(monkeypatch, box)

    snapshot = _run_cli(["start", "--plan", _plan_file(tmp_path)], capsys)

    assert snapshot["session_id"].startswith("hsess-")
    assert snapshot["checkpoint"]["status"] == SessionStatus.RUNNING.value
    assert snapshot["checkpoint"]["current_step_id"] == "s1-purge-and-schedule"
    assert snapshot["ledger"][0]["type"] == "session_started"
    assert len(snapshot["plan"]["steps"]) == 6


def test_cli_start_is_idempotent_same_session(tmp_path, monkeypatch, capsys):
    box = _Box(tmp_path)
    _wire(monkeypatch, box)
    plan = _plan_file(tmp_path)

    first = _run_cli(["start", "--plan", plan], capsys)
    second = _run_cli(["start", "--plan", plan], capsys)

    assert second["session_id"] == first["session_id"]
    starts = [e for e in second["ledger"] if e["type"] == "session_started"]
    assert len(starts) == 1


# ── 2. get：只读快照 ──────────────────────────────────────────────────────────


def test_cli_get_returns_snapshot(tmp_path, monkeypatch, capsys):
    box = _Box(tmp_path)
    _wire(monkeypatch, box)
    sid = _run_cli(["start", "--plan", _plan_file(tmp_path)],
                   capsys)["session_id"]

    snapshot = _run_cli(["get", "--session", sid], capsys)
    assert snapshot["session_id"] == sid
    assert snapshot["checkpoint"]["status"] == SessionStatus.RUNNING.value


# ── 3. drive：自动连环推进到前置人工闸门 ──────────────────────────────────────


def test_cli_drive_reaches_awaiting_human(tmp_path, monkeypatch, capsys):
    box = _Box(tmp_path)
    _wire(monkeypatch, box)
    sid = _run_cli(["start", "--plan", _plan_file(tmp_path)],
                   capsys)["session_id"]

    snapshot = _run_cli([
        "drive", "--session", sid, "--udid", _UDID,
        "--bundle-id", _BUNDLE], capsys)

    assert snapshot["checkpoint"]["status"] == \
        SessionStatus.AWAITING_HUMAN.value
    # s1 子 run（含 purge 启动参数留痕）与 s2 截图都已完成
    assert snapshot["checkpoint"]["step_statuses"]["s1-purge-and-schedule"] \
        == StepStatus.COMPLETED.value
    assert snapshot["checkpoint"]["step_statuses"]["s2-baseline-shot"] \
        == StepStatus.COMPLETED.value
    child = box.get("hybrid_child_run")
    assert len(child.requests) == 1
    assert child.requests[0].launch_arguments == ["--ak-purge-dates"]
    gates = gate_service.list_gates(box)
    assert len(gates) == 1
    assert gates[0].status is GateStatus.PENDING


# ── 4. resume：闸门裁决后续跑 ─────────────────────────────────────────────────


def test_cli_resume_after_gate_response(tmp_path, monkeypatch, capsys):
    box = _Box(tmp_path)
    _wire(monkeypatch, box)
    sid = _run_cli(["start", "--plan", _plan_file(tmp_path)],
                   capsys)["session_id"]
    _run_cli(["drive", "--session", sid, "--udid", _UDID,
              "--bundle-id", _BUNDLE], capsys)
    pre_gate = gate_service.list_gates(box)[0]
    gate_service.respond_gate(
        box, pre_gate.gate_id,
        HumanVerdict(gate_id=pre_gate.gate_id, values={},
                     evidence_urns=[], decided_by="tester-zhang",
                     decided_at="2026-11-16T08:05:00Z"),
        now=_T0 + timedelta(minutes=2))

    snapshot = _run_cli([
        "resume", "--session", sid, "--gate", pre_gate.gate_id,
        "--udid", _UDID, "--bundle-id", _BUNDLE], capsys)

    # 前置闸门消费后重入 s3，挂出 s3 自己的步骤闸门，仍在 awaiting_human
    assert snapshot["checkpoint"]["status"] == \
        SessionStatus.AWAITING_HUMAN.value
    step_gate = next(g for g in gate_service.list_gates(box)
                     if g.step_id == "s3-human-lock")
    assert step_gate.status is GateStatus.PENDING
    ledger_kinds = [e.get("kind") for e in snapshot["ledger"]
                    if e.get("type") == "precondition_gate"
                    and e.get("status") == "responded"]
    assert "notification_permission" in ledger_kinds


# ── 5. unblock：blocked 会话环境恢复后续跑收口 ────────────────────────────────


def test_cli_unblock_blocked_session_reaches_passed(
        tmp_path, monkeypatch, capsys):
    # device_unlocked 无 repair：DH 不可用 → unknown → blocked
    blocked_steps = [PlanStep(
        step_id="s1-home", actor=StepActor.AUTO_DH_AX, action="home",
        preconditions=[Precondition(kind="device_unlocked")],
        idempotency_key="k1")]
    box = _Box(tmp_path, dh=FakeDhAx(
        readiness_error=AdapterUnavailableError("Mac locked")))
    _wire(monkeypatch, box)
    sid = _run_cli(
        ["start", "--plan", _plan_file(tmp_path, steps=blocked_steps)],
        capsys)["session_id"]
    blocked = _run_cli(["drive", "--session", sid, "--udid", _UDID,
                        "--bundle-id", _BUNDLE], capsys)
    assert blocked["checkpoint"]["status"] == SessionStatus.BLOCKED.value

    # 环境恢复（Mac 解锁、DH 重新可用）后续跑
    box._adapters["dh_ax"] = FakeDhAx()
    snapshot = _run_cli(["unblock", "--session", sid, "--udid", _UDID,
                         "--bundle-id", _BUNDLE], capsys)
    assert snapshot["checkpoint"]["status"] == SessionStatus.PASSED.value
    assert snapshot["checkpoint"]["step_statuses"]["s1-home"] \
        == StepStatus.COMPLETED.value


# ── 6. 硬错误统一 exit 2 + stderr 留痕 ────────────────────────────────────────


def test_cli_start_missing_plan_exits_2(tmp_path, monkeypatch, capsys):
    _wire(monkeypatch, _Box(tmp_path))
    with pytest.raises(SystemExit) as exc:
        hybrid_cli.main(["start", "--plan", str(tmp_path / "nope.json")])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "被拒" in err and "混合计划文件不存在" in err


def test_cli_malformed_plan_json_exits_2(tmp_path, monkeypatch, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text("{not-json", encoding="utf-8")
    _wire(monkeypatch, _Box(tmp_path))
    with pytest.raises(SystemExit) as exc:
        hybrid_cli.main(["start", "--plan", str(bad)])
    assert exc.value.code == 2
    assert "被拒" in capsys.readouterr().err


def test_cli_get_unknown_session_exits_2(tmp_path, monkeypatch, capsys):
    _wire(monkeypatch, _Box(tmp_path))
    with pytest.raises(SystemExit) as exc:
        hybrid_cli.main(["get", "--session", "hsess-ffffffffffff"])
    assert exc.value.code == 2
    assert "被拒" in capsys.readouterr().err


def test_cli_drive_missing_device_args_argparse_exits_2(
        tmp_path, monkeypatch, capsys):
    _wire(monkeypatch, _Box(tmp_path))
    # argparse 缺必需参数自身 exit 2，与业务硬错误同口径
    with pytest.raises(SystemExit) as exc:
        hybrid_cli.main(["drive", "--session", "hsess-abc"])
    assert exc.value.code == 2


def test_cli_unknown_subcommand_argparse_exits_2(tmp_path, monkeypatch):
    _wire(monkeypatch, _Box(tmp_path))
    with pytest.raises(SystemExit) as exc:
        hybrid_cli.main(["frobnicate"])
    assert exc.value.code == 2
