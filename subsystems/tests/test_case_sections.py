"""docs/17 先导批 parser 切片红证（2026-11-16，hostless 纯函数）。

两个 section 路由切片：
- ``**前置**：`` 段 → ``TestCase.preconditions``（模型/审批/回写渲染早已支持，
  本切片只补 parser 抽取，老文档无该段时恒为空，行为不变）；
- 独立 ``**背景与平台约束**`` 段 → ``context_notes``，与 docs/16 C2 的预期段内
  词表分流合流到同一背景桶，永不产出 oracle。

红证纪律：每条新能力配定点夹具，切片回退（不识别新段）必须变红；历史文档
夹具（无前置/背景段）必须保持逐字旧行为。
"""

from __future__ import annotations

from testpilot_core.approval import render_case_markdown
from testpilot_core.enums import OracleType
from testpilot_core.testcase import parse_case_markdown


def _parse(text: str):
    result = parse_case_markdown(text, doc_external_id="cases/d.md")
    assert len(result.cases) == 1
    return result.cases[0], result.dead_letters


# ── 前置段 ───────────────────────────────────────────────────────────────────

def test_precondition_section_bullets_populate_preconditions():
    case, dead = _parse(
        "## 模块 M\n\n"
        "### TC-S1 前置段夹具（FR-1）P0\n\n"
        "**前置**：\n"
        "- 冷启携带 `--ak-clear-all`，无残留 mission\n"
        "- 通知授权已授予\n\n"
        "1. 建一个快速闹钟\n"
        "2. 锁屏等响\n\n"
        "**预期**：\n"
        "- 全屏闹钟展示\n"
    )
    assert dead == []
    # 反引号装饰与步骤同口径在 _strip_inline 中去除，文本本身保留
    assert case.preconditions == [
        "冷启携带 --ak-clear-all，无残留 mission",
        "通知授权已授予",
    ]
    assert len(case.steps) == 2
    assert len(case.oracles) == 1


def test_precondition_nested_bullets_and_continuation_merge():
    case, _ = _parse(
        "## 模块 M\n\n"
        "### TC-S2 前置嵌套夹具 P1\n\n"
        "**前置**：\n"
        "- 主前置一条\n"
        "  - 嵌套子项甲\n"
        "  - 嵌套子项乙\n"
        "- 另一条前置，续行散文\n"
        "  拼到同一条里\n\n"
        "1. 唯一步骤\n\n"
        "**预期**：\n"
        "- 页面展示横幅\n"
    )
    assert case.preconditions == [
        "主前置一条 嵌套子项甲 嵌套子项乙",
        "另一条前置，续行散文 拼到同一条里",
    ]
    # 前置子弹绝不泄漏进步骤或断言
    assert case.steps == ["唯一步骤"]
    assert len(case.oracles) == 1


def test_precondition_inline_text_after_marker_colon():
    case, _ = _parse(
        "## M\n\n### TC-S3 行内前置 P0\n\n"
        "**前置**：设备已解锁且在前台\n\n"
        "1. 到点观察\n\n"
        "**预期**：\n- 横幅展示\n"
    )
    assert case.preconditions == ["设备已解锁且在前台"]


def test_precondition_section_ends_when_numbered_steps_begin():
    case, _ = _parse(
        "## M\n\n### TC-S4 段切换夹具 P0\n\n"
        "**前置**：\n"
        "- 只有这一条前置\n\n"
        "1. 第一个动作\n"
        "2. 第二个动作\n\n"
        "**预期**：\n- 接管页展示\n"
    )
    assert case.preconditions == ["只有这一条前置"]
    assert case.steps == ["第一个动作", "第二个动作"]


def test_legacy_doc_without_precondition_section_stays_empty():
    # 历史行为守卫：无前置段时字段恒空，步骤/断言解析逐字不变
    case, _ = _parse(
        "## M\n\n### TC-S5 老形态夹具 P0\n\n"
        "1. 排一个快速闹钟\n"
        "2. 锁屏等待\n\n"
        "**预期**：\n"
        "- T0 见全屏闹钟\n"
        "- 日志见 KEEPALIVE armed\n"
    )
    assert case.preconditions == []
    assert case.context_notes == []
    assert len(case.steps) == 2 and len(case.oracles) == 2


def test_precondition_marker_without_colon_is_not_a_section():
    # 缺冒号是 LINT-C-001 的管辖对象；parser 侧保守不识别，不得误吞后续步骤
    case, _ = _parse(
        "## M\n\n### TC-S6 缺冒号夹具 P0\n\n"
        "**前置**\n"
        "- 这条不应进 preconditions\n\n"
        "1. 正式步骤\n\n"
        "**预期**：\n- 页面可见\n"
    )
    assert case.preconditions == []
    assert case.steps == ["正式步骤"]


