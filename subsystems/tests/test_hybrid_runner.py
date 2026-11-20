"""hybrid_runner 分段编排器 hermetic 测试（docs/01 §5/§6，H3）。

零真机/零 Web：真实 FileHybridSessionStore + FileGateStore 落 tmp_path，
DH AX/devicectl/子 run 全假件。覆盖——
① 六步锁屏计划全链路：自动连环推进 → 前置人工闸门 → 步骤闸门 → 裁决闸门 → passed；
② 崩溃续跑：换全新容器从文件恢复，已完成步骤不重放（子 run 不重复提交）；
③ 前置 blocked/fail_loud 两姿态、动作失败按 failure_policy 收口；
④ 闸门归属/状态守卫、缺端口 fail-loud、账本留痕。
"""

from __future__ import annotations

import struct
from datetime import datetime, timedelta, timezone

import pytest

from testpilot_adapters.gate_store import FileGateStore
from testpilot_adapters.devicectl_control import DevicectlControlError
from testpilot_adapters.dh_ax import DhAxActionError
from testpilot_adapters.hybrid_session_store import FileHybridSessionStore
from testpilot_core.enums import (
    GateStatus,
    SessionStatus,
    StepActor,
    StepStatus,
)
from testpilot_core.errors import AdapterUnavailableError, NotFoundError
from testpilot_core.hybrid_plan import (
    HumanVerdict,
    HybridPlanError,
    PlanRepair,
    PlanStep,
    Precondition,
    VerdictField,
    VerdictForm,
    build_hybrid_plan,
    gate_id_for,
)
from testpilot_core.models import Oracle, OracleType, TestCase
from testpilot_workers import gate_service
from testpilot_workers.hybrid_probes import ProbeContext
from testpilot_workers.hybrid_runner import (
    ChildRunRequest,
    drive_until_wait,
    get_session,
    resume_after_gate,
    start_session,
    unblock,
)

# 锚定墙钟而非硬编码日期：本模块混用了注入时钟与 gate_service.list_gates 的
# 真实 now 读时清扫，硬编码 _T0 一旦被当天墙钟越过，pending 闸门会在读取时
# 被判过期（2026-11-14 日期翻转曾致 7 条用例集体假红）；断言只依赖相对增量。
_T0 = datetime.now(timezone.utc)
_UDID = "00008140-001A39590162801C"
_BUNDLE = "com.example.AlarmKitSpike"
_PNG_SIG = b"\x89PNG\r\n\x1a\n"


def _png(width: int, height: int) -> bytes:
    return (_PNG_SIG + struct.pack(">I4s", 13, b"IHDR")
            + struct.pack(">II", width, height) + b"\x00" * 5)


class FakeDhAx:
    def __init__(self, *, readiness_error=None, home_error=None,
                 readiness=None) -> None:
        self._readiness_error = readiness_error
        self._home_error = home_error
        self._readiness = readiness or {
            "ok": True, "verb": "readiness", "screen_locked": False,
            "buttons": {"home": True, "screenshot": True,
                        "rotate_left": True},
            "windows": [{"width": 320, "height": 480}]}
        self.actions: list[str] = []

    async def readiness(self):
        self.actions.append("readiness")
        if self._readiness_error:
            raise self._readiness_error
        return dict(self._readiness)

    async def home(self):
        self.actions.append("home")
        if self._home_error:
            raise self._home_error
        return {"ok": True, "verb": "home"}

    async def rotate_left(self, times=1):
        self.actions.append(f"rotate_leftx{times}")
        return {"ok": True, "verb": "rotate-left", "times": times}

    async def screenshot(self):
        self.actions.append("screenshot")
        return {"ok": True, "verb": "screenshot"}


