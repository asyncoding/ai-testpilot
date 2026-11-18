// GS 假仓 SPM 圈外夹具（S5）：模拟 TestPilotKit 包内的真实写法。
// 该文件路径不含 AgentGenerated 段，GS 扫描选盘层必须整文件跳过；
// 即使内含裸 sleep 也不允许计入违规（作用域守卫 fail-closed）。
import Foundation

enum TPFakeSPMFixture {
    static func backoff() async {
        // Task.sleep：SDK 内部超时竞速，GS-001 当前规则缺口（不匹配 Task.sleep）。
        try? await Task.sleep(nanoseconds: 500_000_000)
    }

    static func legacyBackoff() {
        // 裸 sleep 同样只在 AgentGenerated 圈内才违规；圈外文件选盘层根本不收。
        sleep(1)
    }
}