def test_render_round_trip_preserves_preconditions():
    original, _ = _parse(
        "## M\n\n### TC-S7 回写往返夹具（FR-7）P1\n\n"
        "**前置**：\n- 冷启无残留\n\n"
        "1. 建钟\n2. 等响\n\n"
        "**预期**：\n- 【UI 断言｜步骤2】全屏闹钟展示\n"
    )
    markdown = render_case_markdown(
        original, original.oracles, reviewed_by="tester"
    )
    assert "**前置**：" in markdown
    reparsed, dead = _parse(markdown)
    assert dead == []
    assert reparsed.preconditions == original.preconditions
    assert reparsed.steps == original.steps
    assert len(reparsed.oracles) == len(original.oracles)
    assert reparsed.oracles[0].step_id == 2


# ── 独立背景段 ─────────────────────────────────────────────────────────────────

def test_independent_background_section_routes_to_context_notes():
    case, dead = _parse(
        "## M\n\n### TC-S8 背景段夹具（FR-2）P0\n\n"
        "1. 锁屏等响\n2. 按侧边键\n\n"
        "**预期**：\n"
        "- 【UI 断言｜步骤2】循环铃声无缝继续\n\n"
        "**背景与平台约束**（仅供理解，不可作为断言依据，不进自动化导出）：\n"
        "- 平台约束：后台非混音会话必抛 !int\n"
        "- 版本差异：iOS 26.4 不投递 LA .ended\n"
    )
    assert dead == []
    assert len(case.oracles) == 1
    assert case.oracles[0].step_id == 2
    assert case.context_notes == [
        "平台约束：后台非混音会话必抛 !int",
        "版本差异：iOS 26.4 不投递 LA .ended",
    ]


def test_background_section_without_colon_still_recognized():
    case, _ = _parse(
        "## M\n\n### TC-S9 无冒号背景夹具 P1\n\n"
        "1. 一步\n\n"
        "**预期**：\n- 页面展示\n\n"
        "**背景与平台约束**\n"
        "- 机制见 SystemStopGate 文档\n"
    )
    assert case.context_notes == ["机制见 SystemStopGate 文档"]
    assert len(case.oracles) == 1


def test_background_nested_bullets_merge_and_inline_form():
    case, _ = _parse(
        "## M\n\n### TC-S10 背景嵌套夹具 P0\n\n"
        "1. 一步\n\n"
        "**预期**：\n- 状态离开 .alerting\n\n"
        "**背景与平台约束**：\n"
        "- 平台约束主句\n"
        "  - 子项甲\n"
        "  - 子项乙\n"
        "  - 缩进编号续行 1. 编号子项\n"
    )
    assert case.context_notes == [
        "平台约束主句 子项甲 子项乙 缩进编号续行 1. 编号子项"
    ]
    assert all("平台约束" not in o.expectation for o in case.oracles)


def test_background_section_coexists_with_in_expect_c2_routing():
    # 预期段内词表分流（C2）与独立背景段合流同一桶，顺序为文档出现顺序
    case, _ = _parse(
        "## M\n\n### TC-S11 双源背景夹具 P0\n\n"
        "1. 一步\n2. 二步\n\n"
        "**预期**：\n"
        "- 真机实锤平台约束：会话只能 handoff\n"
        "- 锁屏无补救通知\n\n"
        "**背景与平台约束**：\n"
        "- 独立段背景一条\n"
    )
    assert len(case.oracles) == 1
    assert "真机实锤" in case.context_notes[0]
    assert case.context_notes[1] == "独立段背景一条"
    assert case.oracle_missing is False


def test_record_marker_terminates_background_section():
    case, _ = _parse(
        "## M\n\n### TC-S12 实测记录收尾夹具 P0\n\n"
        "1. 一步\n\n"
        "**预期**：\n- 铃声继续\n\n"
        "**背景与平台约束**：\n- 背景一条\n\n"
        "**实测记录（2026-09-16，iPhone 16 Pro / iOS 26.4）**\n"
        "- 修复前轮：主钟接管失败\n"
    )
    assert case.context_notes == ["背景一条"]
    assert len(case.oracles) == 1
    # 实测记录散文不进任何桶
    assert all("修复前" not in n for n in case.context_notes)


def test_table_form_preconditions_and_context_stay_empty():
    text = (
        "## 模块 T\n\n"
        "| ID | 环境 | 步骤 | 预期 | 级 |\n"
        "|---|---|---|---|---|\n"
        "| TC-T1 | 🤖 | 建钟并等待 | 横幅展示 | P0 |\n"
    )
    result = parse_case_markdown(text, doc_external_id="cases/d.md")
    table_case = next(c for c in result.cases if c.external_id == "TC-T1")
    assert table_case.preconditions == []
    assert table_case.context_notes == []
    assert len(table_case.oracles) == 1
    assert table_case.oracles[0].type == OracleType.UI_ASSERTION
    assert table_case.oracles[0].expectation == "横幅展示"
