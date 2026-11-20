// device_hub_ax_probe.swift — Device Hub 菜单/工具条刺激动作的 AX 可达性探针
//
// 人工手动探针，永不进 verify_s0（与 device_chain_probe.py 同级纪律）。
// 背景与结论见 docs/archive/04（EVD-20261113-02） 附录 B、out/eval/device-hub-stimulus-probe/。
//
// 用法：
//   xcrun swift scripts/device_hub_ax_probe.swift                 # 只读诊断（默认，零副作用）
//   xcrun swift scripts/device_hub_ax_probe.swift --probe-actions # 额外做合成输入对照实验
//
// 前置：
//   1. 系统设置 → 隐私与安全性 → 辅助功能：运行本脚本的终端（或宿主进程）已勾选；
//   2. Device Hub 已打开且目标设备标签处于前台（模拟器需先在窗口内 Start 到可交互态）；
//   3. --probe-actions 会真实点击 Capture Keyboard 复选框（结束时恢复原值）并尝试
//      Home 动作（对模拟器无害；物理真机上等同于按一次 Home 键，知悉后再开）。
//
// 退出码：0 取证完成；2 未找到 Device Hub 进程；3 AX 权限不足；4 设备会话未就绪。

import AppKit
import ApplicationServices
import CoreGraphics
import Foundation

// MARK: - 进程定位（libproc 按可执行路径，绕开 NSWorkspace/System Events 的坏关联）
//
// 实测（2026-11-13，Xcode 27.0）：Device Hub 长驻后 NSWorkspace.runningApplications
// 给出 processIdentifier=-1、System Events 的 unix id=0，两个高层封装对该进程的
// 关联均损坏；只有 proc_listpids/proc_pidpath 拿到的真实 PID 可用于 AXUIElement。

import Darwin

func runningPid() -> pid_t? {
    let size = Int(proc_listpids(UInt32(PROC_ALL_PIDS), 0, nil, 0))
    guard size > 0 else { return nil }
    var pids = [pid_t](repeating: 0, count: size / MemoryLayout<pid_t>.size)
    let used = proc_listpids(UInt32(PROC_ALL_PIDS), 0, &pids, Int32(size))
    let count = Int(used) / MemoryLayout<pid_t>.size
    var pathBuffer = [CChar](repeating: 0, count: 4096)
    for index in 0..<count {
        let candidate = pids[index]
        if candidate <= 0 { continue }
        let length = proc_pidpath(candidate, &pathBuffer, UInt32(pathBuffer.count))
        if length > 0, String(cString: pathBuffer).contains("DeviceHub.app/Contents/MacOS/DeviceHub") {
            return candidate
        }
    }
    return nil
}

guard let pid = runningPid() else {
    FileHandle.standardError.write("未找到 DeviceHub 进程（DeviceHub.app/Contents/MacOS/DeviceHub），请先打开 Device Hub\n".data(using: .utf8)!)
    exit(2)
}
print("== Device Hub AX 探针，pid=\(pid) ==")

let axApp = AXUIElementCreateApplication(pid)

func raw(_ element: AXUIElement, _ attribute: String) -> CFTypeRef? {
    var value: CFTypeRef?
    return AXUIElementCopyAttributeValue(element, attribute as CFString, &value) == .success ? value : nil
}
func kids(_ element: AXUIElement) -> [AXUIElement] {
    (raw(element, kAXChildrenAttribute as String) as? [AXUIElement]) ?? []
}
func attr(_ element: AXUIElement, _ attribute: String) -> String {
    raw(element, attribute) as? String ?? ""
}
func enabled(_ element: AXUIElement) -> Bool {
    raw(element, kAXEnabledAttribute as String) as? Bool ?? false
}

// MARK: - 0. AX 权限自检（对 Finder 做只读枚举，失败即权限问题）

func accessibilityGranted() -> Bool {
    guard let finder = NSWorkspace.shared.runningApplications
        .first(where: { $0.bundleIdentifier == "com.apple.finder" }) else { return false }
    let fAx = AXUIElementCreateApplication(finder.processIdentifier)
    var value: CFTypeRef?
    return AXUIElementCopyAttributeValue(fAx, kAXMenuBarAttribute as CFString, &value) == .success
}
guard accessibilityGranted() else {
    FileHandle.standardError.write("AX 权限不足：系统设置 → 隐私与安全性 → 辅助功能中勾选宿主终端\n".data(using: .utf8)!)
    exit(3)
}
print("[0] AX 权限自检：通过（Finder menu bar 可读）")

