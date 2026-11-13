"""generated_swift_lint hostless 红证测试（H5）。

红证纪律：每条红线都要有「植入必红、回退转绿」双向证据；注释/字符串里的
关键词不得误伤；作用域守卫保证语料仓存量代码不被这套生成契约管辖。
"""

from __future__ import annotations

import pytest

from testpilot_core.generated_swift_lint import (
    AGENT_GENERATED_MARKER,
    GeneratedSwiftLintError,
    mask_comments_and_strings,
    scan_agent_generated_files,
    scan_generated_swift,
)

_RELAY_PATH = "AlarmKitSpike/UITests/AgentGenerated/RelayTests.swift"
_UNIT_PATH = "AlarmKitSpike/Tests/AgentGenerated/SmokeTests.swift"

_CLEAN_RELAY = """\
import XCTest

final class RelayTests: XCTestCase {
    let app = XCUIApplication()

    // 接力段：附着已有进程，禁止冷启动
    func testRelayFromNotificationScene() throws {
        app.activate()
        let relayPage = app.buttons["继续"]
        XCTAssertTrue(relayPage.waitForExistence(timeout: 10))
        relayPage.tap()
    }
}
"""

_CLEAN_COLD_START = """\
import XCTest

final class SmokeTests: XCTestCase {
    let app = XCUIApplication()

    override func setUp() {
        app.launchArguments = ["--ak-purge-dates"]
        app.launch()
    }

    func testColdStartHome() throws {
        XCTAssertTrue(app.staticTexts["首页"].waitForExistence(timeout: 8))
    }
}
"""


def _rule_ids(violations):
    return sorted(v.rule_id for v in violations)


def test_clean_relay_uses_activate_and_polling_only_green():
    violations = scan_generated_swift(_RELAY_PATH, _CLEAN_RELAY)
    assert violations == []


def test_clean_cold_start_single_launch_in_setup_green():
    violations = scan_generated_swift(_UNIT_PATH, _CLEAN_COLD_START)
    assert violations == []


def test_red_proof_planted_sleep_goes_red_then_revert_green():
    planted = _CLEAN_RELAY.replace(
        'XCTAssertTrue(relayPage.waitForExistence(timeout: 10))',
        'sleep(2)\n        XCTAssertTrue(relayPage.waitForExistence(timeout: 10))',
    )
    red = scan_generated_swift(_RELAY_PATH, planted)
    assert _rule_ids(red) == ["GS-001"]
    # 行号精确指向植入行
    planted_line = planted[:planted.index("sleep(2)")].count("\n") + 1
    assert red[0].line == planted_line
    assert "sleep(2)" in red[0].snippet
    # 回退即转绿
    assert scan_generated_swift(_RELAY_PATH, _CLEAN_RELAY) == []


def test_usleep_and_thread_sleep_both_red():
    text = """\
final class T: XCTestCase {
    func testA() throws {
        usleep(200_000)
        Thread.sleep(forTimeInterval: 1.5)
    }
}
"""
    violations = scan_generated_swift(_RELAY_PATH, text)
    assert _rule_ids(violations) == ["GS-001", "GS-001"]
    assert [v.line for v in violations] == [3, 4]


def test_sleep_in_comments_and_strings_not_flagged():
    text = """\
final class T: XCTestCase {
    // 生成契约：禁止写 sleep(2) 这种死等
    /* 块注释里 Thread.sleep 也不算 */
    func testA() throws {
        let hint = "press sleep( button to continue"
        let docs = '''
            multiline: usleep(1) 与 Thread.sleep 示例
        '''
        XCTAssertTrue(app.buttons["ok"].waitForExistence(timeout: 5))
    }
}
""".replace("'''", '"""')
    assert scan_generated_swift(_RELAY_PATH, text) == []


def test_escaped_quote_does_not_break_string_masking():
    # 转义引号不能提前闭合字符串，闭合后的真实 sleep 必须仍被抓住
    text = """\
final class T: XCTestCase {
    func testA() throws {
        let s = "he said \\"sleep( now\\" loudly"
        sleep(1)
    }
}
"""
    violations = scan_generated_swift(_RELAY_PATH, text)
    assert _rule_ids(violations) == ["GS-001"]
    assert violations[0].line == 4


