"""S3.2 core xcode_commands 纯逻辑测试（无 macOS/无模拟器，hostless）。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from testpilot_core.errors import UnsupportedCapabilityError
from testpilot_core.xcode_commands import (
    CAPABILITY_DEVICE_ONLY,
    CAPABILITY_NOT_OFFERED,
    CAPABILITY_OK,
    DESTINATION_PLATFORM_DEVICE,
    DESTINATION_PLATFORM_SIMULATOR,
    EnvLease,
    build_simctl_erase_argv,
    build_simctl_shutdown_argv,
    build_xcodebuild_argv,
    build_xcodebuild_xctestrun_argv,
    env_lease_id,
    evaluate_capabilities,
    is_env_lease_expired,
    reclaimable_env_leases,
    simctl_reset_plan,
    validate_only_testing,
)

_UDID = "5411787C-8AFF-4EBF-9000-EB5EFD1457D5"
_KWARGS = dict(
    workspace="AlarmKitSpike/AlarmKitSpike.xcworkspace",
    scheme="AlarmKitSpike",
    udid=_UDID,
    result_bundle_path="/tmp/hostless.xcresult",
)
_T0 = datetime(2026, 11, 10, 9, 0, tzinfo=timezone.utc)


# ── xcodebuild argv ──────────────────────────────────────────────────────────


def test_hostless_suite_argv_matches_docs03_exact_shape():
    argv = build_xcodebuild_argv(only_testing=["AlarmKitSpikeTests"], **_KWARGS)
    assert argv == [
        "xcodebuild", "test",
        "-workspace", "AlarmKitSpike/AlarmKitSpike.xcworkspace",
        "-scheme", "AlarmKitSpike",
        "-destination", f"platform=iOS Simulator,id={_UDID}",
        "-only-testing", "AlarmKitSpikeTests",
        "-resultBundlePath", "/tmp/hostless.xcresult",
    ]


def test_single_xcuitest_argv_keeps_fully_qualified_identifier():
    argv = build_xcodebuild_argv(
        only_testing=[
            "AlarmKitSpikeUITests/ProductFlowUITests/test10_WakeForm_SoundPickedThenSwipeStillBlocked"
        ],
        **_KWARGS,
    )
    assert argv[8] == "-only-testing"
    assert argv[9].endswith("/ProductFlowUITests/test10_WakeForm_SoundPickedThenSwipeStillBlocked")


def test_multiple_only_testing_flags_in_order():
    argv = build_xcodebuild_argv(
        only_testing=["AlarmKitSpikeTests", "AlarmKitSpikeUITests/ProductFlowUITests"],
        **_KWARGS,
    )
    assert argv.count("-only-testing") == 2
    first = argv.index("-only-testing")
    assert argv[first + 1] == "AlarmKitSpikeTests"
    assert argv[first + 3] == "AlarmKitSpikeUITests/ProductFlowUITests"


@pytest.mark.parametrize("field", ["workspace", "scheme", "udid", "result_bundle_path"])
def test_missing_required_arg_fails_closed(field):
    kwargs = dict(_KWARGS)
    kwargs[field] = "   "
    with pytest.raises(ValueError, match="不能为空"):
        build_xcodebuild_argv(only_testing=["AlarmKitSpikeTests"], **kwargs)


def test_empty_only_testing_rejected_no_silent_full_run():
    with pytest.raises(ValueError, match="选择执行"):
        build_xcodebuild_argv(only_testing=[], **_KWARGS)


@pytest.mark.parametrize(
    "bad",
    ["AlarmKitSpikeTests;rm -rf /", "../AlarmKitSpikeTests", "Suite/Class/method/extra",
     "Suite With Space", "", "Suite/-Bad", "1Suite"],
)
def test_invalid_only_testing_identifier_rejected(bad):
    with pytest.raises(ValueError, match="非法 -only-testing"):
        validate_only_testing(bad)


@pytest.mark.parametrize("bad_udid", ["", "  ", "ABC; rm", "UDID WITH SPACE", "abc'--"])
def test_udid_injection_chars_rejected(bad_udid):
    kwargs = dict(_KWARGS, udid=bad_udid)
    with pytest.raises(ValueError):
        build_xcodebuild_argv(only_testing=["AlarmKitSpikeTests"], **kwargs)


# ── test-without-building / -xctestrun argv（USB relay 转正）──────────────────

_XCTESTRUN = "/DerivedData/Products/AlarmKitSpike_iphonesimulator27.0-arm64.injected.xctestrun"
_OID = "AlarmKitSpikeUITests/AgentQuickAlarmUITests/test_TCA1_lockRelayFullFlow"
_XC_KWARGS = dict(xctestrun_path=_XCTESTRUN, udid=_UDID)


def test_xctestrun_argv_matches_runbook02_exact_shape():
    # 与 runbook 02 手工命令逐字等价（runbook 用 -only-testing:$OID 冒号形式，
    # xcodebuild 与两参数形式等价；产品内统一两参数形式）
    argv = build_xcodebuild_xctestrun_argv(
        only_testing=[_OID], maximum_test_execution_time_allowance=600, **_XC_KWARGS
    )
    assert argv == [
        "xcodebuild", "test-without-building",
        "-xctestrun", _XCTESTRUN,
        "-destination", f"platform=iOS Simulator,id={_UDID}",
        "-only-testing", _OID,
        "-maximum-test-execution-time-allowance", "600",
    ]


def test_xctestrun_argv_optional_flags_absent_by_default():
    argv = build_xcodebuild_xctestrun_argv(only_testing=[_OID], **_XC_KWARGS)
    assert "-resultBundlePath" not in argv
    assert "-maximum-test-execution-time-allowance" not in argv


def test_xctestrun_argv_result_bundle_appended_before_allowance():
    argv = build_xcodebuild_xctestrun_argv(
        only_testing=[_OID],
        result_bundle_path="/tmp/relay.xcresult",
        maximum_test_execution_time_allowance=900,
        **_XC_KWARGS,
    )
    assert argv[-4:] == [
        "-resultBundlePath", "/tmp/relay.xcresult",
        "-maximum-test-execution-time-allowance", "900",
    ]


def test_xctestrun_argv_multiple_only_testing_in_order():
    argv = build_xcodebuild_xctestrun_argv(
        only_testing=["AlarmKitSpikeUITests/FooTests/test_a",
                      "AlarmKitSpikeUITests/BarTests/test_b"],
        **_XC_KWARGS,
    )
    assert argv.count("-only-testing") == 2
    first = argv.index("-only-testing")
    assert argv[first + 1].endswith("test_a")
    assert argv[first + 3].endswith("test_b")


def test_xctestrun_argv_device_destination_supported():
    argv = build_xcodebuild_xctestrun_argv(
        only_testing=[_OID],
        destination_platform=DESTINATION_PLATFORM_DEVICE,
        **{**_XC_KWARGS, "udid": "00008140-001A39590162801C"},
    )
    assert argv[4:6] == [
        "-destination", "platform=iOS,id=00008140-001A39590162801C"]


def test_xctestrun_argv_empty_only_testing_rejected():
    with pytest.raises(ValueError, match="选择执行"):
        build_xcodebuild_xctestrun_argv(only_testing=[], **_XC_KWARGS)


def test_xctestrun_argv_blank_xctestrun_path_rejected():
    with pytest.raises(ValueError, match="xctestrun_path"):
        build_xcodebuild_xctestrun_argv(
            only_testing=[_OID], **{**_XC_KWARGS, "xctestrun_path": "  "})


def test_xctestrun_argv_blank_result_bundle_rejected():
    with pytest.raises(ValueError, match="result_bundle_path"):
        build_xcodebuild_xctestrun_argv(
            only_testing=[_OID], result_bundle_path=" ", **_XC_KWARGS)


def test_xctestrun_argv_bad_udid_rejected():
    with pytest.raises(ValueError, match="非法设备 udid"):
        build_xcodebuild_xctestrun_argv(
            only_testing=[_OID], **{**_XC_KWARGS, "udid": "UDID WITH SPACE"})


def test_xctestrun_argv_unknown_platform_rejected():
    with pytest.raises(ValueError, match="destination_platform"):
        build_xcodebuild_xctestrun_argv(
            only_testing=[_OID], destination_platform="macOS,id=evil", **_XC_KWARGS)


def test_xctestrun_argv_bad_identifier_rejected():
    with pytest.raises(ValueError, match="非法 -only-testing"):
        build_xcodebuild_xctestrun_argv(
            only_testing=["Suite/Class/method/extra"], **_XC_KWARGS)


@pytest.mark.parametrize("bad_allowance", [0, -1, 1.5, True, False, "600"])
def test_xctestrun_argv_allowance_must_be_positive_int(bad_allowance):
    with pytest.raises(ValueError, match="maximum_test_execution_time_allowance"):
        build_xcodebuild_xctestrun_argv(
            only_testing=[_OID],
            maximum_test_execution_time_allowance=bad_allowance,
            **_XC_KWARGS,
        )


# ── 真机目的地（切片一，2026-11-13）──────────────────────────────────────────


_PHYSICAL_UDID = "00008140-001A39590162801C"


def test_device_destination_argv_uses_ios_platform_and_hardware_udid():
    argv = build_xcodebuild_argv(
        only_testing=["AlarmKitSpikeUITests/AgentStimulusUITests/test_xxx"],
        destination_platform=DESTINATION_PLATFORM_DEVICE,
        **{**_KWARGS, "udid": _PHYSICAL_UDID},
    )
    assert argv[6:8] == ["-destination", f"platform=iOS,id={_PHYSICAL_UDID}"]


def test_default_destination_remains_simulator_for_legacy_callers():
    argv = build_xcodebuild_argv(only_testing=["AlarmKitSpikeTests"], **_KWARGS)
    assert argv[7] == f"platform=iOS Simulator,id={_UDID}"


def test_unknown_destination_platform_rejected():
    with pytest.raises(ValueError, match="destination_platform"):
        build_xcodebuild_argv(
            only_testing=["AlarmKitSpikeTests"],
            destination_platform="macOS,id=evil",
            **_KWARGS,
        )


def test_device_capable_provider_accepts_device_only_caps_when_explicitly_offered():
    decision = evaluate_capabilities(
        ["ios.device", "background_audio", "codesigning"],
        offered=["ios.device", "background_audio", "codesigning", "ui.automation"],
        provider="ios-device",
        device_capable=True,
    )
    assert decision.supported and decision.reason == CAPABILITY_OK


def test_device_capable_provider_still_rejects_caps_not_on_inventory():
    # 真机环境也不能假设能力：profile 没贴标签就是不提供，走普通缺口拒绝
    decision = evaluate_capabilities(
        ["background_audio"],
        offered=["ios.device", "ui.automation"],
        provider="ios-device",
        device_capable=True,
    )
    assert not decision.supported
    assert decision.reason == CAPABILITY_NOT_OFFERED
    assert decision.unsupported == ["background_audio"]
    assert "未覆盖层" not in decision.message


def test_device_capable_false_keeps_simulator_device_only_veto():
    # 默认参数历史行为不变：模拟器环境对真机独占能力一票否决
    decision = evaluate_capabilities(
        ["background_audio"], offered=["background_audio"],
    )
    assert not decision.supported and decision.reason == CAPABILITY_DEVICE_ONLY


def test_simulator_platform_constant_unchanged():
    assert DESTINATION_PLATFORM_SIMULATOR == "iOS Simulator"
    assert DESTINATION_PLATFORM_DEVICE == "iOS"


# ── simctl 重置计划 ──────────────────────────────────────────────────────────


def test_reset_plan_is_shutdown_then_erase_in_fixed_order():
    plan = simctl_reset_plan(_UDID)
    assert plan == [
        ["xcrun", "simctl", "shutdown", _UDID],
        ["xcrun", "simctl", "erase", _UDID],
    ]
    assert build_simctl_shutdown_argv(_UDID)[2] == "shutdown"
    assert build_simctl_erase_argv(_UDID)[2] == "erase"


def test_simctl_argv_rejects_bad_udid():
    with pytest.raises(ValueError):
        simctl_reset_plan("not a udid")


# ── 能力判定（FR-EX-004 AC1）─────────────────────────────────────────────────


def test_empty_requirements_always_supported():
    decision = evaluate_capabilities([], offered=["ios.simulator"])
    assert decision.supported and decision.reason == CAPABILITY_OK
    decision.raise_if_unsupported()  # 不抛


def test_simulator_capabilities_supported_when_offered():
    decision = evaluate_capabilities(
        ["ios.simulator", "ui.automation"],
        offered=["ios.simulator", "ui.automation", "hostless"],
    )
    assert decision.supported


def test_device_only_capability_rejected_with_uncovered_layer_message():
    decision = evaluate_capabilities(
        ["background_audio"], offered=["ios.simulator", "background_audio"]
    )
    # 即使设备清单误标了该能力，真机独占集合仍一票否决（物理层给不出）
    assert not decision.supported
    assert decision.reason == CAPABILITY_DEVICE_ONLY
    assert "background_audio" in decision.unsupported
    assert "未覆盖层" in decision.message
    with pytest.raises(UnsupportedCapabilityError, match="未覆盖层"):
        decision.raise_if_unsupported()


def test_codesigning_and_ios_device_markers_are_device_only():
    for cap in ("codesigning", "ios.device"):
        decision = evaluate_capabilities([cap], offered=["ios.simulator"])
        assert not decision.supported
        assert decision.reason == CAPABILITY_DEVICE_ONLY


def test_not_offered_capability_rejected_distinct_reason():
    decision = evaluate_capabilities(
        ["ui.automation"], offered=["ios.simulator"]
    )
    assert not decision.supported
    assert decision.reason == CAPABILITY_NOT_OFFERED
    assert decision.unsupported == ["ui.automation"]
    assert "设备清单不提供" in decision.message


def test_mixed_unsupported_reports_all_and_device_only_takes_precedence():
    decision = evaluate_capabilities(
        ["codesigning", "gpu.metal"], offered=["ios.simulator"]
    )
    assert not decision.supported
    assert decision.reason == CAPABILITY_DEVICE_ONLY
    assert decision.unsupported == ["codesigning", "gpu.metal"]


# ── 环境租约回收（FR-EX-004 AC2）──────────────────────────────────────────────


def _lease(seconds: int) -> EnvLease:
    return EnvLease(
        lease_id="envlease-x",
        env_id="sim-1",
        request_id="req-1",
        acquired_at=_T0,
        expires_at=_T0 + timedelta(seconds=seconds),
    )


def test_lease_id_content_addressed_and_stable():
    lid = env_lease_id("sim-1", "req-1", _T0)
    assert lid.startswith("envlease-") and len(lid) == len("envlease-") + 12
    assert env_lease_id("sim-1", "req-1", _T0) == lid
    assert env_lease_id("sim-2", "req-1", _T0) != lid


def test_lease_expiry_boundary_is_half_open():
    active = _lease(300)
    assert is_env_lease_expired(active, now=_T0 + timedelta(seconds=299)) is False
    # 恰好到期即视为过期（[acquired, expires) 半开区间）
    assert is_env_lease_expired(active, now=_T0 + timedelta(seconds=300)) is True
    assert is_env_lease_expired(active, now=_T0 + timedelta(seconds=301)) is True


def test_reclaimable_only_returns_expired_leases():
    fresh = EnvLease(
        lease_id="envlease-fresh", env_id="sim-1", request_id="r1",
        acquired_at=_T0, expires_at=_T0 + timedelta(seconds=300))
    stale = EnvLease(
        lease_id="envlease-stale", env_id="sim-2", request_id="r2",
        acquired_at=_T0 - timedelta(seconds=600),
        expires_at=_T0 - timedelta(seconds=1))
    reclaimed = reclaimable_env_leases([fresh, stale], now=_T0)
    assert reclaimed == [stale]