// MARK: - 1. 菜单栏 Controls 全量盘点（标题语言相关，快捷键属性语言无关）

print("\n[1] Controls 菜单盘点")
if let menuBarRef = raw(axApp, kAXMenuBarAttribute as String) {
    let menuBar = menuBarRef as! AXUIElement
    var controlsFound = false
    for barItem in kids(menuBar) where attr(barItem, kAXTitleAttribute as String) == "Controls" {
        controlsFound = true
        guard let menu = kids(barItem).first else { continue }
        for item in kids(menu) where attr(item, kAXRoleAttribute as String) == "AXMenuItem" {
            let title = attr(item, kAXTitleAttribute as String)
            if title.isEmpty { continue }
            let cmdChar = raw(item, "AXMenuItemCmdChar") as? String ?? "-"
            let cmdMods = raw(item, "AXMenuItemCmdModifiers") as? Int ?? -1
            print("    \(title.padding(toLength: 14, withPad: " ", startingAt: 0)) "
                  + "enabled=\(enabled(item)) cmdChar=\(cmdChar) cmdMods=\(cmdMods)")
        }
    }
    if !controlsFound { print("    未找到 Controls 菜单（设备标签可能未前置或 Xcode 版本菜单结构变化）") }
}

// MARK: - 2. 窗口内工具条设备控件盘点（SF Symbol 标识符，语言无关）

print("\n[2] 窗口内工具条设备控件")
let wantedIds = ["app.grid.3x3", "camera.viewfinder", "record.circle"]
let wantedDescs = ["Home", "Screenshot", "Record", "Rotate Left", "Rotate Right"]
var sessionReady = false
func walkButtons(_ element: AXUIElement, depth: Int) {
    if depth > 14 { return }
    let role = attr(element, kAXRoleAttribute as String)
    if role == "AXButton" {
        let id = attr(element, "AXIdentifier")
        let desc = attr(element, kAXDescriptionAttribute as String)
        if wantedIds.contains(id) || wantedDescs.contains(desc) {
            var p = CGPoint.zero
            var s = CGSize.zero
            if let pv = raw(element, kAXPositionAttribute as String) { AXValueGetValue(pv as! AXValue, .cgPoint, &p) }
            if let sv = raw(element, kAXSizeAttribute as String) { AXValueGetValue(sv as! AXValue, .cgSize, &s) }
            print("    desc=\(desc.padding(toLength: 12, withPad: " ", startingAt: 0)) "
                  + "id=\(id.padding(toLength: 18, withPad: " ", startingAt: 0)) enabled=\(enabled(element)) frame=(\(Int(p.x)),\(Int(p.y)) \(Int(s.width))x\(Int(s.height)))")
            if desc == "Home" && enabled(element) { sessionReady = true }
        }
    }
    for child in kids(element) { walkButtons(child, depth: depth + 1) }
}
walkButtons(axApp, depth: 0)

let probeActions = CommandLine.arguments.contains("--probe-actions")
guard probeActions else {
    print("\n[只读模式结束] 加 --probe-actions 可做合成输入对照实验（会动 Capture Keyboard 与 Home）")
    exit(0)
}

// MARK: - 3. 合成输入自检：CGEvent 鼠标移动闭环（证明事件能到达 window server）

print("\n[3] CGEvent 注入自检")
guard let source = CGEventSource(stateID: .hidSystemState) else {
    print("    CGEventSource 创建失败"); exit(3)
}
let savedCursor = CGEvent(source: nil)?.location ?? .zero
let probePoint = CGPoint(x: 200, y: 200)
CGEvent(mouseEventSource: source, mouseType: .mouseMoved, mouseCursorPosition: probePoint, mouseButton: .left)?
    .post(tap: .cghidEventTap)
Thread.sleep(forTimeInterval: 0.4)
let movedTo = CGEvent(source: nil)?.location ?? .zero
let injectionWorks = movedTo == probePoint
CGEvent(mouseEventSource: source, mouseType: .mouseMoved, mouseCursorPosition: savedCursor, mouseButton: .left)?
    .post(tap: .cghidEventTap)
