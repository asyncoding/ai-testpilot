// device_hub_ax_cli.swift — Device Hub AX 语义刺激生产 CLI（docs/01 §3/§9，H3）
//
// 与 out/eval/device-hub-stimulus-probe/dh_action.swift（取证探针，gitignored）的区别：
// 本文件是入库生产工具，只暴露真机已验能力圈内的四个动词（docs/archive/04（EVD-20261113-02） 附录 B.4，
// E24–E30）：readiness / home / rotate-left / screenshot。HID/CGEvent 合成输入
// 已证在模拟器与真机均惰性，本工具不提供任何 HID 动词。
//
// 纪律：
// - 只走 AXPress 语义通道；元素缺失/禁用/窗口零尺寸一律非零退出（fail-loud），
//   绝不打印成功假装完成（AX err=0 不等于设备侧生效，效果由编排层 devicectl
//   截图前后对照判读，见 docs/01 §9.4）；
// - 轮询替盲等：pollUntil 在 deadline 内反复探针，不用固定 Thread.sleep 猜转场；
// - stdout 最后一行输出单个 JSON 对象供适配器解析，诊断信息走 stderr；
// - Mac 锁屏（CGSSessionScreenIsLocked=1）是硬前置违背，readiness 直接 exit 6。
//
// 用法：
//   xcrun swift device_hub_ax_cli.swift readiness
//   xcrun swift device_hub_ax_cli.swift home
//   xcrun swift device_hub_ax_cli.swift rotate-left [--times N]
//   xcrun swift device_hub_ax_cli.swift screenshot
//
// 退出码：0 成功；2 DeviceHub 进程不存在；3 参数/动词非法；4 元素缺失或窗口
// 零尺寸；5 AXPress 返回非 success；6 AX 未授权或 Mac 锁屏。

import ApplicationServices
import CoreGraphics
import Darwin
import Foundation

// MARK: - 进程定位（libproc；NSWorkspace/System Events 对 DeviceHub 关联损坏，docs/archive/04（EVD-20261113-02） E16）

func runningPid() -> pid_t? {
    let size = Int(proc_listpids(UInt32(PROC_ALL_PIDS), 0, nil, 0))
    guard size > 0 else { return nil }
    var pids = [pid_t](repeating: 0, count: size / MemoryLayout<pid_t>.size)
    let used = proc_listpids(UInt32(PROC_ALL_PIDS), 0, &pids, Int32(size))
    var buffer = [CChar](repeating: 0, count: 4096)
    for i in 0..<(Int(used) / MemoryLayout<pid_t>.size) {
        let pid = pids[i]
        if pid <= 0 { continue }
        if proc_pidpath(pid, &buffer, UInt32(buffer.count)) > 0,
           String(cString: buffer).contains("DeviceHub.app/Contents/MacOS/DeviceHub") {
            return pid
        }
    }
    return nil
}

// MARK: - 输出与退出码

enum ExitCode: Int32 {
    case ok = 0
    case processMissing = 2
    case badInvocation = 3
    case elementMissing = 4
    case axActionFailed = 5
    case trustOrLock = 6
}

func emit(_ object: [String: Any], code: ExitCode) -> Never {
    if let data = try? JSONSerialization.data(withJSONObject: object),
       let line = String(data: data, encoding: .utf8) {
        print(line)
    } else {
        print("{\"ok\":false,\"reason\":\"json_serialization_failed\"}")
    }
    exit(code.rawValue)
}

func fail(_ code: ExitCode, reason: String, extra: [String: Any] = [:]) -> Never {
    fputs("device_hub_ax_cli: \(reason)\n", stderr)
    var object: [String: Any] = ["ok": false, "reason": reason]
    extra.forEach { object[$0.key] = $0.value }
    emit(object, code: code)
}

// MARK: - AX 基础

guard let pid = runningPid() else {
    fail(.processMissing, reason: "DeviceHub 进程未找到")
}
let app = AXUIElementCreateApplication(pid)

func raw(_ element: AXUIElement, _ attribute: String) -> CFTypeRef? {
    var value: CFTypeRef?
    return AXUIElementCopyAttributeValue(
        element, attribute as CFString, &value) == .success ? value : nil
}

func kids(_ element: AXUIElement) -> [AXUIElement] {
    (raw(element, kAXChildrenAttribute as String) as? [AXUIElement]) ?? []
}

