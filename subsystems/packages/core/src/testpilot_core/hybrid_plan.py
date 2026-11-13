"""hybrid-plan-v1 混合执行计划纯函数（docs/15，H1 切片）。

零 IO、零三方（仅 pydantic + 标准库）：把一条 TestCase 的自动化+人工混合编排
建模为**有序步骤列表**，每步声明执行方（actor）、动作、结构化前置条件、
证据绑定、超时与幂等键。运行时的会话编排在 workers 侧（H3），本模块只管
四件事：结构校验、内容寻址 hash、会话检查点推进、人工裁决单校验。

设计约束（2026-11-14 讨论定案）：

- TestCase 不拆：一条 TC 派生一份计划，人读规格与执行制品分离；
- schema v15 零 DDL：计划/会话/闸门全部是 store 内 JSON 制品，不建表；
- 子 run 复用既有 RunStatus 状态机，会话层另立 SessionStatus（enums.py），
  本模块的检查点规则不允许出现 RunStatus 之外的数据库状态；
- 圈外刺激与建不起来的前置一律 fail-loud/blocked，禁止静默跳过（用户红线）；
- 人工裁决必须绑定系统侧客观证据 URN，不接受空口打勾。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

from .enums import GateStatus, SessionStatus, StepActor, StepStatus
from .models import TestCase

HYBRID_PLAN_SCHEMA_VERSION = "hybrid-plan-v1"

# ── 闭集词表：计划里出现表外字面量一律 HybridPlanError，绝不默默放行 ──────────

# 各 actor 允许的动作词；H3 适配器只实现子集时，未实现动作在运行期再拒绝，
# 计划期先把拼写/越界挡住。
_ACTIONS_BY_ACTOR: dict[StepActor, frozenset[str]] = {
    StepActor.AUTO_XCODEBUILD: frozenset({"run_selector"}),
    StepActor.AUTO_DH_AX: frozenset({"home", "rotate_left", "readiness", "state"}),
    StepActor.AUTO_DEVICECTL: frozenset(
        {"screenshot", "launch_app", "terminate_app", "list_devices"}
    ),
    StepActor.HUMAN_ACTION: frozenset({"lock_screen", "press_side_button", "other"}),
    StepActor.HUMAN_VERDICT: frozenset({"sensory_check", "other"}),
}

# 结构化前置条件种类（probe 语义见 docs/15 §4，运行期探针 H3 实现）。
PRECONDITION_KINDS = frozenset({
    "device_unlocked",            # DH 窗口 frame 非零 + 锁屏键缺席（E24）
    "session_window_materialized",  # DH 会话窗口已物化、工具条可枚举
    "portrait_orientation",       # devicectl 截图尺寸为竖屏
    "app_not_running",            # 目标 bundle 无存活进程
    "no_active_alarms",           # App 测试钩子 purge（启动参数）后库内无活钟
    "notification_permission",    # 通知授权归属可能不在本进程，不满足只能人工
})

# 前置不满足时允许的修复动作闭集；修复本身也要在能力圈内。
REPAIR_VERBS = frozenset({
    "dh_home",                 # DH AX 回主屏（真机已验，docs/14 E25）
    "dh_rotate_left",          # DH AX 旋转（调用方负责转满复原，E27）
    "devicectl_terminate",     # devicectl 终止 App 进程
    "app_launch_purge_args",   # 段内 xcodebuild 带 --ak-purge-dates 等清场参数
    "human_gate",              # 能力圈外：转人工闸门
})

OnFailure = Literal["blocked", "fail_loud"]
FailurePolicy = Literal["abort_session", "block_for_human"]
VerdictFieldKind = Literal["boolean", "choice", "text"]

# 会话迁移闭集：表外迁移拒绝；终态不可变（与 execution.py 同纪律）。
_SESSION_TRANSITIONS: dict[SessionStatus, frozenset[SessionStatus]] = {
    SessionStatus.PENDING: frozenset({
        SessionStatus.RUNNING, SessionStatus.CANCELED}),
    SessionStatus.RUNNING: frozenset({
        SessionStatus.AWAITING_HUMAN, SessionStatus.BLOCKED,
        SessionStatus.PASSED, SessionStatus.FAILED,
        SessionStatus.ERROR, SessionStatus.CANCELED}),
    SessionStatus.AWAITING_HUMAN: frozenset({
        SessionStatus.RUNNING, SessionStatus.BLOCKED, SessionStatus.CANCELED}),
    SessionStatus.BLOCKED: frozenset({
        SessionStatus.RUNNING, SessionStatus.CANCELED, SessionStatus.ERROR}),
    SessionStatus.PASSED: frozenset(),
    SessionStatus.FAILED: frozenset(),
    SessionStatus.ERROR: frozenset(),
    SessionStatus.CANCELED: frozenset(),
}

_STEP_TERMINAL = frozenset(
    {StepStatus.COMPLETED, StepStatus.FAILED, StepStatus.SKIPPED})


class HybridPlanError(ValueError):
    """计划/检查点/裁决单不合法；服务层映射 422，CLI 映射 exit 2。"""


# ── 计划结构 ────────────────────────────────────────────────────────────────


class PlanRepair(BaseModel):
    """前置不满足时的修复动作；verb 必须在能力圈闭集内。"""

    verb: str
    params: dict[str, Any] = Field(default_factory=dict)


class Precondition(BaseModel):
    """结构化前置条件：probe 种类 + 不满足时的姿态。

    - ``blocked``：转人工/等待，基础设施态不记产品失败；
    - ``fail_loud``：直接响亮失败，禁止带假设状态开跑。
    """

    kind: str
    on_failure: OnFailure = "blocked"
    repair: PlanRepair | None = None
    note: str = ""


class VerdictField(BaseModel):
    """人工裁决表单字段；choice 必须给非空 options。"""

    key: str
    label: str
    kind: VerdictFieldKind
    options: list[str] = Field(default_factory=list)
    required: bool = True


class VerdictForm(BaseModel):
    """human:verdict 步骤的裁决单规格；默认强制关联系统证据 URN。"""

    fields: list[VerdictField]
    require_evidence: bool = True


class PlanStep(BaseModel):
    step_id: str
    actor: StepActor
    action: str
    params: dict[str, Any] = Field(default_factory=dict)
    preconditions: list[Precondition] = Field(default_factory=list)
    # 人工步必填的操作说明；自动步可为空。
    prompt: str = ""
    # 仅 human:verdict 使用；其余 actor 必须为空。
    verdict_form: VerdictForm | None = None
    evidence_bindings: list[str] = Field(default_factory=list)
    timeout_seconds: int = Field(default=300, gt=0)
    idempotency_key: str
    failure_policy: FailurePolicy = "abort_session"


class HybridPlan(BaseModel):
    plan_schema_version: str = HYBRID_PLAN_SCHEMA_VERSION
    case_urn: str
    case_external_id: str
    profile: str
    project: str
    steps: list[PlanStep]
    created_at: str
    content_hash: str


# ── 人工闸门与裁决单 ─────────────────────────────────────────────────────────


class GateRequest(BaseModel):
    """人工闸门的挂起载荷（H2 落 store/gates/<gate_id>.json，零 DDL）。"""

    gate_id: str
    session_id: str
    step_id: str
    actor: StepActor
    prompt: str
    verdict_form: VerdictForm | None = None
    evidence_urns: list[str] = Field(default_factory=list)
    deadline: str
    status: GateStatus = GateStatus.PENDING
    created_at: str


class HumanVerdict(BaseModel):
    """人对闸门的响应：结构化裁决值 + 证据 URN + 见证人。"""

    gate_id: str
    values: dict[str, Any] = Field(default_factory=dict)
    evidence_urns: list[str] = Field(default_factory=list)
    note: str = ""
    decided_by: str
    decided_at: str


# ── 会话检查点 ───────────────────────────────────────────────────────────────


class SessionCheckpoint(BaseModel):
    """混合会话游标（H3 每步落盘，崩溃后凭它续跑，不依赖进程内存）。"""

    session_id: str
    plan_hash: str
    status: SessionStatus = SessionStatus.PENDING
    step_statuses: dict[str, StepStatus] = Field(default_factory=dict)
    completed_steps: list[str] = Field(default_factory=list)
    current_step_id: str | None = None
    gate_ids: list[str] = Field(default_factory=list)
    updated_at: str


# ── 校验 ─────────────────────────────────────────────────────────────────────


def _validate_step(step: PlanStep, seen_ids: set[str]) -> None:
    if not step.step_id:
        raise HybridPlanError("存在空 step_id")
    if step.step_id in seen_ids:
        raise HybridPlanError(f"step_id 重复: {step.step_id}")
    seen_ids.add(step.step_id)

    if step.action not in _ACTIONS_BY_ACTOR[step.actor]:
        raise HybridPlanError(
            f"步骤 {step.step_id} 的动作 {step.action!r} 不在 "
            f"{step.actor.value} 允许闭集 {sorted(_ACTIONS_BY_ACTOR[step.actor])} 内")

    if step.actor is StepActor.AUTO_XCODEBUILD:
        test_ids = step.params.get("only_testing")
        if not isinstance(test_ids, list) or not test_ids \
                or not all(isinstance(x, str) and x for x in test_ids):
            raise HybridPlanError(
                f"步骤 {step.step_id}：xcodebuild 段必须在 params.only_testing "
                "声明非空字符串列表（automation-map 绑定的 Swift 方法）")
        if step.verdict_form is not None:
            raise HybridPlanError(f"步骤 {step.step_id}：自动步不得携带 verdict_form")
    elif step.actor is StepActor.AUTO_DH_AX:
        if step.action == "rotate_left" and not isinstance(
                step.params.get("times", 1), int):
            raise HybridPlanError(
                f"步骤 {step.step_id}：rotate_left 的 times 必须是整数")
        if step.verdict_form is not None:
            raise HybridPlanError(f"步骤 {step.step_id}：自动步不得携带 verdict_form")
    elif step.actor is StepActor.AUTO_DEVICECTL:
        if step.action in {"launch_app", "terminate_app"} \
                and not step.params.get("bundle_id"):
            raise HybridPlanError(
                f"步骤 {step.step_id}：{step.action} 必须声明 params.bundle_id")
        if step.verdict_form is not None:
            raise HybridPlanError(f"步骤 {step.step_id}：自动步不得携带 verdict_form")
    elif step.actor is StepActor.HUMAN_ACTION:
        if not step.prompt.strip():
            raise HybridPlanError(
                f"步骤 {step.step_id}：human:action 必须写清人工操作说明 prompt")
        if step.verdict_form is not None:
            raise HybridPlanError(
                f"步骤 {step.step_id}：human:action 用 human:verdict 承载裁决单")
    elif step.actor is StepActor.HUMAN_VERDICT:
        if not step.prompt.strip():
            raise HybridPlanError(
                f"步骤 {step.step_id}：human:verdict 必须写清裁决说明 prompt")
        if step.verdict_form is None or not step.verdict_form.fields:
            raise HybridPlanError(
                f"步骤 {step.step_id}：human:verdict 必须携带非空 verdict_form")

    for pre in step.preconditions:
        if pre.kind not in PRECONDITION_KINDS:
            raise HybridPlanError(
                f"步骤 {step.step_id}：未知前置条件种类 {pre.kind!r}，"
                f"允许集 {sorted(PRECONDITION_KINDS)}")
        if pre.repair is not None:
            if pre.repair.verb not in REPAIR_VERBS:
                raise HybridPlanError(
                    f"步骤 {step.step_id}：修复动作 {pre.repair.verb!r} 不在 "
                    f"能力圈 {sorted(REPAIR_VERBS)} 内")
            if pre.repair.verb == "human_gate" and pre.on_failure != "blocked":
                raise HybridPlanError(
                    f"步骤 {step.step_id}：human_gate 修复只能配 blocked 姿态")

    if step.verdict_form is not None:
        keys = [f.key for f in step.verdict_form.fields]
        if len(keys) != len(set(keys)):
            raise HybridPlanError(f"步骤 {step.step_id}：裁决字段 key 重复")
        for field in step.verdict_form.fields:
            if field.kind == "choice" and not field.options:
                raise HybridPlanError(
                    f"步骤 {step.step_id}：choice 字段 {field.key} 缺 options")


def validate_plan_steps(steps: list[PlanStep]) -> None:
    """计划级校验：非空、step_id 唯一、逐字段合法、人工步位置约束。"""
    if not steps:
        raise HybridPlanError("混合计划至少包含一个步骤，拒绝空壳计划")
    seen: set[str] = set()
    for step in steps:
        _validate_step(step, seen)
    # 人工裁决步必须挂证据绑定或表单要求证据，防止无旁证裁决
    for step in steps:
        if step.actor is StepActor.HUMAN_VERDICT and step.verdict_form is not None:
            if step.verdict_form.require_evidence and not step.evidence_bindings:
                raise HybridPlanError(
                    f"步骤 {step.step_id}：require_evidence=True 时必须在 "
                    "evidence_bindings 声明系统侧证据来源 URN/标识")


def _utc_stamp(now: datetime) -> str:
    if now.tzinfo is None:
        raise HybridPlanError("时间戳必须带时区信息")
    return now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def canonical_plan_hash(payload: dict[str, Any]) -> str:
    """内容寻址 hash：剔除 created_at/content_hash 后 canonical JSON sha256。"""
    normalized = {
        k: v for k, v in payload.items()
        if k not in ("created_at", "content_hash")
    }
    blob = json.dumps(
        normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def plan_filename(content_hash: str, now: datetime) -> str:
    stamp = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"hybrid-plan-{stamp}-{content_hash[:8]}.json"


def build_hybrid_plan(
    *,
    case: TestCase,
    profile: str,
    project: str,
    steps: list[PlanStep],
    now: datetime,
) -> dict[str, Any]:
    """组装 hybrid-plan-v1 载荷（零 IO）；exploratory/cancelled 用例拒绝建计划。"""
    if case.lifecycle != "active":
        raise HybridPlanError(
            f"用例 {case.external_id} lifecycle={case.lifecycle}，"
            "只有 active 用例可建混合计划")
    if case.exploratory:
        raise HybridPlanError(
            f"探索性用例 {case.external_id} 不进自动化链路，拒绝建混合计划")
    validate_plan_steps(steps)

    payload: dict[str, Any] = {
        "plan_schema_version": HYBRID_PLAN_SCHEMA_VERSION,
        "case_urn": case.urn,
        "case_external_id": case.external_id,
        "profile": profile,
        "project": project,
        "steps": [step.model_dump(mode="json") for step in steps],
        "created_at": _utc_stamp(now),
    }
    payload["content_hash"] = canonical_plan_hash(payload)
    return payload


# ── 检查点推进（纯函数，崩溃恢复的依据）──────────────────────────────────────


def transition_session(current: SessionStatus, target: SessionStatus) -> SessionStatus:
    """按会话迁移闭集求下一状态；非法迁移/终态不可变抛 HybridPlanError。"""
    if target not in _SESSION_TRANSITIONS[current]:
        raise HybridPlanError(
            f"非法会话状态迁移：{current.value} ---> {target.value}")
    return target


def start_checkpoint(
    *, session_id: str, plan_hash: str, first_step_id: str, now: datetime
) -> SessionCheckpoint:
    """会话首个步骤开跑：pending → running，首步 running。"""
    return SessionCheckpoint(
        session_id=session_id,
        plan_hash=plan_hash,
        status=transition_session(SessionStatus.PENDING, SessionStatus.RUNNING),
        step_statuses={first_step_id: StepStatus.RUNNING},
        current_step_id=first_step_id,
        updated_at=_utc_stamp(now),
    )


def _require_step(checkpoint: SessionCheckpoint, step_id: str) -> StepStatus:
    try:
        return checkpoint.step_statuses[step_id]
    except KeyError:
        raise HybridPlanError(
            f"会话 {checkpoint.session_id} 不含步骤 {step_id}，拒绝野步骤更新") from None


def advance_checkpoint(
    checkpoint: SessionCheckpoint,
    *,
    step_id: str,
    result: StepStatus,
    next_step_id: str | None,
    now: datetime,
) -> SessionCheckpoint:
    """推进一个步骤并产出新检查点（不就地改入参）。

    - 当前步必须是 running；result 只允许 completed/failed/skipped/blocked；
    - completed/skipped：有下一步则继续 running，无下一步则会话 passed；
    - blocked：会话进 blocked（等人/等环境），current 步骤保留 blocked；
    - failed：按步骤 failure_policy 决定 failed 终态或 blocked 挂起（由调用方
      传入 result=blocked 表达挂起，本函数只处理机械迁移）。
    """
    current = _require_step(checkpoint, step_id)
    if current is not StepStatus.RUNNING:
        raise HybridPlanError(
            f"步骤 {step_id} 当前态 {current.value}，只有 running 步可推进")
    if result not in (StepStatus.COMPLETED, StepStatus.FAILED,
                      StepStatus.SKIPPED, StepStatus.BLOCKED):
        raise HybridPlanError(f"不接受的步骤结果: {result.value}")

    statuses = dict(checkpoint.step_statuses)
    statuses[step_id] = result
    completed = list(checkpoint.completed_steps)
    if result in (StepStatus.COMPLETED, StepStatus.SKIPPED):
        completed.append(step_id)

    if result is StepStatus.BLOCKED:
        return checkpoint.model_copy(update={
            "status": transition_session(checkpoint.status, SessionStatus.BLOCKED),
            "step_statuses": statuses,
            "current_step_id": step_id,
            "updated_at": _utc_stamp(now),
        })

    if result is StepStatus.FAILED:
        return checkpoint.model_copy(update={
            "status": transition_session(checkpoint.status, SessionStatus.FAILED),
            "step_statuses": statuses,
            "current_step_id": step_id,
            "updated_at": _utc_stamp(now),
        })

    if next_step_id is None:
        return checkpoint.model_copy(update={
            "status": transition_session(checkpoint.status, SessionStatus.PASSED),
            "step_statuses": statuses,
            "completed_steps": completed,
            "current_step_id": None,
            "updated_at": _utc_stamp(now),
        })

    if next_step_id in statuses:
        raise HybridPlanError(
            f"下一步 {next_step_id} 已有状态 {statuses[next_step_id].value}，"
            "禁止重入已处置步骤")
    statuses[next_step_id] = StepStatus.RUNNING
    return checkpoint.model_copy(update={
        "status": checkpoint.status,  # running → running 无迁移
        "step_statuses": statuses,
        "completed_steps": completed,
        "current_step_id": next_step_id,
        "updated_at": _utc_stamp(now),
    })


def suspend_for_human(
    checkpoint: SessionCheckpoint, *, gate_id: str, now: datetime
) -> SessionCheckpoint:
    """自动步挂起等人：running → awaiting_human，登记 gate_id。"""
    gates = list(checkpoint.gate_ids)
    if gate_id not in gates:
        gates.append(gate_id)
    return checkpoint.model_copy(update={
        "status": transition_session(
            checkpoint.status, SessionStatus.AWAITING_HUMAN),
        "gate_ids": gates,
        "updated_at": _utc_stamp(now),
    })


def resume_from_human(checkpoint: SessionCheckpoint, *, now: datetime) -> SessionCheckpoint:
    """人工闸门响应后恢复：awaiting_human → running。"""
    return checkpoint.model_copy(update={
        "status": transition_session(
            checkpoint.status, SessionStatus.RUNNING),
        "updated_at": _utc_stamp(now),
    })


def resume_from_blocked(checkpoint: SessionCheckpoint, *, now: datetime) -> SessionCheckpoint:
    """环境阻塞解除后续跑：blocked → running，当前步重回 running。"""
    if checkpoint.current_step_id is None:
        raise HybridPlanError("blocked 检查点缺少 current_step_id，无法续跑")
    statuses = dict(checkpoint.step_statuses)
    statuses[checkpoint.current_step_id] = StepStatus.RUNNING
    return checkpoint.model_copy(update={
        "status": transition_session(checkpoint.status, SessionStatus.RUNNING),
        "step_statuses": statuses,
        "updated_at": _utc_stamp(now),
    })


def next_pending_step(
    steps: list[PlanStep], checkpoint: SessionCheckpoint
) -> PlanStep | None:
    """按计划顺序返回第一个未处置步骤；全部处置完返回 None。"""
    done = set(checkpoint.completed_steps)
    for step in steps:
        if step.step_id not in done and \
                step.step_id not in checkpoint.step_statuses:
            return step
    return None


# ── 闸门载荷与裁决校验 ───────────────────────────────────────────────────────


def gate_id_for(session_id: str, step_id: str) -> str:
    digest = hashlib.sha1(
        f"{session_id}|{step_id}".encode("utf-8")).hexdigest()[:12]
    return f"gate-{digest}"


def build_gate_request(
    *,
    session_id: str,
    step: PlanStep,
    evidence_urns: list[str],
    now: datetime,
    deadline_seconds: int,
) -> GateRequest:
    """从人工步构造挂起闸门载荷；deadline 为绝对 UTC 时间戳。"""
    if step.actor not in (StepActor.HUMAN_ACTION, StepActor.HUMAN_VERDICT):
        raise HybridPlanError(
            f"步骤 {step.step_id} 不是人工步，不能建闸门")
    if deadline_seconds <= 0:
        raise HybridPlanError("闸门超时必须为正整数秒")
    created = now.astimezone(timezone.utc)
    deadline = created + timedelta(seconds=deadline_seconds)
    return GateRequest(
        gate_id=gate_id_for(session_id, step.step_id),
        session_id=session_id,
        step_id=step.step_id,
        actor=step.actor,
        prompt=step.prompt,
        verdict_form=step.verdict_form,
        evidence_urns=list(evidence_urns),
        deadline=deadline.strftime("%Y-%m-%dT%H:%M:%SZ"),
        created_at=_utc_stamp(now),
    )


def validate_verdict(form: VerdictForm, verdict: HumanVerdict) -> None:
    """裁决单校验：必填字段齐、choice 在 options 内、证据要求满足、无野字段。"""
    allowed = {f.key for f in form.fields}
    unknown = set(verdict.values) - allowed
    if unknown:
        raise HybridPlanError(f"裁决包含表单外字段: {sorted(unknown)}")

    for field in form.fields:
        if field.key not in verdict.values:
            if field.required:
                raise HybridPlanError(f"裁决缺必填字段: {field.key}")
            continue
        value = verdict.values[field.key]
        if field.kind == "boolean":
            if not isinstance(value, bool):
                raise HybridPlanError(f"字段 {field.key} 必须是布尔值")
        elif field.kind == "choice":
            if value not in field.options:
                raise HybridPlanError(
                    f"字段 {field.key} 的值 {value!r} 不在 options {field.options}")
        elif field.kind == "text":
            if not isinstance(value, str) or not value.strip():
                raise HybridPlanError(f"字段 {field.key} 必须是非空文本")

    if form.require_evidence and not verdict.evidence_urns:
        raise HybridPlanError(
            "本裁决要求绑定系统侧证据 URN（os_log/截图/录屏），"
            "空证据裁决一律拒绝，防空口打勾")
    if not verdict.decided_by.strip():
        raise HybridPlanError("裁决必须记录 decided_by 见证人")
    if not verdict.decided_at:
        raise HybridPlanError("裁决必须带 decided_at 时间戳")
