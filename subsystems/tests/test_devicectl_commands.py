"""devicectl_commands 纯函数测试（hostless，零真机；合成 JSON 形态照 2026-11-13 取证裁剪）。"""

from __future__ import annotations

import pytest

from testpilot_core.devicectl_commands import (
    DeviceInventoryEntry,
    build_devicectl_list_argv,
    build_process_launch_argv,
    build_process_terminate_argv,
    build_screenshot_argv,
    find_device,
    parse_devicectl_devices,
    validate_bundle_id,
    validate_pid,
    validate_udid,
)
from testpilot_core.errors import SchemaValidationError

_PHYSICAL_UDID = "00008140-001A39590162801C"
_SIM_UDID = "5411787C-8AFF-4EBF-9000-EB5EFD1457D5"


def _physical_device(*, state: str = "connected", boot: str = "booted") -> dict:
    """形态对齐真机 JSON：新字段全在 properties 下，顶层 identifier 是 CoreDevice UUID。"""
    return {
        "identifier": "9D3A93E6-73B7-5504-AA20-A4E6C5714D43",
        "visibilityClass": "default",
        "properties": {
            "connection": {
                "pairingState": "paired",
                "state": state,
                "transportType": "wired",
            },
            "hardware": {
                "deviceType": "iPhone",
                "marketingName": "iPhone 16 Pro",
                "platform": "iOS",
                "productType": "iPhone17,1",
                "reality": "physical",
                "udid": _PHYSICAL_UDID,
            },
            "software": {"osVersionNumber": {"stringValue": "27.2"}},
            "state": {"bootState": boot, "name": "iPhone"},
        },
    }


def _simulator_device() -> dict:
    return {
        "identifier": _SIM_UDID,
        "visibilityClass": "simulators",
        "properties": {
            "connection": {"pairingState": "paired", "state": "disconnected",
                           "transportType": "sameMachine"},
            "hardware": {"deviceType": "iPhone", "marketingName": "iPhone 17",
                         "platform": "iOS", "productType": "iPhone18,3",
                         "reality": "simulated", "udid": _SIM_UDID},
            "software": {"osVersionNumber": {"stringValue": "27.0"}},
            "state": {"bootState": "shutdown", "name": "iPhone 17",
                      "visibilityClass": "simulators"},
        },
    }


def _payload(*devices: dict) -> dict:
    return {"info": {}, "result": {"devices": list(devices)}}


def test_list_argv_uses_stdout_json_and_no_implicit_target():
    assert build_devicectl_list_argv() == [
        "xcrun", "devicectl", "list", "devices", "--json-output", "-"
    ]


def test_parse_extracts_physical_and_simulator_entries():
    entries = parse_devicectl_devices(_payload(_physical_device(), _simulator_device()))
    assert [e.udid for e in entries] == [_PHYSICAL_UDID, _SIM_UDID]
    physical, simulator = entries
    assert isinstance(physical, DeviceInventoryEntry)
    assert physical.coredevice_id == "9D3A93E6-73B7-5504-AA20-A4E6C5714D43"
    assert physical.is_physical and physical.is_connected
    assert physical.platform == "iOS" and physical.boot_state == "booted"
    assert physical.name == "iPhone" and physical.visibility_class == "default"
    assert simulator.is_physical is False and simulator.is_connected is False


def test_connected_state_allowlist_rejects_unknown_future_state():
    # 只认实测的 connected；unavailable/disconnected/未来枚举一律保守不可用
    for state in ("disconnected", "unavailable", "paired", "mystery-state"):
        entry = parse_devicectl_devices(
            _payload(_physical_device(state=state))
        )[0]
        assert entry.is_connected is False, state
    assert parse_devicectl_devices(
        _payload(_physical_device(state="connected"))
    )[0].is_connected is True


def test_find_device_locates_by_hardware_udid_not_coredevice_id():
    entries = parse_devicectl_devices(_payload(_physical_device(), _simulator_device()))
    found = find_device(entries, _PHYSICAL_UDID)
    assert found is not None and found.is_physical
    # 传 CoreDevice UUID 找不到——决策只认硬件 UDID，防两类标识混用选错设备
    assert find_device(entries, "9D3A93E6-73B7-5504-AA20-A4E6C5714D43") is None


