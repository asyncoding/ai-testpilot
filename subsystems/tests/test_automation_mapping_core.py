"""automation-map-v1 映射纯函数 hostless 测试（docs/12 §3.3 / E3）。

零 IO：手工构造 Swift 文件字典与用例 ID 集，钉死——
标记绑定（一方法多 TC / 一 TC 多方法）、未链接测试、未映射用例、
未知 TC/畸形标记/悬空 manual 绑定 fail-closed、探索性剔除、目录→target
映射、map hash 幂等与 head_commit 敏感。
"""

from __future__ import annotations

import pytest

from testpilot_core.automation_mapping import (
    AUTOMATION_MAP_SCHEMA_VERSION,
    AutomationMapError,
    ManualBindingInput,
    ScanResult,
    automation_map_hash,
    build_automation_map,
    scan_swift_tests,
    target_for_path,
)

_ACTIVE = ["TC-A1", "TC-A2", "TC-B1", "TC-EXP"]
_EXPLORATORY = ["TC-EXP"]

_FILES = {
    "AlarmKitSpike/Tests/AgentGenerated/RoutineAlarmAgentTests.swift": """
import XCTest

final class AgentRoutineAlarmTests: XCTestCase {

    // testpilot-case: TC-A1
    // testpilot-case: TC-A2
    func test_TCA1_ringSnapshot() {
        XCTAssertTrue(true)
    }

    // testpilot-case: TC-A1
    func test_TCA1_secondCoverage() {
        XCTAssertEqual(1, 1)
    }
}
""",
    "AlarmKitSpike/Tests/HolidayCalendarTests.swift": """
import XCTest

final class HolidayCalendarTests: XCTestCase {
    func testPlainDays() { }
    func test调休() { }
}
""",
    "AlarmKitSpike/UITests/ProductFlowUITests.swift": """
import XCTest

final class ProductFlowUITests: XCTestCase {
    // testpilot-case: TC-B1
    func test_TCB1_productFlow() { }
}
""",
    "AlarmKitSpike/Sources/Services/MissionCoordinator.swift": """
struct MissionCoordinator {}
""",
}


def _scan(**over):
    kwargs = dict(files=_FILES, active_case_ids=_ACTIVE,
                  exploratory_ids=_EXPLORATORY)
    kwargs.update(over)
    return scan_swift_tests(**kwargs)


def test_marker_bindings_including_one_method_multi_tc():
    result = _scan()
    pairs = {(b.case_external_id, b.only_testing_id, b.origin) for b in result.bindings}
    assert ("TC-A1",
            "AlarmKitSpikeTests/AgentRoutineAlarmTests/test_TCA1_ringSnapshot",
            "marker") in pairs
    assert ("TC-A2",
            "AlarmKitSpikeTests/AgentRoutineAlarmTests/test_TCA1_ringSnapshot",
            "marker") in pairs
    # UI 目录走 UITests target
    assert ("TC-B1",
            "AlarmKitSpikeUITests/ProductFlowUITests/test_TCB1_productFlow",
            "marker") in pairs
    assert all(b.line > 0 for b in result.bindings)


def test_one_tc_many_methods_emits_duplicate_warning_but_keeps_both_bindings():
    result = _scan()
    a1_bindings = [b for b in result.bindings if b.case_external_id == "TC-A1"]
    assert len(a1_bindings) == 2
    assert any("重复标记: TC-A1" in w for w in result.scan_warnings)


def test_unmarked_existing_tests_go_to_unlinked():
    result = _scan()
    unlinked = {u.only_testing_id for u in result.unlinked_swift_tests}
    assert "AlarmKitSpikeTests/HolidayCalendarTests/testPlainDays" in unlinked
    # 非 ASCII 方法名不被识别为 test\w+，不计入测试台账
    assert all("test调休" not in u for u in unlinked)
    # 生产代码目录不参与扫描
    assert all("MissionCoordinator" not in u.only_testing_id
               for u in result.unlinked_swift_tests)


