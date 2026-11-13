"""混合分段会话编排器（docs/15 §5/§6，H3）。

把 hybrid-plan-v1 计划跑成一个可崩溃续跑的运行时会话，纪律：

- 每推进一步（含挂起/阻塞/账本追加）都整体原子落盘 HybridSessionRecord，
  进程被杀后重启只凭文件从最后检查点继续，不依赖任何内存状态；
- 五 actor 分派闭集：xcodebuild 走 ChildRunPort（H3 测试假件，真实接线 H4）、
  dh-ax/devicectl 走已装配适配器、两个人工 actor 走 gate_service 挂闸门；
- 表外动作/缺适配器/缺子 run 端口一律 HybridPlanError fail-loud，不静默跳过；
- 基础设施不可用（AdapterUnavailableError）→ blocked（不是 failed）；
  动作执行失败按步骤 failure_policy 收口为 failed 或 blocked；
- 人工前置闸门响应后重入同一 running 步骤，凭 ledger 的 responded 记录
  跳过该前置，不重复弹卡。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from testpilot_core.enums import GateStatus, SessionStatus, StepActor, StepStatus
from testpilot_core.errors import AdapterUnavailableError, NotFoundError
from testpilot_core.hybrid_plan import (
    HybridPlan,
    HybridPlanError,
    HybridSessionRecord,
    PlanStep,
    advance_checkpoint,
    resume_from_blocked,
    resume_from_human,
    session_id_for,
    start_checkpoint,
    suspend_for_human,
)

from testpilot_adapters.devicectl_control import DevicectlControlError
from testpilot_adapters.dh_ax import DhAxActionError
from testpilot_workers import gate_service
from testpilot_workers.hybrid_probes import (
    PreconditionResolution,
    ProbeContext,
    evaluate_preconditions,
)

# 自动步连环推进的安全上限：正常计划步骤数远小于此，触顶说明状态机出错，
# fail-loud 而不是无限循环烧设备。
DRIVE_ITERATION_CAP = 100
_TERMINAL = frozenset({
    SessionStatus.PASSED, SessionStatus.FAILED,
    SessionStatus.ERROR, SessionStatus.CANCELED,
})
_WAIT = frozenset({SessionStatus.AWAITING_HUMAN, SessionStatus.BLOCKED})


class HybridRunnerError(RuntimeError):
    """编排器自身契约破坏（迭代触顶/状态与计划不一致）。"""


@dataclass
class ChildRunRequest:
    """xcodebuild 段的子 run 派发载荷；idempotency_key 供端口去重续跑。"""

    only_testing: list[str]
    launch_arguments: list[str]
    idempotency_key: str


class ChildRunPort(Protocol):
    """子 run 端口协议：H3 hermetic 测试用假件，H4 接 execution_service。"""

    async def submit(self, request: ChildRunRequest) -> str:
        """提交一段 xcodebuild 选择器，返回 run_ref（账本留痕用）。"""
        ...


# ── 装配取件与小工具 ──────────────────────────────────────────────────────────


def _store(container: Any):
    store = container.get("hybrid_session_store")
    if store is None:
        raise HybridPlanError(
            "hybrid_session_store 未装配（检查 factory.build_container）")
    return store


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(timezone.utc)


def _stamp(now: datetime) -> str:
    return now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _append_ledger(record: HybridSessionRecord, entry: dict[str, Any],
                   now: datetime) -> None:
    record.ledger.append({"at": _stamp(now), **entry})


def _plan_steps(record: HybridSessionRecord) -> list[PlanStep]:
    return HybridPlan.model_validate(record.plan).steps


def _current_step(record: HybridSessionRecord) -> PlanStep:
    current_id = record.checkpoint.current_step_id
    for step in _plan_steps(record):
        if step.step_id == current_id:
            return step
    raise HybridRunnerError(
        f"检查点 current_step_id={current_id} 在计划中不存在，会话状态损坏")


def _persist(container: Any, record: HybridSessionRecord) -> HybridSessionRecord:
    _store(container).put(record)
    return record


# ── 会话生命周期 ──────────────────────────────────────────────────────────────


def start_session(
    container: Any, plan_payload: dict[str, Any], *,
    now: datetime | None = None,
) -> HybridSessionRecord:
    """从计划载荷建会话；同用例同计划版本派生同 session_id，重建幂等。"""
    moment = _now(now)
    plan = HybridPlan.model_validate(plan_payload)
    session_id = session_id_for(plan.case_external_id, plan.content_hash)

    existing = _store(container).get(session_id)
    if existing is not None:
        return existing

    checkpoint = start_checkpoint(
        session_id=session_id, plan_hash=plan.content_hash,
        first_step_id=plan.steps[0].step_id, now=moment)
    record = HybridSessionRecord(
        session_id=session_id, plan=dict(plan_payload),
        checkpoint=checkpoint, ledger=[])
    _append_ledger(record, {"type": "session_started",
                            "plan_hash": plan.content_hash,
                            "step_count": len(plan.steps)}, moment)
    return _persist(container, record)


def get_session(container: Any, session_id: str) -> HybridSessionRecord:
    record = _store(container).get(session_id)
    if record is None:
        raise NotFoundError(f"混合会话不存在: {session_id}")
    return record


async def drive_until_wait(
    container: Any, session_id: str, ctx: ProbeContext, *,
    now: datetime | None = None,
) -> HybridSessionRecord:
    """连续推进自动步，直到 awaiting_human/blocked/终态；每次迁移都落盘。"""
    record = get_session(container, session_id)
    for _ in range(DRIVE_ITERATION_CAP):
        if record.checkpoint.status in _TERMINAL or \
                record.checkpoint.status in _WAIT:
            return record
        record = await _drive_once(container, record, ctx, _now(now))
    raise HybridRunnerError(
        f"会话 {session_id} 推进超过 {DRIVE_ITERATION_CAP} 步未收口，"
        "疑似状态机循环，fail-loud 停机")


async def unblock(
    container: Any, session_id: str, ctx: ProbeContext, *,
    now: datetime | None = None,
) -> HybridSessionRecord:
    """环境阻塞解除后续跑：blocked → running，再驱动到下一个等待点。"""
    moment = _now(now)
    record = get_session(container, session_id)
    if record.checkpoint.status is not SessionStatus.BLOCKED:
        raise HybridPlanError(
            f"会话 {session_id} 当前态 {record.checkpoint.status.value}，"
            "只有 blocked 会话可 unblock")
    record.checkpoint = resume_from_blocked(record.checkpoint, now=moment)
    _append_ledger(record, {"type": "session_unblocked"}, moment)
    _persist(container, record)
    return await drive_until_wait(container, session_id, ctx, now=moment)


async def resume_after_gate(
    container: Any, session_id: str, gate_id: str, ctx: ProbeContext, *,
    now: datetime | None = None,
) -> HybridSessionRecord:
    """人工闸门响应后续跑：校验归属与终态，再按前置闸门/步骤闸门两条路收口。"""
    moment = _now(now)
    record = get_session(container, session_id)
    if record.checkpoint.status is not SessionStatus.AWAITING_HUMAN:
        raise HybridPlanError(
            f"会话 {session_id} 当前态 {record.checkpoint.status.value}，"
            "只有 awaiting_human 会话可响应闸门后续跑")

    gate = gate_service.get_gate(container, gate_id, now=moment)
    if gate.session_id != session_id:
        raise HybridPlanError(
            f"闸门 {gate_id} 属于会话 {gate.session_id}，与 {session_id} 不一致")
    if any(entry.get("type") == "gate_responded"
           and entry.get("gate_id") == gate_id for entry in record.ledger):
        # 同一闸门重复续跑会让人工步被推进两次（检查点重入保护之外的账本守卫）
        raise HybridPlanError(
            f"闸门 {gate_id} 已消费过一次，拒绝重复续跑")
    if gate.status is not GateStatus.RESPONDED:
        raise HybridPlanError(
            f"闸门 {gate_id} 状态为 {gate.status.value}，"
            "只有 responded 闸门可驱动续跑（pending 请等待，expired 请先续期）")

    record.checkpoint = resume_from_human(record.checkpoint, now=moment)
    decided_by = gate.response.decided_by if gate.response else ""
    _append_ledger(record, {
        "type": "gate_responded", "gate_id": gate_id,
        "gate_step_id": gate.step_id, "decided_by": decided_by}, moment)

    if "#pre-" in gate.step_id:
        # 前置人工闸门：当前 running 步骤不变，登记 acknowledged 后重入，
        # evaluate_preconditions 凭该记录跳过对应前置，不重复弹卡。
        kind = gate.step_id.split("#pre-", 1)[1]
        _append_ledger(record, {
            "type": "precondition_gate", "gate_id": gate_id,
            "step_id": record.checkpoint.current_step_id,
            "kind": kind, "status": "responded"}, moment)
    else:
        # 步骤闸门：人工步本身完成，推进到下一计划步骤
        step = _step_by_id(record, gate.step_id)
        record = _advance(
            container, record, step, StepStatus.COMPLETED, moment)
    _persist(container, record)
    return await drive_until_wait(container, session_id, ctx, now=moment)


# ── 单步驱动 ──────────────────────────────────────────────────────────────────


def _step_by_id(record: HybridSessionRecord, step_id: str) -> PlanStep:
    for step in _plan_steps(record):
        if step.step_id == step_id:
            return step
    raise HybridRunnerError(f"闸门引用了计划中不存在的步骤: {step_id}")


def _acknowledged_kinds(
        record: HybridSessionRecord, step_id: str) -> frozenset[str]:
    return frozenset(
        entry["kind"] for entry in record.ledger
        if entry.get("type") == "precondition_gate"
        and entry.get("step_id") == step_id
        and entry.get("status") == "responded")


async def _drive_once(
    container: Any, record: HybridSessionRecord,
    ctx: ProbeContext, moment: datetime,
) -> HybridSessionRecord:
    step = _current_step(record)

    resolution = await evaluate_preconditions(
        container, step, ctx,
        acknowledged=_acknowledged_kinds(record, step.step_id))
    _append_ledger(record, {
        "type": "preconditions_evaluated", "step_id": step.step_id,
        "proceed": resolution.proceed,
        "launch_arguments": resolution.launch_arguments,
        "audit": resolution.audit}, moment)

    if resolution.human_request is not None:
        return _open_precondition_gate(
            container, record, step, resolution, moment)
    if not resolution.proceed:
        _append_ledger(record, {
            "type": "step_blocked", "step_id": step.step_id,
            "reason": resolution.blocked_reason}, moment)
        return _advance(container, record, step, StepStatus.BLOCKED, moment)

    if step.actor in (StepActor.HUMAN_ACTION, StepActor.HUMAN_VERDICT):
        gate = gate_service.open_gate(
            container, session_id=record.session_id, step=step,
            evidence_urns=list(step.evidence_bindings),
            deadline_seconds=step.timeout_seconds, now=moment)
        record.checkpoint = suspend_for_human(
            record.checkpoint, gate_id=gate.gate_id, now=moment)
        _append_ledger(record, {
            "type": "human_gate", "gate_id": gate.gate_id,
            "step_id": step.step_id, "status": "pending"}, moment)
        return _persist(container, record)

    try:
        detail = await _dispatch_auto(container, step, ctx, resolution)
    except AdapterUnavailableError as exc:
        # 基础设施缺席（设备掉线/工具链不可用）= blocked，绝不记产品失败
        _append_ledger(record, {
            "type": "step_blocked", "step_id": step.step_id,
            "reason": f"适配器不可用：{exc}"}, moment)
        return _advance(container, record, step, StepStatus.BLOCKED, moment)
    except (DhAxActionError, DevicectlControlError) as exc:
        return _fail_or_block(container, record, step, exc, moment)

    _append_ledger(record, {
        "type": "auto_step_completed", "step_id": step.step_id,
        "actor": step.actor.value, "action": step.action, "detail": detail},
        moment)
    return _advance(container, record, step, StepStatus.COMPLETED, moment)


def _open_precondition_gate(
    container: Any, record: HybridSessionRecord, step: PlanStep,
    resolution: PreconditionResolution, moment: datetime,
) -> HybridSessionRecord:
    request = resolution.human_request or {}
    kind = request["kind"]
    synthetic = PlanStep(
        step_id=f"{step.step_id}#pre-{kind}",
        actor=StepActor.HUMAN_ACTION, action="other",
        prompt=f"前置条件 {kind} 需人工现场确认：{request.get('note', '')}",
        idempotency_key=f"{step.idempotency_key}|pre-{kind}")
    gate = gate_service.open_gate(
        container, session_id=record.session_id, step=synthetic,
        evidence_urns=[], deadline_seconds=step.timeout_seconds, now=moment)
    record.checkpoint = suspend_for_human(
        record.checkpoint, gate_id=gate.gate_id, now=moment)
    _append_ledger(record, {
        "type": "precondition_gate", "gate_id": gate.gate_id,
        "step_id": step.step_id, "kind": kind, "status": "pending"}, moment)
    return _persist(container, record)


def _fail_or_block(
    container: Any, record: HybridSessionRecord, step: PlanStep,
    exc: Exception, moment: datetime,
) -> HybridSessionRecord:
    if step.failure_policy == "block_for_human":
        _append_ledger(record, {
            "type": "step_blocked", "step_id": step.step_id,
            "reason": f"动作失败转人工：{exc}"}, moment)
        return _advance(container, record, step, StepStatus.BLOCKED, moment)
    _append_ledger(record, {
        "type": "step_failed", "step_id": step.step_id,
        "reason": str(exc)}, moment)
    return _advance(container, record, step, StepStatus.FAILED, moment)


def _advance(
    container: Any, record: HybridSessionRecord, step: PlanStep,
    result: StepStatus, moment: datetime,
) -> HybridSessionRecord:
    steps = _plan_steps(record)
    index = next(i for i, s in enumerate(steps) if s.step_id == step.step_id)
    next_id = steps[index + 1].step_id if index + 1 < len(steps) else None
    record.checkpoint = advance_checkpoint(
        record.checkpoint, step_id=step.step_id, result=result,
        next_step_id=next_id, now=moment)
    return _persist(container, record)


# ── 自动 actor 分派（闭集，表外动作 fail-loud）────────────────────────────────


async def _dispatch_auto(
    container: Any, step: PlanStep, ctx: ProbeContext,
    resolution: PreconditionResolution,
) -> dict[str, Any]:
    actor = step.actor
    if actor is StepActor.AUTO_DH_AX:
        return await _dispatch_dh_ax(container, step)
    if actor is StepActor.AUTO_DEVICECTL:
        return await _dispatch_devicectl(container, step, ctx)
    if actor is StepActor.AUTO_XCODEBUILD:
        return await _dispatch_xcodebuild(container, step, resolution)
    raise HybridPlanError(
        f"步骤 {step.step_id} 的 actor {actor.value} 不是自动 actor，分派拒绝")


async def _dispatch_dh_ax(container: Any, step: PlanStep) -> dict[str, Any]:
    dh = container.get("dh_ax")
    if dh is None:
        raise HybridPlanError("dh_ax 适配器未装配，无法执行 DH AX 步骤")
    if step.action == "home":
        return await dh.home()
    if step.action == "rotate_left":
        return await dh.rotate_left(int(step.params.get("times", 1)))
    if step.action == "readiness":
        return await dh.readiness()
    # state 未在 H3 接线：明确拒绝，禁止静默跳过（用户红线）
    raise HybridPlanError(
        f"auto:dh-ax 动作 {step.action!r} 尚未接线，拒绝静默跳过")


async def _dispatch_devicectl(
    container: Any, step: PlanStep, ctx: ProbeContext
) -> dict[str, Any]:
    dev = container.get("devicectl")
    if dev is None:
        raise HybridPlanError("devicectl 适配器未装配，无法执行设备控制步骤")
    if step.action == "screenshot":
        path = await dev.capture_screenshot(ctx.udid)
        return {"screenshot": str(path)}
    if step.action == "launch_app":
        await dev.launch_app(
            ctx.udid, step.params["bundle_id"],
            arguments=list(step.params.get("arguments", [])),
            terminate_existing=bool(step.params.get("terminate_existing", False)))
        return {"launched": step.params["bundle_id"]}
    if step.action == "terminate_app":
        pid = step.params.get("pid")
        if not isinstance(pid, int) or pid <= 0:
            raise HybridPlanError(
                f"步骤 {step.step_id}：terminate_app 必须在 params.pid 声明"
                "正整数（devicectl 按 PID 终止，无 bundle 终止动词）")
        await dev.terminate_pid(ctx.udid, pid)
        return {"terminated_pid": pid}
    raise HybridPlanError(
        f"auto:devicectl 动作 {step.action!r} 尚未接线，拒绝静默跳过")


async def _dispatch_xcodebuild(
    container: Any, step: PlanStep, resolution: PreconditionResolution
) -> dict[str, Any]:
    port = container.get("hybrid_child_run")
    if port is None:
        # H3 不静默走旧 runner：混合段必须显式接端子 run，真实接线在 H4
        raise HybridPlanError(
            "hybrid_child_run 端口未装配：xcodebuild 混合段拒绝裸跑，"
            "请在容器装配 ChildRunPort 实现")
    request = ChildRunRequest(
        only_testing=list(step.params["only_testing"]),
        launch_arguments=list(resolution.launch_arguments),
        idempotency_key=step.idempotency_key)
    run_ref = await port.submit(request)
    return {"run_ref": run_ref,
            "only_testing": request.only_testing,
            "launch_arguments": request.launch_arguments}