class FakeDevicectl:
    def __init__(self, *, portrait=True, capture_error=None) -> None:
        self._portrait = portrait
        self._capture_error = capture_error
        self._root = None
        self.shots = 0
        self.launched: list[dict] = []
        self.terminated: list[int] = []

    def bind_root(self, path):
        self._root = path
        path.mkdir(parents=True, exist_ok=True)

    async def capture_screenshot(self, udid):
        self.shots += 1
        if self._capture_error:
            raise self._capture_error
        path = self._root / f"shot-{self.shots}.png"
        w, h = (1206, 2622) if self._portrait else (2622, 1206)
        path.write_bytes(_png(w, h))
        return path

    async def launch_app(self, udid, bundle_id, *, arguments=None,
                         terminate_existing=False):
        self.launched.append({"bundle": bundle_id, "arguments": arguments,
                              "terminate_existing": terminate_existing})

    async def terminate_pid(self, udid, pid, *, kill=False):
        self.terminated.append(pid)


class FakeChildRun:
    def __init__(self) -> None:
        self.requests: list[ChildRunRequest] = []

    async def submit(self, request: ChildRunRequest) -> str:
        self.requests.append(request)
        return f"run-{len(self.requests)}"


class _Box:
    def __init__(self, tmp_path, dh, dev, child_run=None):
        self.base_dir = tmp_path
        if dev is not None:
            dev.bind_root(tmp_path / "shots")
        self._adapters = {
            "dh_ax": dh,
            "devicectl": dev,
            "hybrid_child_run": child_run,
            "gate_store": FileGateStore(tmp_path / "data"),
            "hybrid_session_store": FileHybridSessionStore(tmp_path / "data"),
        }

    def get(self, name):
        return self._adapters.get(name)