def test_unmapped_active_cases_listed():
    result = _scan()
    # TC-EXP 是探索性（不进未映射），其余 TC 都有绑定 → 未映射为空
    assert result.unmapped_active_cases == []
    result2 = scan_swift_tests(
        _FILES, _ACTIVE + ["TC-N2"], exploratory_ids=_EXPLORATORY)
    assert result2.unmapped_active_cases == ["TC-N2"]


def test_unknown_tc_marker_is_hard_error():
    files = dict(_FILES)
    files["AlarmKitSpike/Tests/AgentGenerated/ExtraTests.swift"] = """
final class ExtraTests: XCTestCase {
    // testpilot-case: TC-GHOST
    func test_ghost() { }
}
"""
    with pytest.raises(AutomationMapError, match="TC-GHOST"):
        scan_swift_tests(files, _ACTIVE, exploratory_ids=_EXPLORATORY)


def test_malformed_marker_line_is_hard_error():
    files = dict(_FILES)
    files["AlarmKitSpike/Tests/AgentGenerated/BadTests.swift"] = """
final class BadTests: XCTestCase {
    // testpilot-case:TC-A1   （缺空格的畸形写法也应被严格正则放行到提示分支？否——拒绝）
    func test_bad() { }
}
"""
    # 严格正则不匹配紧凑写法，但 hint 命中 → 畸形错误，fail-closed
    with pytest.raises(AutomationMapError, match="畸形"):
        scan_swift_tests(files, _ACTIVE, exploratory_ids=_EXPLORATORY)


def test_exploratory_marker_binding_excluded_with_warning():
    files = dict(_FILES)
    files["AlarmKitSpike/Tests/AgentGenerated/ExploratoryTests.swift"] = """
final class ExploratoryTests: XCTestCase {
    // testpilot-case: TC-EXP
    func test_TCEXP_probe() { }
}
"""
    result = scan_swift_tests(files, _ACTIVE, exploratory_ids=_EXPLORATORY)
    assert all(b.case_external_id != "TC-EXP" for b in result.bindings)
    assert any("TC-EXP" in w and "探索性" in w for w in result.scan_warnings)
    # 被剔除的方法仍作为未链接测试可见，不凭空消失
    assert any("test_TCEXP_probe" in u.only_testing_id
               for u in result.unlinked_swift_tests)


def test_manual_binding_happy_path_and_dedup():
    manual = ManualBindingInput(
        case_external_id="TC-N2",
        only_testing_id="AlarmKitSpikeTests/HolidayCalendarTests/testPlainDays",
        reviewed_by="alice", reviewed_at="2026-11-13T09:00:00Z")
    result = scan_swift_tests(
        _FILES, _ACTIVE + ["TC-N2"], exploratory_ids=_EXPLORATORY,
        manual_bindings=[manual, manual])  # 重复输入去重
    binding = next(b for b in result.bindings if b.case_external_id == "TC-N2")
    assert binding.origin == "manual"
    assert binding.reviewed_by == "alice"
    # 手工绑定后该方法从未链接清单消失
    assert all("testPlainDays" not in u.only_testing_id
               for u in result.unlinked_swift_tests)
    assert len([b for b in result.bindings if b.case_external_id == "TC-N2"]) == 1


def test_manual_binding_dangling_method_is_hard_error():
    manual = ManualBindingInput(
        case_external_id="TC-N2",
        only_testing_id="AlarmKitSpikeTests/GhostTests/testDeleted",
        reviewed_by="alice", reviewed_at="2026-11-13T09:00:00Z")
    with pytest.raises(AutomationMapError, match="已不存在"):
        scan_swift_tests(_FILES, _ACTIVE + ["TC-N2"],
                         exploratory_ids=_EXPLORATORY, manual_bindings=[manual])


