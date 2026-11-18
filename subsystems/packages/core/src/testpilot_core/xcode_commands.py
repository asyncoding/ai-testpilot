"""iOS xcodebuild/simctl 命令拼装与环境能力纯逻辑（S3.2，FR-EX-003/004）。

防腐纪律：core 不碰子进程、不 import xcrun/xcodebuild。本模块只产出 argv 列表
与判定结论，真正的进程执行在 adapters（SimctlEnvironment / XcodeTestRunner），
因此全部逻辑可在无 macOS/无模拟器的 hostless 测试里钉死。

四块内容：
- ``-only-testing`` 标识符校验与 xcodebuild argv 拼装（docs/03 §3.1/3.2）；
- simctl shutdown→erase 重置计划（docs/03 §3 注意事项：erase 前必须先 shutdown）；
- 能力匹配纯判定（FR-EX-004 AC1：真机能力在模拟器环境显式拒绝并说明未覆盖层）；
- 环境租约过期回收判定（FR-EX-004 AC2：租约泄漏有超时回收）。
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime

from pydantic import BaseModel

from .errors import UnsupportedCapabilityError

# 真机独占能力：模拟器在物理层就给不出，拒绝时必须说清"未覆盖层"而不是含糊报错
# （docs/08 FR-EX-004 AC1 点名后台音频/签名；ios.device 是真机环境标记本身）。
DEVICE_ONLY_CAPABILITIES = frozenset(
    {"ios.device", "background_audio", "codesigning"}
)

CAPABILITY_OK = "ok"
CAPABILITY_DEVICE_ONLY = "device_only_capability"
CAPABILITY_NOT_OFFERED = "capability_not_offered"

# xcodebuild -destination platform 白名单：模拟器与真机两条目的地形态，
# 防 profile 拼出任意 platform 字符串进 argv（真机切片一，2026-11-13）。
DESTINATION_PLATFORM_SIMULATOR = "iOS Simulator"
DESTINATION_PLATFORM_DEVICE = "iOS"
DESTINATION_PLATFORMS = frozenset(
    {DESTINATION_PLATFORM_SIMULATOR, DESTINATION_PLATFORM_DEVICE}
)

# Suite / Suite/Class / Suite/Class/method 三段以内，字母数字下划线开头。
# 既覆盖 AlarmKitSpikeTests 套件级，也覆盖单条 XCUITest 全限定名。
_ONLY_TESTING_RE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(/[A-Za-z_][A-Za-z0-9_]*){0,2}$"
)
# udid 进 -destination 字符串拼接，禁止空白与引号注入。
_UDID_RE = re.compile(r"^[A-Za-z0-9\-]+$")


def _require_nonempty(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"xcodebuild 参数 {name} 不能为空")


def validate_only_testing(identifier: str) -> str:
    """校验 -only-testing 标识符形态，防注入/防手滑写错静默跑全量。"""
    if not isinstance(identifier, str) or not _ONLY_TESTING_RE.match(identifier):
        raise ValueError(
            f"非法 -only-testing 标识符（只允许 Suite[/Class[/method]] 形态）: {identifier!r}"
        )
    return identifier


def build_xcodebuild_argv(
    *,
    workspace: str,
    scheme: str,
    udid: str,
    only_testing: list[str],
    result_bundle_path: str,
    destination_platform: str = DESTINATION_PLATFORM_SIMULATOR,
) -> list[str]:
    """拼 xcodebuild test argv（docs/03 §3.1/3.2）。

    始终带显式 ``-only-testing``：POC 纪律是"选择执行"，空选择直接拒，
    杜绝标识符写错时静默退化为全量跑（单条 UI 测试可达十余分钟）。

    ``destination_platform`` 白名单二选一（真机切片一）：默认
    ``iOS Simulator`` 走模拟器池；真机 profile 传 ``iOS``，目的地变为
    ``platform=iOS,id=<硬件UDID>``，udid 均为硬件/模拟器标识，同一套注入校验。
    """
    _require_nonempty("workspace", workspace)
    _require_nonempty("scheme", scheme)
    _require_nonempty("result_bundle_path", result_bundle_path)
    _require_nonempty("udid", udid)
    if not _UDID_RE.match(udid):
        raise ValueError(f"非法设备 udid（只允许字母数字与连字符）: {udid!r}")
    if destination_platform not in DESTINATION_PLATFORMS:
        raise ValueError(
            f"非法 destination_platform（白名单 {sorted(DESTINATION_PLATFORMS)}）: "
            f"{destination_platform!r}"
        )
    if not only_testing:
        raise ValueError("only_testing 不能为空：本系统只做选择执行，不跑无范围全量")

    argv = [
        "xcodebuild",
        "test",
        "-workspace",
        workspace,
        "-scheme",
        scheme,
        "-destination",
        f"platform={destination_platform},id={udid}",
    ]
    for identifier in only_testing:
        argv.extend(("-only-testing", validate_only_testing(identifier)))
    argv.extend(("-resultBundlePath", result_bundle_path))
    return argv


def build_xcodebuild_xctestrun_argv(
    *,
    xctestrun_path: str,
    udid: str,
    only_testing: list[str],
    result_bundle_path: str | None = None,
    destination_platform: str = DESTINATION_PLATFORM_SIMULATOR,
    maximum_test_execution_time_allowance: int | None = None,
) -> list[str]:
    """拼 ``xcodebuild test-without-building`` argv（USB relay 转正，docs/03 §3.8）。

    与 :func:`build_xcodebuild_argv` 的区别：消费 ``build-for-testing`` 预构建的
    ``.xctestrun``（relay 场景的注入副本，env 已在副本里改好），因此没有
    workspace/scheme，改为 ``-xctestrun`` 直指产物；runbook 02/03 手工命令里的
    ``-only-testing:$OID`` 冒号形式与这里的两参数形式对 xcodebuild 完全等价，
    产品内统一两参数形式（同 build_xcodebuild_argv 惯例）。

    仍然只做选择执行：``only_testing`` 为空直接拒，防静默退化为全量。
    ``maximum_test_execution_time_allowance`` 给秒级正整数（对应 runbook 的 600），
    必须大于被测用例内全部闸门硬超时之和，由上层 profile 兜底，本函数只做形态校验。
    """
    _require_nonempty("xctestrun_path", xctestrun_path)
    _require_nonempty("udid", udid)
    if not _UDID_RE.match(udid):
        raise ValueError(f"非法设备 udid（只允许字母数字与连字符）: {udid!r}")
    if destination_platform not in DESTINATION_PLATFORMS:
        raise ValueError(
            f"非法 destination_platform（白名单 {sorted(DESTINATION_PLATFORMS)}）: "
            f"{destination_platform!r}"
        )
    if not only_testing:
        raise ValueError("only_testing 不能为空：本系统只做选择执行，不跑无范围全量")
    if result_bundle_path is not None:
        _require_nonempty("result_bundle_path", result_bundle_path)
    if maximum_test_execution_time_allowance is not None:
        # bool 是 int 子类，显式挡掉 True/False 这种手滑值
        if (
            isinstance(maximum_test_execution_time_allowance, bool)
            or not isinstance(maximum_test_execution_time_allowance, int)
            or maximum_test_execution_time_allowance <= 0
        ):
            raise ValueError(
                "maximum_test_execution_time_allowance 必须是正整数秒: "
                f"{maximum_test_execution_time_allowance!r}"
            )

    argv = [
        "xcodebuild",
        "test-without-building",
        "-xctestrun",
        xctestrun_path,
        "-destination",
        f"platform={destination_platform},id={udid}",
    ]
    for identifier in only_testing:
        argv.extend(("-only-testing", validate_only_testing(identifier)))
    if result_bundle_path is not None:
        argv.extend(("-resultBundlePath", result_bundle_path))
    if maximum_test_execution_time_allowance is not None:
        argv.extend((
            "-maximum-test-execution-time-allowance",
            str(maximum_test_execution_time_allowance),
        ))
    return argv


def build_simctl_shutdown_argv(udid: str) -> list[str]:
    _require_nonempty("udid", udid)
    if not _UDID_RE.match(udid):
        raise ValueError(f"非法模拟器 udid: {udid!r}")
    return ["xcrun", "simctl", "shutdown", udid]


def build_simctl_erase_argv(udid: str) -> list[str]:
    _require_nonempty("udid", udid)
    if not _UDID_RE.match(udid):
        raise ValueError(f"非法模拟器 udid: {udid!r}")
    return ["xcrun", "simctl", "erase", udid]


def simctl_reset_plan(udid: str) -> list[list[str]]:
    """模拟器重置计划：**顺序固定 shutdown → erase**（docs/03 §3，erase 必须先 shutdown）。

    返回 argv 序列而非直接执行，adapter 逐条跑、测试可逐字断言顺序。
    """
    return [build_simctl_shutdown_argv(udid), build_simctl_erase_argv(udid)]


class CapabilityDecision(BaseModel):
    supported: bool
    unsupported: list[str] = []
    reason: str = CAPABILITY_OK
    message: str = ""

    def raise_if_unsupported(self) -> None:
        if not self.supported:
            raise UnsupportedCapabilityError(self.message)


def evaluate_capabilities(
    required: list[str],
    offered: list[str],
    *,
    provider: str = "ios-simulator",
    device_capable: bool = False,
) -> CapabilityDecision:
    """判定环境能否满足能力要求（纯函数，FR-EX-004 AC1）。

    两类拒绝要分清：真机独占能力（模拟器物理层不具备，需说明未覆盖层）与
    设备清单未提供的能力。真机独占优先报，因为它换一台模拟器也解决不了。

    真机切片一（2026-11-13）：``device_capable=True`` 表示 provider 本身就是真机
    环境（ios-device），此时"真机独占能力"不再天然拒绝，而是与普通能力一样要求
    清单**显式声明**——profile 没给某台真机贴 ``background_audio`` 标签就不能租它
    跑后台音频用例，防止能力假设静默成立。默认 False 保持模拟器历史行为不变。
    """
    if not required:
        return CapabilityDecision(supported=True)

    offered_set = set(offered)
    device_only = sorted(c for c in required if c in DEVICE_ONLY_CAPABILITIES)
    not_offered = sorted(
        c
        for c in required
        if c not in DEVICE_ONLY_CAPABILITIES and c not in offered_set
    )
    if device_capable:
        # 真机环境：独占能力也要清单显式提供，未提供的并入普通缺口统一报
        unresolved_device_only = [c for c in device_only if c not in offered_set]
        not_offered = sorted(set(not_offered) | set(unresolved_device_only))
        device_only_blocked = []
    else:
        device_only_blocked = device_only
    unsupported = sorted(set(device_only_blocked) | set(not_offered))
    if not unsupported:
        return CapabilityDecision(supported=True)

    if device_only_blocked:
        return CapabilityDecision(
            supported=False,
            unsupported=unsupported,
            reason=CAPABILITY_DEVICE_ONLY,
            message=(
                f"unsupported_capability：{device_only_blocked} 属真机独占能力，"
                f"{provider} 环境未覆盖（未覆盖层：真机后台音频/推送/签名校验等"
                "物理设备行为，模拟器无法验证）；其余缺口："
                f"{not_offered or '无'}"
            ),
        )
    return CapabilityDecision(
        supported=False,
        unsupported=unsupported,
        reason=CAPABILITY_NOT_OFFERED,
        message=(
            f"unsupported_capability：当前 {provider} 设备清单不提供 {not_offered}，"
            "请检查能力标签或更换环境"
        ),
    )


class EnvLease(BaseModel):
    lease_id: str
    env_id: str
    request_id: str
    acquired_at: datetime
    expires_at: datetime


def env_lease_id(env_id: str, request_id: str, acquired_at: datetime) -> str:
    """环境租约内容寻址 ID：同设备同请求同时刻重复登记得到同一 ID。"""
    digest = hashlib.sha1(
        f"{env_id}|{request_id}|{acquired_at.isoformat()}".encode("utf-8")
    ).hexdigest()
    return f"envlease-{digest[:12]}"


def is_env_lease_expired(lease: EnvLease, *, now: datetime) -> bool:
    """到期判定：now >= expires_at 即过期（租约有效期是半开区间 [acquired, expires)）。"""
    return now >= lease.expires_at


def reclaimable_env_leases(leases: list[EnvLease], *, now: datetime) -> list[EnvLease]:
    """挑出所有过期租约供 adapter 回收（FR-EX-004 AC2），未到期绝不抢。"""
    return [lease for lease in leases if is_env_lease_expired(lease, now=now)]