func frame(of element: AXUIElement) -> (CGPoint, CGSize) {
    var point = CGPoint.zero
    var size = CGSize.zero
    if let position = raw(element, kAXPositionAttribute as String) {
        AXValueGetValue(position as! AXValue, .cgPoint, &point)
    }
    if let sizeValue = raw(element, kAXSizeAttribute as String) {
        AXValueGetValue(sizeValue as! AXValue, .cgSize, &size)
    }
    return (point, size)
}

/// 条件轮询：deadline 秒内反复执行 probe，首个非 nil 结果返回；超时 nil。
/// 间隔 usleep 只是轮询节拍，不是对转场时长的盲猜（docs/01 §9.1）。
func pollUntil<T>(deadline seconds: Double = 5.0, interval: Double = 0.2,
                  _ probe: () -> T?) -> T? {
    let start = Date()
    repeat {
        if let hit = probe() { return hit }
        usleep(useconds_t(interval * 1_000_000))
    } while Date().timeIntervalSince(start) < seconds
    return nil
}

func findEnabledButton(description: String) -> AXUIElement? {
    var hit: AXUIElement?
    func walk(_ element: AXUIElement, depth: Int) {
        if depth > 14 || hit != nil { return }
        if (raw(element, kAXRoleAttribute as String) as? String) == kAXButtonRole,
           (raw(element, kAXDescriptionAttribute as String) as? String) == description,
           (raw(element, kAXEnabledAttribute as String) as? Bool ?? false) {
            hit = element
            return
        }
        for child in kids(element) { walk(child, depth: depth + 1) }
    }
    walk(app, depth: 0)
    return hit
}

func screenIsLocked() -> Bool {
    // 私有会话字典但字段稳定：docs/archive/04（EVD-20261113-02） E24 实证锁屏时窗口零尺寸、AX 子树脱物化
    guard let info = CGSessionCopyCurrentDictionary() as? [String: Any] else {
        return false
    }
    return info["CGSSessionScreenIsLocked"] as? Bool ?? false
}

func axTrusted() -> Bool {
    AXIsProcessTrustedWithOptions(
        ["kAXTrustedCheckOptionPrompt.tel" as CFString: false] as CFDictionary)
}

func materializedWindows() -> [[String: Any]] {
    guard let windows = raw(app, kAXWindowsAttribute as String)
        as? [AXUIElement] else { return [] }
    return windows.map { window in
        let (point, size) = frame(of: window)
        return [
            "title": raw(window, kAXTitleAttribute as String) as? String ?? "",
            "x": Int(point.x), "y": Int(point.y),
            "width": Int(size.width), "height": Int(size.height),
        ]
    }
}

/// 置前 DH 窗口并轮询等待工具条子树物化。
/// 实验实证（dh_action.swift activate，docs/archive/04（EVD-20261113-02） E18 旁证）：DH 非前台时
/// frontmost=0、工具条 AX 子树脱物化（只剩 Close 按钮），AXPress 无目标；
/// AX 置前后物化是异步的，必须轮询 Home 按钮出现而不是盲睡固定时长。
@discardableResult
func ensureFrontmost(deadline seconds: Double = 5.0) -> Bool {
    AXUIElementSetAttributeValue(
        app, kAXHiddenAttribute as CFString, kCFBooleanFalse)
    AXUIElementSetAttributeValue(
        app, kAXFrontmostAttribute as CFString, kCFBooleanTrue)
    return pollUntil(deadline: seconds) {
        findEnabledButton(description: "Home")
    } != nil
}

func readinessReport() -> [String: Any] {
    let windows = materializedWindows()
    let hasZeroSizeWindow = windows.allSatisfy {
        ($0["width"] as? Int ?? 0) == 0 || ($0["height"] as? Int ?? 0) == 0
    }
    let home = findEnabledButton(description: "Home") != nil
    let screenshot = findEnabledButton(description: "Screenshot") != nil
    let rotateLeft = findEnabledButton(description: "Rotate Left") != nil
    return [
        "ok": !hasZeroSizeWindow && home && screenshot && rotateLeft,
        "verb": "readiness",
        "pid": Int(pid),
        "screen_locked": screenIsLocked(),
        "ax_trusted": axTrusted(),
        "windows": windows,
        "buttons": ["home": home, "screenshot": screenshot,
                    "rotate_left": rotateLeft],
    ]
}

