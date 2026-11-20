"""``devicectl list devices`` 命令拼装与 JSON 解析纯函数（真机切片一，零 IO）。

防腐纪律同 xcode_commands：本模块不碰子进程，只产出 argv 与解析结论，进程调用在
adapters/ios_device_env.py，全部判定可在 hostless 测试里钉死。

本机取证（Xcode 27.0，2026-11-13，真机 iPhone 16 Pro / iOS 27.2）：

- ``devicectl list devices --json-output -`` 的 ``-`` 表示 stdout（命令帮助原文示例），
  脚本/CI 不需要临时文件即可拿到结构化清单；
- 返回体顶层 ``result.devices[]``；新形态字段全部挂在 ``properties`` 字典下
  （connection/hardware/software/state 四组），旧顶层键 hardwareProperties/
  deviceProperties/connectionProperties 已带弃用通知，**本模块只读新形态**；
- 顶层 ``identifier`` 是 CoreDevice UUID（如 ``9D3A93E6-…``），**不是**硬件 UDID；
  xcodebuild ``-destination id=`` 与 ``log collect --device-udid`` 要的硬件 UDID 在
  ``properties.hardware.udid``（如 ``00008140-001A39590162801C``）；
- 连接态读 ``properties.connection.state``：实测仅 ``connected`` / ``disconnected``；
  未知态一律保守视为不可用（fail-closed），不猜测未来枚举语义；
- 真机/模拟器区分读 ``properties.hardware.reality``（physical / simulated）。
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel

from .errors import SchemaValidationError

# 硬件 UDID：真机 25 位（含连字符）、模拟器大写 UUID；统一字母数字+连字符白名单，
# 与 xcode_commands._UDID_RE 同口径，防 argv 注入。
_UDID_RE = re.compile(r"^[A-Za-z0-9\-]+$")

# 连接态白名单：只认实测的 connected；其余（含未来新增枚举）保守判不可用。
CONNECTED_STATE = "connected"


class DeviceInventoryEntry(BaseModel):
    """devicectl 清单中的一台设备（只保留平台决策需要的字段，原样大 JSON 不入模型）。"""

    udid: str  # 硬件 UDID：xcodebuild destination / log collect --device-udid 用它
    coredevice_id: str  # 顶层 identifier（CoreDevice UUID），诊断/日志用
    name: str
    platform: str  # properties.hardware.platform：iOS / watchOS ...
    reality: str  # properties.hardware.reality：physical / simulated
    connection_state: str  # properties.connection.state
    boot_state: str  # properties.state.bootState：booted / shutdown ...
    visibility_class: str = ""  # simulators / default ...

    @property
    def is_physical(self) -> bool:
        return self.reality == "physical"

    @property
    def is_connected(self) -> bool:
        return self.connection_state == CONNECTED_STATE


def build_devicectl_list_argv() -> list[str]:
    """盘点 argv：JSON 走 stdout（``-``），无 shell、无隐式设备目标。"""
    return ["xcrun", "devicectl", "list", "devices", "--json-output", "-"]


def validate_udid(udid: str) -> str:
    """硬件 UDID 形态校验；空串/含空白或引号一律拒绝（argv 拼接前的最后防线）。"""
    if not isinstance(udid, str) or not udid.strip():
        raise SchemaValidationError("设备 udid 为空：拒绝隐式设备目标")
    udid = udid.strip()
    if not _UDID_RE.match(udid):
        raise SchemaValidationError(f"非法设备 udid（只允许字母数字与连字符）: {udid!r}")
    return udid


def _require_dict(parent: Any, key: str, *, ctx: str) -> dict[str, Any]:
    value = parent.get(key)
    if not isinstance(value, dict):
        raise SchemaValidationError(f"devicectl JSON 形态异常：{ctx} 缺少对象字段 {key!r}")
    return value


def parse_devicectl_devices(payload: dict[str, Any]) -> list[DeviceInventoryEntry]:
    """解析 ``devicectl --json-output`` 全量清单；形态不符即响亮失败，不静默吞设备。

    单条设备缺关键字段时抛 SchemaValidationError（带序号）——盘点是租约决策的输入，
    静默跳过一台真机可能让 acquire 选到错误设备，宁可整份清单拒收。
    """
    if not isinstance(payload, dict):
        raise SchemaValidationError("devicectl JSON 顶层不是对象")
    result = _require_dict(payload, "result", ctx="顶层")
    devices = result.get("devices")
    if not isinstance(devices, list):
        raise SchemaValidationError("devicectl JSON 形态异常：result.devices 不是数组")

    entries: list[DeviceInventoryEntry] = []
    for index, raw in enumerate(devices):
        if not isinstance(raw, dict):
            raise SchemaValidationError(f"devicectl JSON 形态异常：devices[{index}] 不是对象")
        ctx = f"devices[{index}]"
        props = _require_dict(raw, "properties", ctx=ctx)
        hardware = _require_dict(props, "hardware", ctx=ctx)
        connection = _require_dict(props, "connection", ctx=ctx)
        state = _require_dict(props, "state", ctx=ctx)

        udid = hardware.get("udid")
        if not isinstance(udid, str) or not udid.strip():
            raise SchemaValidationError(f"devicectl JSON 形态异常：{ctx} 缺 hardware.udid")
        identifier = raw.get("identifier")
        if not isinstance(identifier, str) or not identifier.strip():
            raise SchemaValidationError(f"devicectl JSON 形态异常：{ctx} 缺顶层 identifier")

        entries.append(
            DeviceInventoryEntry(
                udid=udid.strip(),
                coredevice_id=identifier.strip(),
                name=str(state.get("name") or hardware.get("marketingName") or ""),
                platform=str(hardware.get("platform", "")),
                reality=str(hardware.get("reality", "")),
                connection_state=str(connection.get("state", "")),
                boot_state=str(state.get("bootState", "")),
                visibility_class=str(raw.get("visibilityClass", "")),
            )
        )
    return entries


def find_device(entries: list[DeviceInventoryEntry], udid: str) -> DeviceInventoryEntry | None:
    """按硬件 UDID 定位清单条目；未登记返回 None（适配器据此区分未知设备/不可用）。"""
    udid = validate_udid(udid)
    for entry in entries:
        if entry.udid == udid:
            return entry
    return None


# ── H3 混合执行：process/capture 控制面 argv 纯函数（docs/01 §3/§4）──────────
#
# 本机取证（Xcode 27.0，2026-11-13/14，真机 iPhone 16 Pro）：
# - 截图：``devicectl device capture screenshot --device <udid> --destination <path>``，
#   --destination 是必填文件路径，产物为 PNG，竖屏判定交给 core.png_probe；
# - 启动：``devicectl device process launch [--terminate-existing] --device <udid>
#   <bundle-id> [args...]``，位置参数透传给 App（对应段内 purge 启动参数）；
# - 终止：``devicectl device process terminate --device <udid> --pid <pid> [--kill]``，
#   注意按 **PID** 终止而非 bundle id；devicectl 词汇表无进程清单动词，
#   app_not_running 探针主机侧只能判 unknown（docs/01 §4），pid 由计划参数显式带入。

_BUNDLE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\-]{1,}$")


def validate_bundle_id(bundle_id: str) -> str:
    """bundle id 白名单：字母数字/点/连字符，拒绝空白与 shell 元字符。"""
    if not isinstance(bundle_id, str) or not bundle_id.strip():
        raise SchemaValidationError("bundle_id 为空：拒绝隐式 App 目标")
    bundle_id = bundle_id.strip()
    if not _BUNDLE_ID_RE.match(bundle_id):
        raise SchemaValidationError(
            f"非法 bundle_id（只允许字母数字、点、连字符）: {bundle_id!r}")
    return bundle_id


def validate_pid(pid: int) -> int:
    """PID 必须是正整数；拒绝透传负数/字符串，argv 注入的最后防线。"""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise SchemaValidationError(f"非法 pid（必须为正整数）: {pid!r}")
    return pid


def build_screenshot_argv(udid: str, destination: str) -> list[str]:
    """截图 argv：设备与落盘路径全部显式，无隐式目标、无 shell。"""
    udid = validate_udid(udid)
    if not isinstance(destination, str) or not destination.strip():
        raise SchemaValidationError("截图 destination 为空：拒绝隐式落盘路径")
    return [
        "xcrun", "devicectl", "device", "capture", "screenshot",
        "--device", udid, "--destination", destination,
    ]


def build_process_launch_argv(
    udid: str,
    bundle_id: str,
    *,
    arguments: list[str] | None = None,
    terminate_existing: bool = False,
) -> list[str]:
    """App 启动 argv；arguments 作为位置参数透传，逐元素做 argv 注入校验。"""
    udid = validate_udid(udid)
    bundle_id = validate_bundle_id(bundle_id)
    argv = ["xcrun", "devicectl", "device", "process", "launch"]
    if terminate_existing:
        argv.append("--terminate-existing")
    argv.extend(["--device", udid, bundle_id])
    for arg in arguments or []:
        if not isinstance(arg, str) or not arg or any(ch in arg for ch in "\0\n"):
            raise SchemaValidationError(f"非法启动参数（拒绝空串/换行/NUL）: {arg!r}")
        argv.append(arg)
    return argv


def build_process_terminate_argv(
    udid: str, pid: int, *, kill: bool = False
) -> list[str]:
    """进程终止 argv：PID 显式必填（devicectl 不支持按 bundle 终止）。"""
    udid = validate_udid(udid)
    pid = validate_pid(pid)
    argv = [
        "xcrun", "devicectl", "device", "process", "terminate",
        "--device", udid, "--pid", str(pid),
    ]
    if kill:
        argv.append("--kill")
    return argv
