"""六类结构化前置探针与 repair 执行（docs/15 §4，H3）。

探针只回答三态：``satisfied`` / ``unsatisfied`` / ``unknown``。unknown 是
诚实的能力圈边界，不是失败——devicectl 词汇表没有进程清单、通知授权归属
可能不在本进程，这类事实主机侧无从观测，按计划的 on_failure 姿态收口
（blocked 或 fail_loud），绝不带假设开跑（用户红线）。

能力映射（2026-11-14 取证）：

- device_unlocked / session_window_materialized：DhAxAdapter.readiness()
  的 screen_locked/buttons/windows 报告；Mac 锁屏（exit 6）或 AX 树脱物化
  （exit 4，docs/14 E31）时给不出结论 → unknown/unsatisfied；
- portrait_orientation：devicectl 截图 + core.png_probe 读像素宽高，
  方向只认截图本身（E27：按压次数不算证据）；
- app_not_running：恒 unknown。devicectl 只能按显式 PID 终止、无进程清单
  动词，主机侧无法可靠枚举 App 进程；带 devicectl_terminate repair 时按
  计划显式 pid 执行后仍只能 unknown，姿态由计划决定；
- no_active_alarms：恒 unknown。App 内闹钟库状态主机侧不可见，唯一机制是
  段内 app_launch_purge_args（xcodebuild 启动参数 → Swift 测试钩子清场），
  清场效果由该段 xcodebuild 结果兜底验证；
- notification_permission：恒 unknown，只接受 human_gate repair（计划期
  已强制 human_gate 配 blocked 姿态）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from testpilot_core.errors import AdapterUnavailableError
from testpilot_core.hybrid_plan import (
    HybridPlanError,
    PlanRepair,
    PlanStep,
    Precondition,
)
from testpilot_core.png_probe import PngProbeError, is_portrait_png

from testpilot_adapters.devicectl_control import DevicectlControlError
from testpilot_adapters.dh_ax import DhAxActionError

# iPhone 不渲染 portraitUpsideDown（E27）：连续 3 次左旋必遍历所有可达方向，
# 给到 4 次上限留一次冗余，仍不转回竖屏就按姿态收口，绝不无限转。
MAX_ROTATE_ATTEMPTS = 4
_DEFAULT_PURGE_ARGS = ["--ak-purge-dates"]


@dataclass
class ProbeContext:
    """单次步骤执行的设备目标上下文（显式传入，不写死机器信息）。"""

    udid: str
    bundle_id: str


@dataclass
class ProbeOutcome:
    kind: str
    status: str  # satisfied | unsatisfied | unknown
    reason: str
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class RepairOutcome:
    verb: str
    ok: bool
    detail: dict[str, Any] = field(default_factory=dict)
    # app_launch_purge_args 无 IO，只产出要并入 xcodebuild 段的启动参数
    launch_arguments: list[str] = field(default_factory=list)
    # human_gate 不在探针层直接建闸门，交编排器合成人工步
    needs_human: bool = False


@dataclass
class PreconditionResolution:
    """一个步骤全部前置的收口结论，编排器据此继续/挂起/失败。"""

    proceed: bool
    audit: list[dict[str, Any]] = field(default_factory=list)
    launch_arguments: list[str] = field(default_factory=list)
    blocked_reason: str | None = None
    human_request: dict[str, Any] | None = None

    @classmethod
    def ready(cls, audit: list[dict[str, Any]],
              launch_arguments: list[str]) -> "PreconditionResolution":
        return cls(proceed=True, audit=audit,
                   launch_arguments=launch_arguments)

    @classmethod
    def blocked(cls, reason: str,
                audit: list[dict[str, Any]]) -> "PreconditionResolution":
        return cls(proceed=False, audit=audit, blocked_reason=reason)

    @classmethod
    def human(cls, request: dict[str, Any],
              audit: list[dict[str, Any]]) -> "PreconditionResolution":
        return cls(proceed=False, audit=audit, human_request=request)


# ── 探针注册表 ────────────────────────────────────────────────────────────────


def _dh_ax(container: Any):
    adapter = container.get("dh_ax")
    if adapter is None:
        raise HybridPlanError("dh_ax 适配器未装配，无法执行 DH 类前置探针")
    return adapter


def _devicectl(container: Any):
    adapter = container.get("devicectl")
    if adapter is None:
        raise HybridPlanError(
            "devicectl 适配器未装配，无法执行截图/进程类前置探针")
    return adapter


async def _probe_device_unlocked(container, ctx: ProbeContext) -> ProbeOutcome:
    try:
        report = await _dh_ax(container).readiness()
    except AdapterUnavailableError as exc:
        return ProbeOutcome("device_unlocked", "unknown",
                           f"DH 环境不可用，锁屏态无法判定：{exc}")
    except DhAxActionError as exc:
        # E31：AX 树脱物化时连 screen_locked 都取不到，不得猜 unlocked
        return ProbeOutcome("device_unlocked", "unknown",
                           f"DH AX 报告失败，锁屏态无法判定：{exc}")
    locked = bool(report.get("screen_locked"))
    return ProbeOutcome(
        "device_unlocked",
        "unsatisfied" if locked else "satisfied",
        "screen_locked=true" if locked else "screen_locked=false",
        evidence={"buttons": report.get("buttons")})


async def _probe_window_materialized(
        container, ctx: ProbeContext) -> ProbeOutcome:
    try:
        report = await _dh_ax(container).readiness()
    except AdapterUnavailableError as exc:
        return ProbeOutcome("session_window_materialized", "unknown",
                           f"DH 环境不可用：{exc}")
    except DhAxActionError as exc:
        # exit 4：零尺寸窗口/工具条按钮不可达 = 会话窗口未物化（E28/E31）
        return ProbeOutcome("session_window_materialized", "unsatisfied",
                           f"工具条/窗口脱物化：{exc}")
    buttons = report.get("buttons", {})
    reachable = all(bool(buttons.get(k))
                    for k in ("home", "screenshot", "rotate_left"))
    ok = bool(report.get("ok")) and reachable
    return ProbeOutcome(
        "session_window_materialized",
        "satisfied" if ok else "unsatisfied",
        "readiness ok 且三按钮可达" if ok else f"readiness 报告不完整：{report}",
        evidence={"buttons": buttons, "windows": report.get("windows")})


async def _probe_portrait(container, ctx: ProbeContext) -> ProbeOutcome:
    try:
        shot = await _devicectl(container).capture_screenshot(ctx.udid)
        data = Path(shot).read_bytes()
        portrait = is_portrait_png(data)
    except (AdapterUnavailableError, DevicectlControlError) as exc:
        return ProbeOutcome("portrait_orientation", "unknown",
                           f"截图通道不可用：{exc}")
    except PngProbeError as exc:
        # 产物不是合法 PNG = 工具契约破坏，按 unknown 交姿态收口，不瞎判方向
        return ProbeOutcome("portrait_orientation", "unknown",
                           f"截图产物无法解析：{exc}")
    return ProbeOutcome(
        "portrait_orientation",
        "satisfied" if portrait else "unsatisfied",
        "截图高>宽，竖屏" if portrait else "截图宽>=高，非竖屏",
        evidence={"screenshot": str(shot)})


async def _probe_app_not_running(
        container, ctx: ProbeContext) -> ProbeOutcome:
    # 能力圈边界：devicectl 无进程清单动词（2026-11-14 词汇表取证），
    # 主机侧无法枚举存活 App 进程；显式记 unknown，禁止假装 satisfied。
    return ProbeOutcome(
        "app_not_running", "unknown",
        "devicectl 无进程清单动词，主机侧不可观测；仅支持按显式 PID 终止")


async def _probe_no_active_alarms(
        container, ctx: ProbeContext) -> ProbeOutcome:
    # App 内闹钟库对主机侧不可见；清场靠段内启动参数钩子，效果由 xcodebuild
    # 段结果验证（证据链闭环），探针本身只能诚实报 unknown。
    return ProbeOutcome(
        "no_active_alarms", "unknown",
        "App 内闹钟库主机侧不可见；依赖 app_launch_purge_args 段内清场")


async def _probe_notification_permission(
        container, ctx: ProbeContext) -> ProbeOutcome:
    return ProbeOutcome(
        "notification_permission", "unknown",
        "通知授权归属可能不在本进程，重置命令未必有效；只能人工现场确认")


_PROBES = {
    "device_unlocked": _probe_device_unlocked,
    "session_window_materialized": _probe_window_materialized,
    "portrait_orientation": _probe_portrait,
    "app_not_running": _probe_app_not_running,
    "no_active_alarms": _probe_no_active_alarms,
    "notification_permission": _probe_notification_permission,
}


# ── repair 执行 ───────────────────────────────────────────────────────────────


async def _repair_dh_home(container, repair: PlanRepair,
                          ctx: ProbeContext) -> RepairOutcome:
    await _dh_ax(container).home()
    return RepairOutcome("dh_home", ok=True)


async def _repair_dh_rotate(container, repair: PlanRepair,
                            ctx: ProbeContext) -> RepairOutcome:
    """转一步截一张，竖屏立即停；上限 MAX_ROTATE_ATTEMPTS，不盲睡不连按。"""
    dh = _dh_ax(container)
    dev = _devicectl(container)
    attempts = 0
    portrait = False
    for attempts in range(1, MAX_ROTATE_ATTEMPTS + 1):
        await dh.rotate_left(1)
        shot = await dev.capture_screenshot(ctx.udid)
        portrait = is_portrait_png(Path(shot).read_bytes())
        if portrait:
            break
    return RepairOutcome(
        "dh_rotate_left", ok=portrait,
        detail={"attempts": attempts, "portrait": portrait})


async def _repair_devicectl_terminate(
        container, repair: PlanRepair, ctx: ProbeContext) -> RepairOutcome:
    pid = repair.params.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        # 计划期无法替运行期发现的 pid 兜底；缺 pid 就是计划不完整，fail-loud
        raise HybridPlanError(
            "devicectl_terminate repair 必须在 params.pid 声明正整数 PID"
            "（devicectl 不支持按 bundle id 终止）")
    await _devicectl(container).terminate_pid(
        ctx.udid, pid, kill=bool(repair.params.get("kill", False)))
    return RepairOutcome("devicectl_terminate", ok=True, detail={"pid": pid})


def _repair_purge_args(container, repair: PlanRepair,
                       ctx: ProbeContext) -> RepairOutcome:
    arguments = repair.params.get("arguments")
    if arguments is None:
        arguments = list(_DEFAULT_PURGE_ARGS)
    if not isinstance(arguments, list) or not arguments \
            or not all(isinstance(a, str) and a for a in arguments):
        raise HybridPlanError(
            "app_launch_purge_args 的 params.arguments 必须是非空字符串列表")
    return RepairOutcome("app_launch_purge_args", ok=True,
                         launch_arguments=list(arguments))


def _repair_human_gate(container, repair: PlanRepair,
                       ctx: ProbeContext, note: str) -> RepairOutcome:
    return RepairOutcome("human_gate", ok=True,
                         needs_human=True, detail={"note": note})


_REPAIR_DISPATCH = {
    "dh_home": _repair_dh_home,
    "dh_rotate_left": _repair_dh_rotate,
    "devicectl_terminate": _repair_devicectl_terminate,
}


async def apply_repair(container, repair: PlanRepair, ctx: ProbeContext,
                       *, note: str = "") -> RepairOutcome:
    if repair.verb == "app_launch_purge_args":
        return _repair_purge_args(container, repair, ctx)
    if repair.verb == "human_gate":
        return _repair_human_gate(container, repair, ctx, note)
    handler = _REPAIR_DISPATCH.get(repair.verb)
    if handler is None:
        # REPAIR_VERBS 闭集已在计划期校验，这里是兜底防线
        raise HybridPlanError(f"未实现的 repair 动词：{repair.verb!r}")
    return await handler(container, repair, ctx)


# ── 步骤级前置收口 ────────────────────────────────────────────────────────────


def _posture(pre: Precondition, reason: str,
             audit: list[dict[str, Any]]) -> PreconditionResolution:
    if pre.on_failure == "fail_loud":
        raise HybridPlanError(
            f"前置 {pre.kind} 无法满足且姿态为 fail_loud：{reason}")
    return PreconditionResolution.blocked(
        f"前置 {pre.kind} 阻塞：{reason}", audit)


async def evaluate_preconditions(
    container, step: PlanStep, ctx: ProbeContext,
    *, acknowledged: frozenset[str] = frozenset(),
) -> PreconditionResolution:
    """逐探针评估步骤前置；不满足且带 repair 时修复后复探一次（有界）。

    unknown 的语义按 repair 类型分流：human_gate → 挂人工；
    app_launch_purge_args → 收启动参数放行（段内结果兜底）；其余 repair
    执行后复探，仍 unknown/unsatisfied 按 on_failure 姿态收口。

    ``acknowledged`` 是本步骤已由人工闸门确认过的前置 kind 集合（崩溃续跑/
    闸门响应后重入同一 running 步骤时传入），命中即记审计并跳过，不再弹卡。
    """
    audit: list[dict[str, Any]] = []
    launch_arguments: list[str] = []

    for pre in step.preconditions:
        if pre.kind in acknowledged:
            audit.append({"step_id": step.step_id, "kind": pre.kind,
                          "status": "acknowledged",
                          "reason": "人工闸门已确认，续跑跳过"})
            continue
        outcome = await _PROBES[pre.kind](container, ctx)
        audit.append({"step_id": step.step_id, "kind": pre.kind,
                      "status": outcome.status, "reason": outcome.reason,
                      "evidence": outcome.evidence})
        if outcome.status == "satisfied":
            continue

        if pre.repair is None:
            return _posture(pre, outcome.reason, audit)

        repair = await apply_repair(
            container, pre.repair, ctx, note=pre.note)
        audit.append({"step_id": step.step_id, "kind": pre.kind,
                      "repair": repair.verb, "ok": repair.ok,
                      "needs_human": repair.needs_human,
                      "detail": repair.detail})

        if repair.needs_human:
            return PreconditionResolution.human(
                {"kind": pre.kind, "note": pre.note or outcome.reason,
                 "repair_verb": repair.verb}, audit)
        launch_arguments.extend(repair.launch_arguments)

        if repair.verb == "app_launch_purge_args":
            # 主机侧不可观测的 App 内状态：清场参数已挂接，效果归该段
            # xcodebuild 结果验证，这里不做无意义复探（探了仍是 unknown）。
            continue

        recheck = await _PROBES[pre.kind](container, ctx)
        audit.append({"step_id": step.step_id, "kind": pre.kind,
                      "reprobe": recheck.status, "reason": recheck.reason})
        if recheck.status != "satisfied":
            return _posture(
                pre, f"修复 {repair.verb} 后仍为 {recheck.status}："
                     f"{recheck.reason}", audit)

    return PreconditionResolution.ready(audit, launch_arguments)
