// GS 假仓基线夹具（S5）：形态对齐 TC-A1 单方法生成稿的 GS 合规结构。
// 仅作静态红线扫描夹具，不参与编译；任何变异只允许发生在 tmp_path 副本上。
import XCTest

@MainActor
final class AgentQuickAlarmUITests: XCTestCase {
    private let app = XCUIApplication()

    // 异步 setUp 钩子（同规范稿 S7 形态：Swift 6 下同步 setUpWithError 为 nonisolated）。
    override func setUp() async throws {
        continueAfterFailure = false
        // 唯一一次冷启动落在 setUp：GS-002/003 只切 test 方法体，不计 setUp。
        app.launchArguments += [
            "--ak-clear-all",
            "--ak-purge-dates",
            "--ak-accept-privacy",
            "--ak-schedule-quick", "120"
        ]
        app.launch()
        XCTAssertTrue(app.staticTexts["spike.home.title"].waitForExistence(timeout: 10))
    }

    // testpilot-case: TC-A1
    func test_TCA1_lockRelayFullFlow() async throws {
        // 测试方法体内只允许 activate 附着，零 launch、零裸 sleep。
        XCUIDevice.shared.press(.home)
        app.activate()
        XCTAssertTrue(app.staticTexts["spike.home.title"].waitForExistence(timeout: 10))
    }
}