def _case() -> TestCase:
    return TestCase(
        urn="urn:testpilot:fs-cases:test_case:cases.md#TC-LOCK-01",
        external_id="TC-LOCK-01", title="锁屏响铃侧键接力",
        module="RoutineAlarm", priority="P0",
        steps=["排钟", "人工锁屏", "到点响铃", "侧键解锁进接力页"],
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


def _steps() -> list[PlanStep]:
    return [
        PlanStep(
            step_id="s1-purge-and-schedule",
            actor=StepActor.AUTO_XCODEBUILD, action="run_selector",
            params={"only_testing": ["AlarmKitSpikeUITests/testLockRelay_schedule"]},
            preconditions=[
                Precondition(kind="portrait_orientation",
                             repair=PlanRepair(verb="dh_rotate_left")),
                Precondition(kind="no_active_alarms",
                             repair=PlanRepair(verb="app_launch_purge_args")),
            ],
            evidence_bindings=["urn:testpilot:store:artifact:segA-xcresult"],
            idempotency_key="s1|TC-LOCK-01|schedule"),
        PlanStep(
            step_id="s2-baseline-shot",
            actor=StepActor.AUTO_DEVICECTL, action="screenshot",
            params={"bundle_id": _BUNDLE},
            idempotency_key="s2|TC-LOCK-01|baseline"),
        PlanStep(
            step_id="s3-human-lock",
            actor=StepActor.HUMAN_ACTION, action="lock_screen",
            prompt="请在 60 秒内按电源键锁屏，并确认屏幕熄灭",
            preconditions=[Precondition(
                kind="notification_permission", on_failure="blocked",
                repair=PlanRepair(verb="human_gate"))],
            idempotency_key="s3|TC-LOCK-01|lock"),
        PlanStep(
            step_id="s4-human-sidebutton",
            actor=StepActor.HUMAN_ACTION, action="press_side_button",
            prompt="闹钟响铃后按一次物理侧边键，然后解锁进入接力页",
            idempotency_key="s4|TC-LOCK-01|sidebutton"),
        PlanStep(
            step_id="s5-sensory-verdict",
            actor=StepActor.HUMAN_VERDICT, action="sensory_check",
            prompt="请确认刚才锁屏期间的铃声与震动表现",
            verdict_form=_verdict_form(),
            evidence_bindings=[
                "urn:testpilot:store:artifact:oslog-ring-chain",
                "urn:testpilot:store:artifact:relay-video"],
            idempotency_key="s5|TC-LOCK-01|verdict"),
        PlanStep(
            step_id="s6-relay-assert",
            actor=StepActor.AUTO_XCODEBUILD, action="run_selector",
            params={"only_testing": ["AlarmKitSpikeUITests/testLockRelay_relayPage"]},
            evidence_bindings=["urn:testpilot:store:artifact:segB-xcresult"],
            idempotency_key="s6|TC-LOCK-01|relay"),
    ]


def _plan_payload() -> dict:
    return build_hybrid_plan(
        case=_case(), profile="ios-local-poc", project="AlarmKitSpike",
        steps=_steps(), now=_T0)


def _box(tmp_path, *, dh=None, dev=None, child_run=None) -> _Box:
    return _Box(tmp_path,
                dh or FakeDhAx(),
                dev if dev is not None else FakeDevicectl(),
                child_run if child_run is not None else FakeChildRun())


def _ctx() -> ProbeContext:
    return ProbeContext(udid=_UDID, bundle_id=_BUNDLE)


def _ack(gate_id: str, *, form: bool = False) -> HumanVerdict:
    values = ({"ringtone_heard": True, "vibration_felt": True,
               "confidence": "high"} if form else {})
    return HumanVerdict(
        gate_id=gate_id, values=values,
        evidence_urns=(["urn:testpilot:store:artifact:oslog-ring-chain"]
                       if form else []),
        decided_by="tester-zhang",
        decided_at="2026-11-14T08:05:00Z")


# ── 1. 会话建立与幂等 ─────────────────────────────────────────────────────────


def test_start_session_idempotent_and_shape(tmp_path):
    box = _box(tmp_path)
    record = start_session(box, _plan_payload(), now=_T0)
    assert record.checkpoint.status is SessionStatus.RUNNING
    assert record.checkpoint.current_step_id == "s1-purge-and-schedule"
    assert record.ledger[0]["type"] == "session_started"

    again = start_session(box, _plan_payload(), now=_T0)
    assert again.session_id == record.session_id
    assert again is record or again.session_id == record.session_id
    # 重建不产生第二条 session_started 账本
    assert len([e for e in again.ledger if e["type"] == "session_started"]) == 1

    with pytest.raises(NotFoundError):
        get_session(box, "hsess-ffffffffffff")


# ── 2. 自动连环推进到前置人工闸门 ─────────────────────────────────────────────


async def test_drive_auto_chain_suspends_at_precondition_gate(tmp_path):
    box = _box(tmp_path)
    record = start_session(box, _plan_payload(), now=_T0)

    record = await drive_until_wait(
        box, record.session_id, _ctx(), now=_T0 + timedelta(minutes=1))
    assert record.checkpoint.status is SessionStatus.AWAITING_HUMAN

    # s1 子 run 携带 purge 启动参数；s2 截图完成；s3 前置闸门挂起
    child = box.get("hybrid_child_run")
    assert len(child.requests) == 1
    req = child.requests[0]
    assert req.only_testing == ["AlarmKitSpikeUITests/testLockRelay_schedule"]
    assert req.launch_arguments == ["--ak-purge-dates"]
    assert box.get("devicectl").shots >= 1
    assert record.checkpoint.step_statuses["s1-purge-and-schedule"] \
        is StepStatus.COMPLETED
    assert record.checkpoint.step_statuses["s2-baseline-shot"] \
        is StepStatus.COMPLETED

    gates = gate_service.list_gates(box)
    assert len(gates) == 1
    pre_gate = gates[0]
    assert pre_gate.step_id == "s3-human-lock#pre-notification_permission"
    assert pre_gate.status is GateStatus.PENDING
    assert record.checkpoint.gate_ids == [pre_gate.gate_id]


# ── 3. 前置闸门响应后重入，挂出 s3 步骤闸门 ──────────────────────────────────


async def test_precondition_gate_resume_reenters_without_duplicate_card(
        tmp_path):
    box = _box(tmp_path)
    record = start_session(box, _plan_payload(), now=_T0)
    record = await drive_until_wait(
        box, record.session_id, _ctx(), now=_T0 + timedelta(minutes=1))
    pre_gate = gate_service.list_gates(box)[0]

    gate_service.respond_gate(box, pre_gate.gate_id,
                              _ack(pre_gate.gate_id), now=_T0 + timedelta(minutes=2))
    record = await resume_after_gate(
        box, record.session_id, pre_gate.gate_id, _ctx(),
        now=_T0 + timedelta(minutes=2))

    # 仍是 awaiting：s3 自己的 human:action 闸门
    assert record.checkpoint.status is SessionStatus.AWAITING_HUMAN
    gates = gate_service.list_gates(box)
    step_gate = next(g for g in gates if g.step_id == "s3-human-lock")
    assert step_gate.status is GateStatus.PENDING
    # 前置闸门不重复弹：队列里仍是两条（前置 responded + 步骤 pending）
    assert len(gates) == 2
    ack_kinds = [e for e in record.ledger
                 if e.get("type") == "precondition_gate"
                 and e.get("status") == "responded"]
    assert ack_kinds and ack_kinds[0]["kind"] == "notification_permission"


# ── 4. 全链路收口：动作闸门 + 裁决闸门 → passed ──────────────────────────────


async def test_full_happy_path_reaches_passed(tmp_path):
    box = _box(tmp_path)
    record = start_session(box, _plan_payload(), now=_T0)
    moment = _T0

    # 每次挂起恰有一张 pending 闸门（s3 会挂两次：前置闸门 + 步骤闸门）
    while record.checkpoint.status is not SessionStatus.PASSED:
        record = await drive_until_wait(
            box, record.session_id, _ctx(), now=moment)
        pending = gate_service.list_gates(box, status="pending")
        assert len(pending) == 1
        due = pending[0]
        moment += timedelta(minutes=1)
        gate_service.respond_gate(
            box, due.gate_id,
            _ack(due.gate_id, form=due.verdict_form is not None),
            now=moment)
        record = await resume_after_gate(
            box, record.session_id, due.gate_id, _ctx(), now=moment)

    # 六个步骤全部 completed，会话 passed，s6 子 run 已提交
    assert record.checkpoint.status is SessionStatus.PASSED
    assert record.checkpoint.current_step_id is None
    statuses = record.checkpoint.step_statuses
    assert all(v is StepStatus.COMPLETED for v in statuses.values())
    child = box.get("hybrid_child_run")
    assert [r.only_testing[0] for r in child.requests] == [
        "AlarmKitSpikeUITests/testLockRelay_schedule",
        "AlarmKitSpikeUITests/testLockRelay_relayPage"]
    # 裁决闸门留痕：responded 且带结构化裁决与证据
    verdict_gate = gate_service.get_gate(
        box, gate_id_for(record.session_id, "s5-sensory-verdict"))
    assert verdict_gate.status is GateStatus.RESPONDED
    assert verdict_gate.response.values["confidence"] == "high"


# ── 5. 崩溃续跑：文件恢复，已完成步骤不重放 ─────────────────────────────────


async def test_crash_resume_replays_nothing(tmp_path):
    box1 = _box(tmp_path)
    record = start_session(box1, _plan_payload(), now=_T0)
    record = await drive_until_wait(
        box1, record.session_id, _ctx(), now=_T0 + timedelta(minutes=1))
    sid = record.session_id
    assert box1.get("hybrid_child_run").requests  # s1 已提交过

    # 进程重启：全新容器接同一文件根，全部假件从零开始
    box2 = _Box(tmp_path, FakeDhAx(), FakeDevicectl(), FakeChildRun())
    recovered = get_session(box2, sid)
    assert recovered.checkpoint.current_step_id == "s3-human-lock"
    pre_gate = gate_service.list_gates(box2, status="pending")[0]
    gate_service.respond_gate(
        box2, pre_gate.gate_id, _ack(pre_gate.gate_id),
        now=_T0 + timedelta(minutes=2))
    recovered = await resume_after_gate(
        box2, sid, pre_gate.gate_id, _ctx(),
        now=_T0 + timedelta(minutes=2))
    # 新容器的子 run 端口没有重放 s1（恢复后第一个提交只能是后续段）
    new_requests = box2.get("hybrid_child_run").requests
    assert all("testLockRelay_schedule" not in r.only_testing[0]
               for r in new_requests)


# ── 6. 前置姿态：blocked 与 fail_loud ────────────────────────────────────────


async def test_unsatisfied_precondition_without_repair_blocks(tmp_path):
    # device_unlocked 无 repair：DH 不可用 → unknown → blocked 姿态
    dh = FakeDhAx(readiness_error=AdapterUnavailableError("Mac locked"))
    steps = [PlanStep(
        step_id="s1", actor=StepActor.AUTO_DH_AX, action="home",
        preconditions=[Precondition(kind="device_unlocked")],
        idempotency_key="k1")]
    box = _Box(tmp_path, dh, FakeDevicectl(), FakeChildRun())
    record = start_session(box, build_hybrid_plan(
        case=_case(), profile="p", project="j", steps=steps, now=_T0), now=_T0)
    record = await drive_until_wait(box, record.session_id, _ctx(), now=_T0)
    assert record.checkpoint.status is SessionStatus.BLOCKED
    assert record.checkpoint.step_statuses["s1"] is StepStatus.BLOCKED

    # 环境恢复后 unblock 续跑成功
    dh._readiness_error = None
    record = await unblock(box, record.session_id, _ctx(),
                           now=_T0 + timedelta(minutes=1))
    assert record.checkpoint.status is SessionStatus.PASSED


async def test_fail_loud_precondition_raises(tmp_path):
    steps = [PlanStep(
        step_id="s1", actor=StepActor.AUTO_DH_AX, action="home",
        preconditions=[Precondition(
            kind="device_unlocked", on_failure="fail_loud")],
        idempotency_key="k1")]
    dh = FakeDhAx(readiness_error=AdapterUnavailableError("no AX"))
    box = _Box(tmp_path, dh, FakeDevicectl(), FakeChildRun())
    record = start_session(box, build_hybrid_plan(
        case=_case(), profile="p", project="j", steps=steps, now=_T0), now=_T0)
    with pytest.raises(HybridPlanError, match="fail_loud"):
        await drive_until_wait(box, record.session_id, _ctx(), now=_T0)


# ── 7. 动作失败按 failure_policy 收口 ────────────────────────────────────────


async def test_action_failure_abort_vs_block_for_human(tmp_path):
    steps_block = [PlanStep(
        step_id="s1", actor=StepActor.AUTO_DH_AX, action="home",
        failure_policy="block_for_human", idempotency_key="k1")]
    box = _Box(tmp_path, FakeDhAx(home_error=DhAxActionError("exit 5")),
               FakeDevicectl(), FakeChildRun())
    record = start_session(box, build_hybrid_plan(
        case=_case(), profile="p", project="j", steps=steps_block, now=_T0),
        now=_T0)
    record = await drive_until_wait(box, record.session_id, _ctx(), now=_T0)
    assert record.checkpoint.status is SessionStatus.BLOCKED

    steps_abort = [PlanStep(
        step_id="s1", actor=StepActor.AUTO_DH_AX, action="home",
        failure_policy="abort_session", idempotency_key="k2")]
    box2 = _Box(tmp_path, FakeDhAx(home_error=DevicectlControlError("x")),
                FakeDevicectl(), FakeChildRun())
    record2 = start_session(box2, build_hybrid_plan(
        case=_case(), profile="p", project="j", steps=steps_abort, now=_T0),
        now=_T0)
    record2 = await drive_until_wait(
        box2, record2.session_id, _ctx(), now=_T0)
    assert record2.checkpoint.status is SessionStatus.FAILED


async def test_adapter_unavailable_is_blocked_not_failed(tmp_path):
    # 基础设施缺席与产品缺陷严格区分（docs/01 §9）
    steps = [PlanStep(
        step_id="s2", actor=StepActor.AUTO_DEVICECTL, action="screenshot",
        params={"bundle_id": _BUNDLE}, idempotency_key="k2")]
    dev = FakeDevicectl(capture_error=AdapterUnavailableError("device gone"))
    box = _Box(tmp_path, FakeDhAx(), dev, FakeChildRun())
    record = start_session(box, build_hybrid_plan(
        case=_case(), profile="p", project="j", steps=steps, now=_T0), now=_T0)
    record = await drive_until_wait(box, record.session_id, _ctx(), now=_T0)
    assert record.checkpoint.status is SessionStatus.BLOCKED
    assert any(e["type"] == "step_blocked" for e in record.ledger)


# ── 8. 守卫与 fail-loud ──────────────────────────────────────────────────────


async def test_resume_guards_gate_status_and_ownership(tmp_path):
    box = _box(tmp_path)
    record = start_session(box, _plan_payload(), now=_T0)
    record = await drive_until_wait(
        box, record.session_id, _ctx(), now=_T0 + timedelta(minutes=1))
    pre_gate = gate_service.list_gates(box)[0]

    # pending 闸门不能驱动续跑
    with pytest.raises(HybridPlanError, match="responded"):
        await resume_after_gate(
            box, record.session_id, pre_gate.gate_id, _ctx(), now=_T0)

    # 外会话闸门 id 归属不符
    foreign = gate_id_for("hsess-000000000000", "s3-human-lock")
    with pytest.raises(NotFoundError):
        gate_service.get_gate(box, foreign)

    # 同一闸门重复续跑拒绝（防人工步双推进）
    gate_service.respond_gate(
        box, pre_gate.gate_id, _ack(pre_gate.gate_id),
        now=_T0 + timedelta(minutes=2))
    await resume_after_gate(
        box, record.session_id, pre_gate.gate_id, _ctx(),
        now=_T0 + timedelta(minutes=2))
    with pytest.raises(HybridPlanError, match="已消费过一次"):
        await resume_after_gate(
            box, record.session_id, pre_gate.gate_id, _ctx(), now=_T0)


async def test_missing_child_run_port_is_loud(tmp_path):
    box = _Box(tmp_path, FakeDhAx(), FakeDevicectl(), child_run=None)
    record = start_session(box, _plan_payload(), now=_T0)
    with pytest.raises(HybridPlanError, match="hybrid_child_run"):
        await drive_until_wait(box, record.session_id, _ctx(), now=_T0)


async def test_terminal_session_drive_is_noop(tmp_path):
    box = _box(tmp_path)
    record = start_session(box, _plan_payload(), now=_T0)
    moment = _T0
    while record.checkpoint.status is not SessionStatus.PASSED:
        record = await drive_until_wait(
            box, record.session_id, _ctx(), now=moment)
        pending = gate_service.list_gates(box, status="pending")
        assert len(pending) == 1
        due = pending[0]
        moment += timedelta(minutes=1)
        gate_service.respond_gate(
            box, due.gate_id,
            _ack(due.gate_id, form=due.verdict_form is not None), now=moment)
        record = await resume_after_gate(
            box, record.session_id, due.gate_id, _ctx(), now=moment)
    assert record.checkpoint.status is SessionStatus.PASSED

    again = await drive_until_wait(
        box, record.session_id, _ctx(), now=moment)
    assert again.checkpoint.status is SessionStatus.PASSED


async def test_verdict_gate_rejects_empty_evidence_end_to_end(tmp_path):
    box = _box(tmp_path)
    record = start_session(box, _plan_payload(), now=_T0)
    moment = _T0
    # 前两张闸门（s3 前置 + s3 动作 + s4 动作）正常响应，直到 s5 裁决闸验红证
    while True:
        record = await drive_until_wait(
            box, record.session_id, _ctx(), now=moment)
        pending = gate_service.list_gates(box, status="pending")
        assert len(pending) == 1
        due = pending[0]
        moment += timedelta(minutes=1)
        if due.verdict_form is not None:
            empty = _ack(due.gate_id, form=True)
            empty.evidence_urns = []
            with pytest.raises(HybridPlanError, match="空证据裁决"):
                gate_service.respond_gate(box, due.gate_id, empty, now=moment)
            return
        gate_service.respond_gate(
            box, due.gate_id, _ack(due.gate_id), now=moment)
        record = await resume_after_gate(
            box, record.session_id, due.gate_id, _ctx(), now=moment)