def test_find_device_unknown_returns_none():
    entries = parse_devicectl_devices(_payload(_physical_device()))
    assert find_device(entries, "00000000-0000000000000000") is None


def test_validate_udid_rejects_blank_and_injection_chars():
    with pytest.raises(SchemaValidationError, match="udid 为空"):
        validate_udid("  ")
    for bad in ("UDID; rm -rf /", "UDID NAME", 'UDID"', ""):
        with pytest.raises(SchemaValidationError):
            validate_udid(bad)
    assert validate_udid(f"  {_PHYSICAL_UDID}  ") == _PHYSICAL_UDID


def test_parse_rejects_malformed_payload_loudly():
    with pytest.raises(SchemaValidationError, match="顶层不是对象"):
        parse_devicectl_devices("nope")  # type: ignore[arg-type]
    with pytest.raises(SchemaValidationError, match="result.devices"):
        parse_devicectl_devices({"result": {}})
    with pytest.raises(SchemaValidationError, match=r"devices\[0\]"):
        parse_devicectl_devices({"result": {"devices": [{"identifier": "x"}]}})
    with pytest.raises(SchemaValidationError, match="hardware.udid"):
        broken = _physical_device()
        del broken["properties"]["hardware"]["udid"]
        parse_devicectl_devices(_payload(broken))
    with pytest.raises(SchemaValidationError, match="identifier"):
        broken = _physical_device()
        del broken["identifier"]
        parse_devicectl_devices(_payload(broken))


def test_empty_device_list_is_valid_parse():
    # 空清单不报错：装配期空 inventory 由适配器构造守卫负责，解析层只忠实反映
    assert parse_devicectl_devices(_payload()) == []


# ── H3：screenshot / process launch / terminate argv 契约（零真机纯函数）──────


def test_screenshot_argv_is_fully_explicit():
    argv = build_screenshot_argv(_PHYSICAL_UDID, "/tmp/shot.png")
    assert argv == [
        "xcrun", "devicectl", "device", "capture", "screenshot",
        "--device", _PHYSICAL_UDID, "--destination", "/tmp/shot.png",
    ]
    with pytest.raises(SchemaValidationError, match="destination 为空"):
        build_screenshot_argv(_PHYSICAL_UDID, "  ")
    with pytest.raises(SchemaValidationError):
        build_screenshot_argv("bad udid!", "/tmp/x.png")


def test_launch_argv_flags_bundle_and_positional_args():
    argv = build_process_launch_argv(
        _PHYSICAL_UDID, "com.example.AlarmKitSpike",
        arguments=["--ak-purge-dates", "2026-11-14"])
    assert argv == [
        "xcrun", "devicectl", "device", "process", "launch",
        "--device", _PHYSICAL_UDID, "com.example.AlarmKitSpike",
        "--ak-purge-dates", "2026-11-14",
    ]
    terminated = build_process_launch_argv(
        _PHYSICAL_UDID, "com.example.App", terminate_existing=True)
    assert terminated[5] == "--terminate-existing"
    # 默认不带 --terminate-existing（冷启动语义必须显式声明，防误杀现场）
    assert "--terminate-existing" not in argv


def test_launch_argv_rejects_injection():
    with pytest.raises(SchemaValidationError, match="bundle_id"):
        build_process_launch_argv(_PHYSICAL_UDID, "com.bad; rm -rf /")
    with pytest.raises(SchemaValidationError, match="非法启动参数"):
        build_process_launch_argv(
            _PHYSICAL_UDID, "com.example.App", arguments=["--x\n"])
    with pytest.raises(SchemaValidationError):
        validate_bundle_id("")


def test_terminate_argv_requires_pid_and_supports_kill():
    argv = build_process_terminate_argv(_PHYSICAL_UDID, 4321)
    assert argv == [
        "xcrun", "devicectl", "device", "process", "terminate",
        "--device", _PHYSICAL_UDID, "--pid", "4321",
    ]
    killed = build_process_terminate_argv(_PHYSICAL_UDID, 4321, kill=True)
    assert killed[-1] == "--kill"
    for bad in (0, -1, "4321", True):
        with pytest.raises(SchemaValidationError, match="pid"):
            validate_pid(bad)  # type: ignore[arg-type]