func press(description: String) -> (attempts: Int, error: AXError)? {
    let result = pollUntil { () -> (Int, AXError)? in
        guard let button = findEnabledButton(description: description) else {
            return nil  // 子树可能尚未物化，继续轮询
        }
        let error = AXUIElementPerformAction(
            button, kAXPressAction as CFString)
        return (1, error)
    }
    guard let (_, error) = result else { return nil }
    return (1, error)
}

// MARK: - 动词分发

let arguments = CommandLine.arguments
guard arguments.count > 1 else {
    fail(.badInvocation, reason: "缺动词：readiness/home/rotate-left/screenshot")
}
let verb = arguments[1]

switch verb {
case "readiness":
    if !axTrusted() {
        fail(.trustOrLock, reason: "进程未获得辅助功能（AX）授权")
    }
    if screenIsLocked() {
        fail(.trustOrLock, reason: "Mac 会话处于锁屏态，AX 窗口子树已脱物化（docs/archive/04（EVD-20261113-02） E24）",
             extra: readinessReport())
    }
    if !ensureFrontmost() {
        fail(.elementMissing,
             reason: "置前后轮询超时：工具条按钮仍不可达（会话窗口未物化？）",
             extra: readinessReport())
    }
    let report = readinessReport()
    if !(report["ok"] as? Bool ?? false) {
        fail(.elementMissing, reason: "DH 会话未就绪：窗口零尺寸或工具条按钮不可达",
             extra: report)
    }
    emit(report, code: .ok)

case "home":
    if !ensureFrontmost() {
        fail(.elementMissing, reason: "置前后轮询超时：Home 按钮不可达",
             extra: readinessReport())
    }
    guard let outcome = press(description: "Home") else {
        fail(.elementMissing, reason: "轮询期内未找到启用的 Home 按钮",
             extra: readinessReport())
    }
    guard outcome.error == .success else {
        fail(.axActionFailed, reason: "Home AXPress 失败",
             extra: ["ax_error": outcome.error.rawValue])
    }
    emit(["ok": true, "verb": "home", "pid": Int(pid)], code: .ok)

case "rotate-left":
    var times = 1
    var index = 2
    while index < arguments.count {
        if arguments[index] == "--times", index + 1 < arguments.count,
           let parsed = Int(arguments[index + 1]), parsed > 0 {
            times = parsed
            index += 2
        } else {
            fail(.badInvocation, reason: "rotate-left 仅支持 --times <正整数>")
        }
    }
    if !ensureFrontmost() {
        fail(.elementMissing, reason: "置前后轮询超时：Rotate Left 按钮不可达",
             extra: readinessReport())
    }
    // E27：方向不能按按压次数推断（iPhone 不渲染 portraitUpsideDown），
    // 本动词只负责按 N 次；竖屏复原由编排层截图尺寸轮询判读并循环补按。
    for pressIndex in 1...times {
        guard let outcome = press(description: "Rotate Left") else {
            fail(.elementMissing,
                 reason: "第 \(pressIndex)/\(times) 次 Rotate Left 按钮不可达",
                 extra: readinessReport())
        }
        guard outcome.error == .success else {
            fail(.axActionFailed, reason: "Rotate Left AXPress 失败",
                 extra: ["ax_error": outcome.error.rawValue,
                         "press_index": pressIndex])
        }
    }
    emit(["ok": true, "verb": "rotate-left", "times": times,
          "pid": Int(pid)], code: .ok)

case "screenshot":
    // DH 工具条截图的落盘位置由 Device Hub 自身设置决定；编排层的客观证据
    // 仍走 devicectl capture（auto:devicectl/screenshot），本动词只触发设备侧捕获。
    if !ensureFrontmost() {
        fail(.elementMissing, reason: "置前后轮询超时：Screenshot 按钮不可达",
             extra: readinessReport())
    }
    guard let outcome = press(description: "Screenshot") else {
        fail(.elementMissing, reason: "轮询期内未找到启用的 Screenshot 按钮",
             extra: readinessReport())
    }
    guard outcome.error == .success else {
        fail(.axActionFailed, reason: "Screenshot AXPress 失败",
             extra: ["ax_error": outcome.error.rawValue])
    }
    emit(["ok": true, "verb": "screenshot", "pid": Int(pid)], code: .ok)

default:
    fail(.badInvocation,
         reason: "未知动词 \(verb)（允许：readiness/home/rotate-left/screenshot）")
}