print("    合成鼠标移动生效=\(injectionWorks)（\(savedCursor) → \(movedTo)，已恢复光标）")

// MARK: - 4. 本地控件对照：HID 点击 Capture Keyboard 并读回翻转（结束恢复原值）

print("\n[4] 本地控件合成点击对照（Capture Keyboard）")
var checkBox: AXUIElement?
func findCheckBox(_ element: AXUIElement, depth: Int) {
    if depth > 14 { return }
    if attr(element, kAXDescriptionAttribute as String) == "Capture Keyboard" { checkBox = element }
    for child in kids(element) { findCheckBox(child, depth: depth + 1) }
}
findCheckBox(axApp, depth: 0)
if let cb = checkBox {
    let before = raw(cb, kAXValueAttribute as String)
    var p = CGPoint.zero
    var s = CGSize.zero
    if let pv = raw(cb, kAXPositionAttribute as String) { AXValueGetValue(pv as! AXValue, .cgPoint, &p) }
    if let sv = raw(cb, kAXSizeAttribute as String) { AXValueGetValue(sv as! AXValue, .cgSize, &s) }
    let center = CGPoint(x: p.x + s.width / 2, y: p.y + s.height / 2)
    for down in [true, false] {
        CGEvent(mouseEventSource: source, mouseType: down ? .leftMouseDown : .leftMouseUp,
                mouseCursorPosition: center, mouseButton: .left)?.post(tap: .cghidEventTap)
        Thread.sleep(forTimeInterval: 0.12)
    }
    Thread.sleep(forTimeInterval: 0.6)
    let after = raw(cb, kAXValueAttribute as String)
    let localControlResponds = String(describing: before) != String(describing: after)
    if localControlResponds {
        print("    Capture Keyboard \(String(describing: before)) → \(String(describing: after))，本地控件接受合成点击=true")
        // 恢复原值：若发生翻转则再点一次
        for down in [true, false] {
            CGEvent(mouseEventSource: source, mouseType: down ? .leftMouseDown : .leftMouseUp,
                    mouseCursorPosition: center, mouseButton: .left)?.post(tap: .cghidEventTap)
            Thread.sleep(forTimeInterval: 0.12)
        }
        Thread.sleep(forTimeInterval: 0.4)
        print("    已恢复 Capture Keyboard 至 \(String(describing: before))")
    } else {
        print("    Capture Keyboard \(String(describing: before)) → \(String(describing: after))，未翻转：")
        print("    设备会话可能已关闭（模拟器需 Start 到可交互态/真机需 connected）。请在会话活跃时重跑，")
        print("    会话活跃仍不翻转才是异常；2026-11-13 活跃会话下实测可稳定翻转（docs/archive/04（EVD-20261113-02） 附录 B E19）。")
    }
} else {
    print("    未找到 Capture Keyboard（设备流视图未初始化？）")
}

// MARK: - 5. 设备动词实验：AXPress 窗口内 Home（设备侧效果需人工/截图判读）

print("\n[5] 设备动词实验（Home）")
guard sessionReady else {
    print("    Home 按钮未处于 enabled：设备会话未就绪（模拟器先 Start、物理机确认 connected）。exit 4")
    exit(4)
}
print("    提示：enabled 状态在会话拆除后可能滞后仍为 true，设备侧效果一律以截图/录屏对照为准。")
var homeButton: AXUIElement?
func findHome(_ element: AXUIElement, depth: Int) {
    if depth > 14 { return }
    if attr(element, "AXIdentifier") == "app.grid.3x3" { homeButton = element }
    for child in kids(element) { findHome(child, depth: depth + 1) }
}
findHome(axApp, depth: 0)
if let home = homeButton {
    let err = AXUIElementPerformAction(home, kAXPressAction as CFString)
    print("    AXPress Home 返回 err=\(err.rawValue)（注意：err=0 仅代表 AX 调用被接受，")
    print("    不代表设备侧动作生效——必须用 simctl io screenshot 或真机录屏做前后对照判读）")
}
print("\n取证完成。判读口径见 docs/archive/04（EVD-20261113-02） 附录 B：本地控件翻转而设备动词无效果，即为合成输入隔离墙复现。")
exit(0)