def test_manual_binding_illegal_identifier_and_unknown_tc():
    bad_shape = ManualBindingInput(
        case_external_id="TC-N2", only_testing_id="not-a-valid-oid",
        reviewed_by="alice", reviewed_at="2026-11-13T09:00:00Z")
    with pytest.raises(AutomationMapError, match="标识符非法"):
        scan_swift_tests(_FILES, _ACTIVE + ["TC-N2"],
                         exploratory_ids=_EXPLORATORY, manual_bindings=[bad_shape])
    unknown_tc = ManualBindingInput(
        case_external_id="TC-GHOST",
        only_testing_id="AlarmKitSpikeTests/HolidayCalendarTests/testPlainDays",
        reviewed_by="alice", reviewed_at="2026-11-13T09:00:00Z")
    with pytest.raises(AutomationMapError, match="非活跃"):
        scan_swift_tests(_FILES, _ACTIVE,
                         exploratory_ids=_EXPLORATORY, manual_bindings=[unknown_tc])


def test_pending_markers_cleared_at_class_boundary():
    files = {"AlarmKitSpike/Tests/OrphanTests.swift": """
// testpilot-case: TC-A1
final class OrphanTests: XCTestCase {
    func test_noMarkerAbove() { }
}
"""}
    result = scan_swift_tests(files, _ACTIVE, exploratory_ids=_EXPLORATORY)
    assert all(b.case_external_id != "TC-A1" for b in result.bindings)
    assert any("test_noMarkerAbove" in u.only_testing_id
               for u in result.unlinked_swift_tests)


def test_target_for_path_directory_mapping():
    assert target_for_path("AlarmKitSpike/Tests/X.swift") == "AlarmKitSpikeTests"
    assert target_for_path("AlarmKitSpike/UITests/X.swift") == "AlarmKitSpikeUITests"
    assert target_for_path("AlarmKitSpike/Sources/X.swift") is None


def test_target_for_path_rejects_stale_root_package_outside_workspace():
    """回归（E5 实测抓出）：根 Tests/ 是 2026-08 作废的 CalendarStrongReminder
    SPM 包，不在 AlarmKitSpike.xcworkspace 内，其测试 OID 传给 xcodebuild
    必然 suite-not-found；含 Tests 段但缺 AlarmKitSpike 工程根一律不映射。"""
    assert target_for_path(
        "Tests/CalendarStrongReminderTests/AlarmSchedulerTests.swift") is None
    assert target_for_path(
        "Tests/CalendarStrongReminderTests/StressTests.swift") is None
    # 工程根内的 AgentGenerated 落点仍正常识别（附录 A 的生成目录）
    assert target_for_path(
        "AlarmKitSpike/Tests/AgentGenerated/RoutineAlarmAgentTests.swift"
    ) == "AlarmKitSpikeTests"
    # Windows 风格分隔符同样按工程根 containment 判定
    assert target_for_path(
        "Tests\\CalendarStrongReminderTests\\AnalyticsTests.swift") is None


def test_map_payload_hash_idempotent_and_head_sensitive():
    result = _scan()
    map1 = build_automation_map(
        result, repo_path="/repo", branch="demo/agent-generated-tests",
        head_commit="a" * 40, generated_at="2026-11-13T08:00:00Z")
    map2 = build_automation_map(
        result, repo_path="/repo", branch="demo/agent-generated-tests",
        head_commit="a" * 40, generated_at="2026-11-13T09:00:00Z")
    assert map1["map_schema_version"] == AUTOMATION_MAP_SCHEMA_VERSION
    assert map1["map_hash"] == map2["map_hash"]  # 时间戳不影响 hash
    assert automation_map_hash(map1) == map1["map_hash"]
    map3 = build_automation_map(
        result, repo_path="/repo", branch="demo/agent-generated-tests",
        head_commit="b" * 40, generated_at="2026-11-13T08:00:00Z")
    assert map3["map_hash"] != map1["map_hash"]  # HEAD 移动必须改变 hash
    assert isinstance(result, ScanResult)