def test_mask_preserves_newlines_so_line_numbers_stable():
    text = "// sleep(9)\n// Thread.sleep\nlet x = 1\n"
    masked = mask_comments_and_strings(text)
    assert masked.count("\n") == text.count("\n")
    assert "sleep" not in masked


def test_relay_method_mixing_activate_and_launch_is_gs002():
    text = """\
final class T: XCTestCase {
    func testRelay() throws {
        app.activate()
        app.launch()
    }
}
"""
    violations = scan_generated_swift(_RELAY_PATH, text)
    assert _rule_ids(violations) == ["GS-002"]
    assert "testRelay" in violations[0].message
    assert violations[0].line == 4  # 指向 launch 行


def test_double_launch_in_one_test_method_is_gs003():
    text = """\
final class T: XCTestCase {
    func testRetry() throws {
        app.launch()
        app.buttons["retry"].tap()
        app.launch()
    }
}
"""
    violations = scan_generated_swift(_RELAY_PATH, text)
    assert _rule_ids(violations) == ["GS-003"]
    assert violations[0].line == 5


def test_single_launch_per_test_method_allowed():
    text = """\
final class T: XCTestCase {
    func testCold() throws {
        app.launch()
        XCTAssertTrue(app.buttons["home"].waitForExistence(timeout: 6))
    }
}
"""
    assert scan_generated_swift(_UNIT_PATH, text) == []


def test_launch_in_non_test_helper_not_counted():
    text = """\
final class T: XCTestCase {
    private func restartApp() {
        app.terminate()
        app.launch()
    }

    func testCold() throws {
        app.launch()
        restartApp()
    }
}
"""
    # testCold 方法体内只有一个直接 .launch(（helper 调用不是 launch 语法）
    assert scan_generated_swift(_UNIT_PATH, text) == []


def test_red_proof_remove_second_launch_turns_gs003_green():
    bad = """\
final class T: XCTestCase {
    func testRetry() throws {
        app.launch()
        app.launch()
    }
}
"""
    assert _rule_ids(scan_generated_swift(_RELAY_PATH, bad)) == ["GS-003"]
    good = bad.replace("        app.launch()\n", "", 1)
    assert scan_generated_swift(_RELAY_PATH, good) == []


def test_out_of_scope_path_rejected_fail_closed():
    with pytest.raises(GeneratedSwiftLintError, match="圈外路径拒绝扫描"):
        scan_generated_swift("AlarmKitSpike/UITests/ManualTests/Old.swift",
                             "sleep(1)\n")
    with pytest.raises(GeneratedSwiftLintError, match="只扫描 .swift"):
        scan_generated_swift(f"UITests/{AGENT_GENERATED_MARKER}/notes.txt",
                             "x")


def test_batch_scan_aggregates_and_sorts_by_line():
    files = {
        _UNIT_PATH: "final class A {\n func testX() { sleep(1) }\n}\n",
        _RELAY_PATH: _CLEAN_RELAY,
    }
    violations = scan_agent_generated_files(files)
    assert len(violations) == 1
    assert violations[0].rule_id == "GS-001"
    assert violations[0].path == _UNIT_PATH


def test_batch_rejects_whole_batch_if_any_path_out_of_scope():
    with pytest.raises(GeneratedSwiftLintError):
        scan_agent_generated_files({
            _RELAY_PATH: _CLEAN_RELAY,
            "Other/Plain.swift": "let x = 1\n",
        })


def test_block_comment_containing_braces_does_not_break_method_spans():
    text = """\
final class T: XCTestCase {
    // 注释里的假大括号 } 不得截断方法体
    func testA() throws { /* { } */
        app.activate()
        app.launch()
    }
}
"""
    violations = scan_generated_swift(_RELAY_PATH, text)
    assert _rule_ids(violations) == ["GS-002"]


def test_unrelated_identifier_containing_sleep_substring_not_flagged():
    text = """\
final class T: XCTestCase {
    func testA() throws {
        let sleeper = Sleeper()
        sleeper.pollUntilReady()
    }
}
"""
    assert scan_generated_swift(_RELAY_PATH, text) == []
