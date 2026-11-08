"""E6 离线对比探针 scripts/chunker_compare.py 的纯函数单测。

探针本体要扫真实语料仓 + xcrun swiftc，进不了无宿主测试；这里只锁它的
dump-ast 解析与 GT 连接逻辑（实测中真实踩过的两个坑：range 结束行误回退、
file: URI 与属性行偏移导致的假漏报），样例截自真实 App.swift dump-ast 输出形态。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

SUBSYSTEMS = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "chunker_compare", SUBSYSTEMS / "scripts" / "chunker_compare.py")
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)

# 真实形态：声明名在 range 之后、属性之前；implicit 合成构造器须排除；
# range 末端 col=1 指向闭合括号本身所在行（不回退）。
_AST_SAMPLE = '''(source_file "/x/App.swift"
  (import_decl range=[/x/App.swift:1:1 - line:1:8] module="SwiftUI")
  (struct_decl decl_context=0x1 range=[/x/App.swift:19:1 - line:34:1] "AlarmKitSpikeApp" interface_type="X.Type" access=internal
    (func_decl decl_context=0x2 implicit range=[/x/App.swift:18:1 - line:18:1] "$main()" interface_type="() -> ()")
    (constructor_decl decl_context=0x2 implicit range=[/x/App.swift:19:8 - line:19:8] "init()" interface_type="() -> X"))
  (class_decl decl_context=0x3 range=[/x/App.swift:39:1 - line:862:1] "AlertListViewModel" access=internal
    (constructor_decl decl_context=0x4 range=[/x/App.swift:78:5 - line:104:5] "init()" access=internal designated)
    (func_decl decl_context=0x4 range=[/x/App.swift:411:5 - line:419:5] "saveImportantDate(_:)" access=internal)
    (func_decl decl_context=0x4 range=[/x/App.swift:437:5 - line:443:5] "setImportantDateEnabled(_:enabled:)" access=internal))
  (extension_decl decl_context=0x5 range=[/x/App.swift:900:1 - line:910:1] "Notification.Name" access=internal)
'''


def test_parse_ast_ground_truth_filters_implicit_and_keeps_selector_names():
    gt = probe.parse_ast_ground_truth(_AST_SAMPLE)
    by_name = {d["name"]: d for d in gt}
    # implicit 的 $main 与合成 init 被排除，显式 init 保留
    assert "$main()" not in by_name
    assert sum(1 for d in gt if d["name"] == "init()") == 1
    # 多参数选择器与 dotted 扩展原名逐字保留
    assert by_name["saveImportantDate(_:)"]["start_line"] == 411
    assert by_name["setImportantDateEnabled(_:enabled:)"]["start_line"] == 437
    assert by_name["Notification.Name"]["kind"] == "extension_decl"


def test_parse_ast_end_line_points_at_closing_brace_line():
    # 回归坑：range 末端 line:34:1 是闭合括号所在行，曾被错误回退到 33
    gt = {d["name"]: d for d in probe.parse_ast_ground_truth(_AST_SAMPLE)}
    assert gt["AlarmKitSpikeApp"]["end_line"] == 34
    assert gt["AlertListViewModel"]["end_line"] == 862
    # 单行声明（起止同行）保持该行
    assert gt["saveImportantDate(_:)"]["end_line"] == 419


def test_anchor_key_strips_disambiguation_and_type_prefix():
    assert probe._anchor_key("Foo#L2") == "Foo"
    assert probe._anchor_key("AlertListViewModel.saveImportantDate(_:)") == "saveImportantDate(_:)"
    assert probe._anchor_key("topLevel()") == "topLevel()"


def _chunk(anchor, start_line, end_line):
    return SimpleNamespace(anchor=anchor, start_line=start_line, end_line=end_line)


def test_join_gt_exact_offset_artifact_and_true_miss():
    chunks = [
        _chunk("ViewModel.save(_:)", 411, 419),       # 精确命中
        _chunk("ViewModel.modern()", 38, 40),          # 属性行偏移（GT 关键字在 39）
        _chunk("ViewModel.register(role:fire:entry:)", 338, 347),  # 同位置异名
    ]
    hit, mode, delta = probe._join_gt(
        {"name": "save(_:)", "start_line": 411}, chunks)
    assert mode == "exact" and hit.anchor == "ViewModel.save(_:)" and delta == 0

    hit, mode, delta = probe._join_gt(
        {"name": "modern()", "start_line": 39}, chunks)
    assert mode == "offset" and delta == -1

    # GT 选择器不含返回元组标签，anchor 含 → 同位置判命名口径差，不算漏报
    hit, mode, delta = probe._join_gt(
        {"name": "register(role:fire:)", "start_line": 338}, chunks)
    assert mode == "artifact" and hit is not None

    # 同位置 ±5 行无任何块才是真漏报
    assert probe._join_gt({"name": "ghost()", "start_line": 700}, chunks)[1] == "miss"
