"""hybrid-plan-v1 混合执行计划纯函数 hostless 测试（docs/15，H1）。

零 IO、零容器：手工构造 core 模型，钉死四组契约——
① 计划校验闭集（actor/动作/前置/修复/裁决单，表外字面量响亮失败）；
② 内容寻址 hash 跨时间戳稳定、随内容变；
③ 会话检查点迁移闭集（终态不可变、野步骤/重入拒绝、挂起/恢复）；
④ 人工闸门载荷与裁决单校验（空证据/空见证人/野字段一律拒绝）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from testpilot_core.enums import (
    GateStatus,
    SessionStatus,
    StepActor,
    StepStatus,
)
from testpilot_core.hybrid_plan import (
    HYBRID_PLAN_SCHEMA_VERSION,
    GateRequest,
    HumanVerdict,
    HybridPlan,
    HybridPlanError,
    PlanRepair,
    PlanStep,
    Precondition,
    SessionCheckpoint,
    VerdictField,
    VerdictForm,
    advance_checkpoint,
    build_gate_request,
    build_hybrid_plan,
    cancel_gate,
    canonical_plan_hash,
    expire_gate_if_due,
    gate_id_for,
    gate_is_expired,
    next_pending_step,
    plan_filename,
    record_gate_response,
    renew_gate,
    resume_from_blocked,
    resume_from_human,
    start_checkpoint,
    suspend_for_human,
    transition_session,
    validate_verdict,
)
from testpilot_core.models import Oracle, TestCase
from testpilot_core.enums import OracleType

_T0 = datetime(2026, 11, 14, 8, 0, tzinfo=timezone.utc)
_T1 = _T0 + timedelta(minutes=5)
_CASE_URN = "urn:testpilot:fs-cases:test_case:cases.md#TC-LOCK-01"
_BUNDLE = "com.example.AlarmKitSpike"


def _case(**over) -> TestCase:
    kwargs = dict(
        urn=_CASE_URN, external_id="TC-LOCK-01", title="锁屏响铃侧键接力",
        module="RoutineAlarm", priority="P0",
        steps=["排钟", "人工锁屏", "到点响铃", "侧键解锁进接力页"],
        oracles=[Oracle(type=OracleType.LOG_ASSERTION,
                        expectation="os_log 出现响铃事件链", confidence=0.9)],
        lifecycle="active", exploratory=False)
    kwargs.update(over)
    return TestCase(**kwargs)


def _verdict_form() -> VerdictForm:
    return VerdictForm(fields=[
        VerdictField(key="ringtone_heard", label="是否听到铃声", kind="boolean"),
        VerdictField(key="vibration_felt", label="是否感到震动", kind="boolean"),
        VerdictField(key="confidence", label="把握程度", kind="choice",
                     options=["high", "medium", "low"]),
    ])


def _steps() -> list[PlanStep]:
    """锁屏响铃→侧键→接力页的完整混合编排（讨论中的定版样例）。"""
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


# ── 1. 建计划 happy path 与载荷契约 ─────────────────────────────────────────


def test_build_plan_happy_path_roundtrips():
    payload = build_hybrid_plan(
        case=_case(), profile="ios-local-poc", project="AlarmKitSpike",
        steps=_steps(), now=_T0)

    assert payload["plan_schema_version"] == HYBRID_PLAN_SCHEMA_VERSION
    assert payload["case_urn"] == _CASE_URN
    assert payload["case_external_id"] == "TC-LOCK-01"
    assert len(payload["steps"]) == 6
    assert payload["created_at"] == "2026-11-14T08:00:00Z"
    assert len(payload["content_hash"]) == 64

    # pydantic 往返：载荷本身必须是合法 HybridPlan
    model = HybridPlan.model_validate(payload)
    assert model.steps[2].actor is StepActor.HUMAN_ACTION
    assert model.steps[4].verdict_form.fields[0].key == "ringtone_heard"


def test_build_plan_rejects_cancelled_and_exploratory():
    with pytest.raises(HybridPlanError, match="lifecycle=cancelled"):
        build_hybrid_plan(case=_case(lifecycle="cancelled"),
                          profile="p", project="j", steps=_steps()[:1], now=_T0)
    with pytest.raises(HybridPlanError, match="探索性用例"):
        build_hybrid_plan(case=_case(exploratory=True),
                          profile="p", project="j", steps=_steps()[:1], now=_T0)


def test_naive_timestamp_rejected():
    with pytest.raises(HybridPlanError, match="时区"):
        build_hybrid_plan(case=_case(), profile="p", project="j",
                          steps=_steps()[:1],
                          now=datetime(2026, 11, 14, 8, 0))


# ── 2. 校验闭集：表外字面量一律响亮失败 ─────────────────────────────────────


def test_empty_plan_rejected():
    with pytest.raises(HybridPlanError, match="至少包含一个步骤"):
        build_hybrid_plan(case=_case(), profile="p", project="j",
                          steps=[], now=_T0)


def test_duplicate_step_id_rejected():
    steps = _steps()
    steps[1] = steps[1].model_copy(update={"step_id": steps[0].step_id})
    with pytest.raises(HybridPlanError, match="step_id 重复"):
        build_hybrid_plan(case=_case(), profile="p", project="j",
                          steps=steps, now=_T0)


def test_unknown_actor_rejected_at_construction():
    # actor 是枚举闭集：表外 actor 连模型都构造不出来，而不是默默落库
    with pytest.raises(ValidationError):
        PlanStep(step_id="x", actor="auto:robot", action="home",
                 idempotency_key="k")


def test_action_outside_actor_vocabulary_rejected():
    bad = PlanStep(step_id="x", actor=StepActor.AUTO_DH_AX,
                   action="lock_screen", idempotency_key="k")
    with pytest.raises(HybridPlanError, match="不在.*允许闭集"):
        build_hybrid_plan(case=_case(), profile="p", project="j",
                          steps=[bad], now=_T0)


def test_xcodebuild_requires_only_testing():
    bad = PlanStep(step_id="x", actor=StepActor.AUTO_XCODEBUILD,
                   action="run_selector", params={}, idempotency_key="k")
    with pytest.raises(HybridPlanError, match="only_testing"):
        build_hybrid_plan(case=_case(), profile="p", project="j",
                          steps=[bad], now=_T0)

    empty = PlanStep(step_id="x", actor=StepActor.AUTO_XCODEBUILD,
                     action="run_selector",
                     params={"only_testing": []}, idempotency_key="k")
    with pytest.raises(HybridPlanError, match="only_testing"):
        build_hybrid_plan(case=_case(), profile="p", project="j",
                          steps=[empty], now=_T0)


def test_devicectl_requires_bundle_id():
    bad = PlanStep(step_id="x", actor=StepActor.AUTO_DEVICECTL,
                   action="launch_app", params={}, idempotency_key="k")
    with pytest.raises(HybridPlanError, match="bundle_id"):
        build_hybrid_plan(case=_case(), profile="p", project="j",
                          steps=[bad], now=_T0)


def test_human_action_requires_prompt():
    bad = PlanStep(step_id="x", actor=StepActor.HUMAN_ACTION,
                   action="lock_screen", idempotency_key="k")
    with pytest.raises(HybridPlanError, match="人工操作说明 prompt"):
        build_hybrid_plan(case=_case(), profile="p", project="j",
                          steps=[bad], now=_T0)


def test_human_verdict_requires_form():
    bad = PlanStep(step_id="x", actor=StepActor.HUMAN_VERDICT,
                   action="sensory_check", prompt="确认铃声",
                   idempotency_key="k")
    with pytest.raises(HybridPlanError, match="非空 verdict_form"):
        build_hybrid_plan(case=_case(), profile="p", project="j",
                          steps=[bad], now=_T0)


def test_auto_step_must_not_carry_form():
    bad = PlanStep(step_id="x", actor=StepActor.AUTO_DH_AX, action="home",
                   verdict_form=_verdict_form(), idempotency_key="k")
    with pytest.raises(HybridPlanError, match="自动步不得携带"):
        build_hybrid_plan(case=_case(), profile="p", project="j",
                          steps=[bad], now=_T0)


def test_unknown_precondition_and_repair_rejected():
    bad_pre = PlanStep(
        step_id="x", actor=StepActor.AUTO_DH_AX, action="home",
        preconditions=[Precondition(kind="device_rooted")],
        idempotency_key="k")
    with pytest.raises(HybridPlanError, match="未知前置条件种类"):
        build_hybrid_plan(case=_case(), profile="p", project="j",
                          steps=[bad_pre], now=_T0)

    bad_repair = PlanStep(
        step_id="y", actor=StepActor.AUTO_DH_AX, action="home",
        preconditions=[Precondition(
            kind="device_unlocked",
            repair=PlanRepair(verb="sudo_reboot"))],
        idempotency_key="k2")
    with pytest.raises(HybridPlanError, match="不在.*能力圈"):
        build_hybrid_plan(case=_case(), profile="p", project="j",
                          steps=[bad_repair], now=_T0)


def test_human_gate_repair_requires_blocked_posture():
    bad = PlanStep(
        step_id="x", actor=StepActor.HUMAN_ACTION, action="lock_screen",
        prompt="锁屏", preconditions=[Precondition(
            kind="notification_permission", on_failure="fail_loud",
            repair=PlanRepair(verb="human_gate"))],
        idempotency_key="k")
    with pytest.raises(HybridPlanError, match="human_gate 修复只能配 blocked"):
        build_hybrid_plan(case=_case(), profile="p", project="j",
                          steps=[bad], now=_T0)


def test_verdict_form_duplicate_keys_and_choice_options():
    dup_form = VerdictForm(fields=[
        VerdictField(key="a", label="A", kind="boolean"),
        VerdictField(key="a", label="A2", kind="boolean")])
    dup = PlanStep(step_id="x", actor=StepActor.HUMAN_VERDICT,
                   action="sensory_check", prompt="p", verdict_form=dup_form,
                   evidence_bindings=["urn:testpilot:store:artifact:e1"],
                   idempotency_key="k")
    with pytest.raises(HybridPlanError, match="裁决字段 key 重复"):
        build_hybrid_plan(case=_case(), profile="p", project="j",
                          steps=[dup], now=_T0)

    no_opts = VerdictForm(fields=[
        VerdictField(key="c", label="C", kind="choice")])
    bad = PlanStep(step_id="y", actor=StepActor.HUMAN_VERDICT,
                   action="sensory_check", prompt="p", verdict_form=no_opts,
                   evidence_bindings=["urn:testpilot:store:artifact:e1"],
                   idempotency_key="k2")
    with pytest.raises(HybridPlanError, match="choice 字段 c 缺 options"):
        build_hybrid_plan(case=_case(), profile="p", project="j",
                          steps=[bad], now=_T0)


def test_required_evidence_step_must_bind_evidence():
    bare = PlanStep(step_id="x", actor=StepActor.HUMAN_VERDICT,
                    action="sensory_check", prompt="p",
                    verdict_form=_verdict_form(), idempotency_key="k")
    with pytest.raises(HybridPlanError, match="evidence_bindings"):
        build_hybrid_plan(case=_case(), profile="p", project="j",
                          steps=[bare], now=_T0)


# ── 3. 内容寻址 ──────────────────────────────────────────────────────────────


def test_hash_stable_across_timestamps_but_changes_with_content():
    steps = _steps()
    p1 = build_hybrid_plan(case=_case(), profile="p", project="j",
                           steps=steps, now=_T0)
    p2 = build_hybrid_plan(case=_case(), profile="p", project="j",
                           steps=steps, now=_T1)
    assert p1["content_hash"] == p2["content_hash"]

    changed = [s.model_copy(
        update={"params": {"only_testing": ["OtherTests/test_changed"]}})
        if s.step_id == "s1-purge-and-schedule" else s for s in steps]
    p3 = build_hybrid_plan(case=_case(), profile="p", project="j",
                           steps=changed, now=_T0)
    assert p3["content_hash"] != p1["content_hash"]

    # 手工篡改 payload 的时间戳不影响 canonical 重算
    assert canonical_plan_hash(p1) == p1["content_hash"]
    tampered = dict(p1, created_at="2099-01-01T00:00:00Z")
    assert canonical_plan_hash(tampered) == p1["content_hash"]


def test_plan_filename_uses_utc_stamp_and_hash_prefix():
    payload = build_hybrid_plan(case=_case(), profile="p", project="j",
                                steps=_steps()[:1], now=_T0)
    name = plan_filename(payload["content_hash"], _T0)
    assert name == f"hybrid-plan-20261114T080000Z-{payload['content_hash'][:8]}.json"


# ── 4. 检查点迁移闭集 ────────────────────────────────────────────────────────


def _checkpoint(steps: list[PlanStep]) -> SessionCheckpoint:
    return start_checkpoint(
        session_id="sess-1", plan_hash="h" * 64,
        first_step_id=steps[0].step_id, now=_T0)


def test_start_checkpoint_pending_to_running():
    cp = _checkpoint(_steps())
    assert cp.status is SessionStatus.RUNNING
    assert cp.step_statuses["s1-purge-and-schedule"] is StepStatus.RUNNING
    assert cp.current_step_id == "s1-purge-and-schedule"


def test_advance_through_steps_ends_passed():
    steps = _steps()
    cp = _checkpoint(steps)
    for i, step in enumerate(steps[:-1]):
        cp = advance_checkpoint(
            cp, step_id=step.step_id, result=StepStatus.COMPLETED,
            next_step_id=steps[i + 1].step_id, now=_T1)
        assert cp.status is SessionStatus.RUNNING
        assert cp.current_step_id == steps[i + 1].step_id
        assert cp.step_statuses[steps[i + 1].step_id] is StepStatus.RUNNING

    cp = advance_checkpoint(
        cp, step_id=steps[-1].step_id, result=StepStatus.COMPLETED,
        next_step_id=None, now=_T1)
    assert cp.status is SessionStatus.PASSED
    assert cp.current_step_id is None
    assert cp.completed_steps == [s.step_id for s in steps]


def test_blocked_result_suspends_session_without_failure():
    steps = _steps()
    cp = _checkpoint(steps)
    cp = advance_checkpoint(
        cp, step_id=steps[0].step_id, result=StepStatus.BLOCKED,
        next_step_id=None, now=_T1)
    assert cp.status is SessionStatus.BLOCKED
    assert cp.step_statuses[steps[0].step_id] is StepStatus.BLOCKED

    resumed = resume_from_blocked(cp, now=_T1)
    assert resumed.status is SessionStatus.RUNNING
    assert resumed.step_statuses[steps[0].step_id] is StepStatus.RUNNING


def test_failed_step_terminates_session():
    steps = _steps()
    cp = _checkpoint(steps)
    cp = advance_checkpoint(
        cp, step_id=steps[0].step_id, result=StepStatus.FAILED,
        next_step_id=None, now=_T1)
    assert cp.status is SessionStatus.FAILED
    # 终态不可变
    with pytest.raises(HybridPlanError, match="非法会话状态迁移"):
        transition_session(cp.status, SessionStatus.RUNNING)


def test_advance_requires_running_step():
    steps = _steps()
    cp = _checkpoint(steps)
    cp = advance_checkpoint(
        cp, step_id=steps[0].step_id, result=StepStatus.COMPLETED,
        next_step_id=steps[1].step_id, now=_T1)
    # 再推一次已完成的旧步 = 拒绝
    with pytest.raises(HybridPlanError, match="只有 running 步可推进"):
        advance_checkpoint(
            cp, step_id=steps[0].step_id, result=StepStatus.COMPLETED,
            next_step_id=steps[2].step_id, now=_T1)


def test_wild_step_update_rejected():
    cp = _checkpoint(_steps())
    with pytest.raises(HybridPlanError, match="不含步骤"):
        advance_checkpoint(
            cp, step_id="s99-ghost", result=StepStatus.COMPLETED,
            next_step_id=None, now=_T1)


def test_reentry_into_disposed_step_rejected():
    steps = _steps()
    cp = _checkpoint(steps)
    cp = advance_checkpoint(
        cp, step_id=steps[0].step_id, result=StepStatus.COMPLETED,
        next_step_id=steps[1].step_id, now=_T1)
    with pytest.raises(HybridPlanError, match="禁止重入已处置步骤"):
        advance_checkpoint(
            cp, step_id=steps[1].step_id, result=StepStatus.COMPLETED,
            next_step_id=steps[0].step_id, now=_T1)


def test_human_gate_suspend_and_resume():
    steps = _steps()
    cp = _checkpoint(steps)
    cp = advance_checkpoint(
        cp, step_id=steps[0].step_id, result=StepStatus.COMPLETED,
        next_step_id=steps[1].step_id, now=_T1)
    cp = advance_checkpoint(
        cp, step_id=steps[1].step_id, result=StepStatus.COMPLETED,
        next_step_id=steps[2].step_id, now=_T1)

    gate_id = gate_id_for("sess-1", steps[2].step_id)
    suspended = suspend_for_human(cp, gate_id=gate_id, now=_T1)
    assert suspended.status is SessionStatus.AWAITING_HUMAN
    assert suspended.gate_ids == [gate_id]

    resumed = resume_from_human(suspended, now=_T1)
    assert resumed.status is SessionStatus.RUNNING
    # 幂等：同一 gate 重复登记不产生重复项
    again = suspend_for_human(
        resumed.model_copy(update={"status": SessionStatus.RUNNING}),
        gate_id=gate_id, now=_T1)
    assert again.gate_ids == [gate_id]


def test_next_pending_step_orders_by_plan():
    steps = _steps()
    cp = _checkpoint(steps)
    # s1 已在 running（登记于 step_statuses），下一个待处置的是 s2
    assert next_pending_step(steps, cp).step_id == "s2-baseline-shot"
    cp = advance_checkpoint(
        cp, step_id=steps[0].step_id, result=StepStatus.COMPLETED,
        next_step_id=steps[1].step_id, now=_T1)
    assert next_pending_step(steps, cp).step_id == "s3-human-lock"

    # 全部处置完
    terminal = cp.model_copy(update={
        "status": SessionStatus.PASSED, "current_step_id": None,
        "completed_steps": [s.step_id for s in steps],
        "step_statuses": {s.step_id: StepStatus.COMPLETED for s in steps}})
    assert next_pending_step(steps, terminal) is None


def test_resume_blocked_without_current_step_rejected():
    orphan = SessionCheckpoint(
        session_id="x", plan_hash="h", status=SessionStatus.BLOCKED,
        current_step_id=None, updated_at="2026-11-14T08:00:00Z")
    with pytest.raises(HybridPlanError, match="缺少 current_step_id"):
        resume_from_blocked(orphan, now=_T1)


# ── 5. 闸门载荷与裁决单 ──────────────────────────────────────────────────────


def test_build_gate_request_happy_path():
    step = _steps()[4]  # human:verdict
    gate = build_gate_request(
        session_id="sess-1", step=step,
        evidence_urns=["urn:testpilot:store:artifact:oslog-ring-chain"],
        now=_T0, deadline_seconds=120)

    assert gate.gate_id == gate_id_for("sess-1", step.step_id)
    assert gate.session_id == "sess-1"
    assert gate.actor is StepActor.HUMAN_VERDICT
    assert gate.status is GateStatus.PENDING
    assert gate.deadline == "2026-11-14T08:02:00Z"
    assert gate.verdict_form is not None
    assert gate.evidence_urns == ["urn:testpilot:store:artifact:oslog-ring-chain"]
    # gate_id 确定性：同会话同步骤反复建单同 id（崩溃重建幂等）
    again = build_gate_request(
        session_id="sess-1", step=step, evidence_urns=[],
        now=_T1, deadline_seconds=60)
    assert again.gate_id == gate.gate_id


def test_gate_rejects_auto_step_and_bad_deadline():
    auto = _steps()[0]
    with pytest.raises(HybridPlanError, match="不是人工步"):
        build_gate_request(session_id="s", step=auto, evidence_urns=[],
                           now=_T0, deadline_seconds=60)
    human = _steps()[2]
    with pytest.raises(HybridPlanError, match="超时必须为正整数"):
        build_gate_request(session_id="s", step=human, evidence_urns=[],
                           now=_T0, deadline_seconds=0)


def _good_verdict(gate: GateRequest) -> HumanVerdict:
    return HumanVerdict(
        gate_id=gate.gate_id,
        values={"ringtone_heard": True, "vibration_felt": True,
                "confidence": "high"},
        evidence_urns=["urn:testpilot:store:artifact:oslog-ring-chain"],
        decided_by="tester-zhang", decided_at="2026-11-14T08:05:00Z")


def test_validate_verdict_accepts_well_formed():
    gate = build_gate_request(
        session_id="sess-1", step=_steps()[4], evidence_urns=[],
        now=_T0, deadline_seconds=120)
    validate_verdict(gate.verdict_form, _good_verdict(gate))  # 不抛即通过


def test_validate_verdict_rejects_bad_values():
    gate = build_gate_request(
        session_id="sess-1", step=_steps()[4], evidence_urns=[],
        now=_T0, deadline_seconds=120)
    form = gate.verdict_form

    base = _good_verdict(gate)

    missing = base.model_copy(update={"values": {"ringtone_heard": True}})
    with pytest.raises(HybridPlanError, match="缺必填字段: vibration_felt"):
        validate_verdict(form, missing)

    bad_bool = base.model_copy(
        update={"values": dict(base.values, ringtone_heard="yes")})
    with pytest.raises(HybridPlanError, match="必须是布尔值"):
        validate_verdict(form, bad_bool)

    bad_choice = base.model_copy(
        update={"values": dict(base.values, confidence="absolute")})
    with pytest.raises(HybridPlanError, match="不在 options"):
        validate_verdict(form, bad_choice)

    wild = base.model_copy(
        update={"values": dict(base.values, ghost_field=1)})
    with pytest.raises(HybridPlanError, match="表单外字段"):
        validate_verdict(form, wild)


def test_validate_verdict_requires_evidence_and_witness():
    gate = build_gate_request(
        session_id="sess-1", step=_steps()[4], evidence_urns=[],
        now=_T0, deadline_seconds=120)
    no_evidence = _good_verdict(gate).model_copy(update={"evidence_urns": []})
    with pytest.raises(HybridPlanError, match="空证据裁决"):
        validate_verdict(gate.verdict_form, no_evidence)

    no_witness = _good_verdict(gate).model_copy(update={"decided_by": "  "})
    with pytest.raises(HybridPlanError, match="decided_by"):
        validate_verdict(gate.verdict_form, no_witness)


def test_optional_text_field_allows_absent_but_rejects_blank():
    form = VerdictForm(require_evidence=False, fields=[
        VerdictField(key="note", label="备注", kind="text", required=False)])
    step = PlanStep(
        step_id="v", actor=StepActor.HUMAN_VERDICT, action="other",
        prompt="补充说明", verdict_form=form,
        evidence_bindings=["urn:testpilot:store:artifact:e1"],
        idempotency_key="k")
    gate = build_gate_request(
        session_id="s", step=step, evidence_urns=[],
        now=_T0, deadline_seconds=60)

    validate_verdict(form, HumanVerdict(
        gate_id=gate.gate_id, values={}, evidence_urns=[],
        decided_by="t1", decided_at="2026-11-14T08:05:00Z"))

    blank = HumanVerdict(
        gate_id=gate.gate_id, values={"note": "   "}, evidence_urns=[],
        decided_by="t1", decided_at="2026-11-14T08:05:00Z")
    with pytest.raises(HybridPlanError, match="非空文本"):
        validate_verdict(form, blank)


# ── 6. 闸门生命周期纯函数（过期/响应/续期/取消）──────────────────────────────


def _action_gate(deadline_seconds: int = 120) -> GateRequest:
    return build_gate_request(
        session_id="sess-1", step=_steps()[2], evidence_urns=[],
        now=_T0, deadline_seconds=deadline_seconds)


def _verdict_gate() -> GateRequest:
    return build_gate_request(
        session_id="sess-1", step=_steps()[4],
        evidence_urns=["urn:testpilot:store:artifact:oslog-ring-chain"],
        now=_T0, deadline_seconds=120)


def test_gate_expiry_is_time_and_status_driven():
    gate = _action_gate()
    assert gate_is_expired(gate, _T0 + timedelta(seconds=60)) is False
    assert gate_is_expired(gate, _T0 + timedelta(seconds=121)) is True

    # 已响应/已取消的闸门不再被时间判过期（终态稳定）
    responded = gate.model_copy(update={"status": GateStatus.RESPONDED})
    assert gate_is_expired(responded, _T0 + timedelta(days=1)) is False

    swept = expire_gate_if_due(gate, _T0 + timedelta(seconds=121))
    assert swept.status is GateStatus.EXPIRED
    # 未到期返回原对象（无副作用语义，便于服务层判变）
    assert expire_gate_if_due(gate, _T0) is gate


def test_gate_expiry_bad_deadline_string_loud():
    broken = _action_gate().model_copy(update={"deadline": "not-a-timestamp"})
    with pytest.raises(HybridPlanError, match="deadline 格式非法"):
        gate_is_expired(broken, _T0)


def test_record_response_happy_and_gate_id_guard():
    gate = _verdict_gate()
    verdict = HumanVerdict(
        gate_id=gate.gate_id,
        values={"ringtone_heard": True, "vibration_felt": True,
                "confidence": "high"},
        evidence_urns=["urn:testpilot:store:artifact:oslog-ring-chain"],
        decided_by="tester-zhang", decided_at="2026-11-14T08:05:00Z")
    updated = record_gate_response(gate, verdict, _T1)
    assert updated.status is GateStatus.RESPONDED
    assert updated.response == verdict

    mismatched = verdict.model_copy(update={"gate_id": "gate-deadbeef0000"})
    with pytest.raises(HybridPlanError, match="与闸门.*不一致"):
        record_gate_response(gate, mismatched, _T1)


def test_action_gate_response_needs_witness_only():
    # human:action 无裁决单：不校验表单，但见证人与时间戳必填
    gate = _action_gate()
    ack = HumanVerdict(
        gate_id=gate.gate_id, values={}, evidence_urns=[],
        decided_by="tester-li", decided_at="2026-11-14T08:03:00Z")
    updated = record_gate_response(gate, ack, _T1)
    assert updated.status is GateStatus.RESPONDED

    ghost = ack.model_copy(update={"decided_by": ""})
    with pytest.raises(HybridPlanError, match="见证人"):
        record_gate_response(gate, ghost, _T1)


def test_expired_gate_must_renew_before_response():
    gate = expire_gate_if_due(
        _verdict_gate(), _T0 + timedelta(seconds=200))
    verdict = HumanVerdict(
        gate_id=gate.gate_id,
        values={"ringtone_heard": True, "vibration_felt": True,
                "confidence": "high"},
        evidence_urns=["urn:testpilot:store:artifact:oslog-ring-chain"],
        decided_by="t1", decided_at="2026-11-14T08:10:00Z")
    with pytest.raises(HybridPlanError, match="已过期.*先续期"):
        record_gate_response(gate, verdict, _T0 + timedelta(seconds=201))

    reopened = renew_gate(gate, _T0 + timedelta(seconds=201), 300)
    assert reopened.status is GateStatus.PENDING
    assert reopened.renewed_at is not None
    accepted = record_gate_response(
        reopened, verdict, _T0 + timedelta(seconds=202))
    assert accepted.status is GateStatus.RESPONDED


def test_renew_rules_and_cancel_terminal_guard():
    gate = _action_gate()
    extended = renew_gate(gate, _T0 + timedelta(seconds=30), 300)
    assert extended.status is GateStatus.PENDING
    assert extended.deadline == "2026-11-14T08:07:00Z"

    with pytest.raises(HybridPlanError, match="正整数秒"):
        renew_gate(gate, _T0, 0)

    responded = record_gate_response(
        gate, HumanVerdict(
            gate_id=gate.gate_id, values={}, evidence_urns=[],
            decided_by="t1", decided_at="2026-11-14T08:02:00Z"), _T1)
    with pytest.raises(HybridPlanError, match="终态不可续期"):
        renew_gate(responded, _T1, 60)
    with pytest.raises(HybridPlanError, match="已响应，不能取消"):
        cancel_gate(responded, _T1)

    canceled = cancel_gate(gate, _T1)
    assert canceled.status is GateStatus.CANCELED
    with pytest.raises(HybridPlanError, match="重复取消"):
        cancel_gate(canceled, _T1)
