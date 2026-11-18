"""docs/17 M1 lint 探针红证（2026-11-16，hostless 纯函数 + 一个 worker 装配测试）。

每条规则配「违规夹具必报 + 修复夹具必静默」一对断言，等价于把变异/回退
红证写进用例本身：若探针规则被删或阈值被放松，违规夹具立刻变红；若规则
误报，修复夹具变红。合成夹具不依赖语料仓，真档基线由
``testpilot-lint-cases`` CLI 在本机指向 app-calendar-alarm 仓执行。
"""

from __future__ import annotations

import pytest

from testpilot_adapters.case_docs import CaseDocumentAdapter
from testpilot_core.case_lint import (
    MAX_ORACLE_CHARS,
    MAX_ORACLE_LOG_TOKENS,
    aggregate_reports,
    lint_case_doc,
)
from testpilot_core.testcase import parse_case_markdown
from testpilot_workers.lint_cases import run_lint


def _lint(text: str):
    result = parse_case_markdown(text, doc_external_id="cases/d.md")
    return lint_case_doc(text, result, "cases/d.md"), result


def _findings(report, rule_id: str):
    return [f for f in report.findings if f.rule_id == rule_id]


def _narrative(case_id: str, body: str) -> str:
    return f"## 模块 M\n\n### {case_id} 夹具（FR-1）P0\n\n{body}\n"


# ── LINT-C-001 标记缺冒号 ─────────────────────────────────────────────────────

def test_001_missing_colon_marker_is_error_with_line_owner():
    report, _ = _lint(_narrative(
        "TC-L1",
        "1. 一步\n\n**预期**\n- 横幅展示\n",
    ))
    findings = _findings(report, "LINT-C-001")
    assert len(findings) == 1
    assert findings[0].severity == "error"
    assert findings[0].case_id == "TC-L1"
    assert findings[0].line is not None


def test_001_colon_form_and_optional_colon_background_are_clean():
    report, _ = _lint(_narrative(
        "TC-L2",
        "1. 一步\n\n**预期**：\n- 横幅展示\n\n"
        "**背景与平台约束**\n- 机制见内部文档\n",
    ))
    assert _findings(report, "LINT-C-001") == []


# ── LINT-C-002 真缺预期 ───────────────────────────────────────────────────────

def test_002_steps_without_oracle_is_error():
    report, _ = _lint(_narrative("TC-L3", "1. 只有步骤\n2. 没有预期段\n"))
    findings = _findings(report, "LINT-C-002")
    assert len(findings) == 1 and findings[0].case_id == "TC-L3"


def test_002_silent_when_oracle_present():
    report, _ = _lint(_narrative(
        "TC-L4", "1. 一步\n\n**预期**：\n- 横幅展示\n",
    ))
    assert _findings(report, "LINT-C-002") == []


# ── LINT-C-003 死信与步骤编号 ─────────────────────────────────────────────────

def test_003_anchor_dead_letter_surfaces_as_error():
    report, result = _lint(_narrative(
        "TC-L5",
        "1. 唯一步骤\n\n**预期**：\n- 【UI 断言｜步骤9】横幅展示\n",
    ))
    assert len(result.dead_letters) == 1
    findings = _findings(report, "LINT-C-003")
    assert findings and findings[0].severity == "error"
    assert "解析死信" in findings[0].message
    assert report.metrics["parse_dead_letters"] == 1


def test_003_step_number_gap_is_error_and_contiguous_is_clean():
    broken, _ = _lint(_narrative("TC-L6", "1. 甲\n3. 丙\n\n**预期**：\n- 状态正确\n"))
    gap = _findings(broken, "LINT-C-003")
    assert gap and "不连续" in gap[0].message

    clean, _ = _lint(_narrative(
        "TC-L7", "1. 甲\n2. 乙\n\n**预期**：\n- 状态正确\n",
    ))
    assert _findings(clean, "LINT-C-003") == []


# ── LINT-C-004 知识材料未独立成段 ─────────────────────────────────────────────

def test_004_mixed_in_context_warns_but_independent_section_is_clean():
    mixed, _ = _lint(_narrative(
        "TC-L8",
        "1. 一步\n\n**预期**：\n"
        "- 真机实锤平台约束：会话只能 handoff\n- 横幅展示\n",
    ))
    findings = _findings(mixed, "LINT-C-004")
    assert len(findings) == 1 and findings[0].severity == "warning"

    separated, _ = _lint(_narrative(
        "TC-L9",
        "1. 一步\n\n**预期**：\n- 横幅展示\n\n"
        "**背景与平台约束**：\n- 真机实锤平台约束：会话只能 handoff\n",
    ))
    assert _findings(separated, "LINT-C-004") == []


# ── LINT-C-005 超长断言（双阈值） ─────────────────────────────────────────────

def test_005_char_threshold_and_backtick_token_threshold():
    unit = "闹钟响铃页面展示完整且按钮可点按状态正确。"  # 21 字，无背景词表
    long_text = unit * 12  # 252 字，确定性越过 240 阈值
    assert len(long_text) > MAX_ORACLE_CHARS
    report, parsed = _lint(_narrative(
        "TC-L10", f"1. 一步\n\n**预期**：\n- {long_text}\n",
    ))
    assert len(parsed.cases[0].oracles) == 1
    findings = _findings(report, "LINT-C-005")
    assert len(findings) == 1 and "超长断言" in findings[0].message

    tokens = " ".join(f"`log{i}`" for i in range(MAX_ORACLE_LOG_TOKENS + 1))
    token_doc = _narrative(
        "TC-L11", f"1. 一步\n\n**预期**：\n- 日志见 {tokens} 九 token 短文本\n",
    )
    token_report, token_parsed = _lint(token_doc)
    expectation = token_parsed.cases[0].oracles[0].expectation
    assert len(expectation) <= MAX_ORACLE_CHARS  # 字符不超标，仅 token 数触发
    token_findings = _findings(token_report, "LINT-C-005")
    assert len(token_findings) == 1 and "日志 token" in token_findings[0].message


def test_005_normal_length_oracle_is_clean():
    report, _ = _lint(_narrative(
        "TC-L12", "1. 一步\n\n**预期**：\n- 横幅展示且按钮可点\n",
    ))
    assert _findings(report, "LINT-C-005") == []


def test_005_multiline_bullet_merges_continuations_before_measure():
    # 红证：首行很短、续行把总长推过阈值；不合并续行就会漏报（真档 TC-A1 形态）
    tail = "续行检查点事实描述" * 26  # 260 字，首行+续行确定越过 240
    report, _ = _lint(_narrative(
        "TC-L12b",
        "1. 一步\n\n**预期**：\n- 首行很短\n  嵌套子项一句\n" + tail + "\n",
    ))
    findings = _findings(report, "LINT-C-005")
    assert len(findings) == 1

    token_tail = " ".join(f"`t{i}`" for i in range(9))
    token_report, _ = _lint(_narrative(
        "TC-L12c",
        "1. 一步\n\n**预期**：\n- 首行短\n  " + token_tail + "\n",
    ))
    assert len(_findings(token_report, "LINT-C-005")) == 1


# ── LINT-C-006 无标签人工断言 ─────────────────────────────────────────────────

def test_006_untagged_manual_warns_tagged_and_auto_typed_are_clean():
    report, _ = _lint(_narrative(
        "TC-L13",
        "1. 一步\n\n**预期**：\n"
        "- 主观手感顺滑\n- 【人工确认】亲自听一遍铃声无杂音\n- 横幅展示\n",
    ))
    findings = _findings(report, "LINT-C-006")
    assert len(findings) == 1  # 仅无标签的主观句；显式标签与自动归类的 UI 句不报警


# ── LINT-C-007 表格体 ─────────────────────────────────────────────────────────

def test_007_table_case_warns_narrative_is_clean():
    table_doc = (
        "## 模块 T\n\n"
        "| ID | 环境 | 步骤 | 预期 | 级 |\n"
        "|---|---|---|---|---|\n"
        "| TC-T1 | 🤖 | 建钟并等待 | 横幅展示 | P0 |\n"
    )
    report, _ = _lint(table_doc)
    findings = _findings(report, "LINT-C-007")
    assert len(findings) == 1 and findings[0].case_id == "TC-T1"

    narrative, _ = _lint(_narrative(
        "TC-L14",
        "**前置**：\n- 通知授权已授予\n\n"
        "1. 一步\n\n**预期**：\n- 【UI 断言｜步骤1】横幅展示\n",
    ))
    assert _findings(narrative, "LINT-C-007") == []


# ── LINT-C-008 缺前置段 ───────────────────────────────────────────────────────

def test_008_missing_precondition_warns_with_scope_limits():
    missing, _ = _lint(_narrative(
        "TC-L15", "1. 一步\n\n**预期**：\n- 【UI 断言｜步骤1】横幅展示\n",
    ))
    assert len(_findings(missing, "LINT-C-008")) == 1

    with_precond, _ = _lint(_narrative(
        "TC-L16",
        "**前置**：\n- 通知授权已授予\n\n"
        "1. 一步\n\n**预期**：\n- 【UI 断言｜步骤1】横幅展示\n",
    ))
    assert _findings(with_precond, "LINT-C-008") == []

    # 表格体由 007 管辖；取消用例不参与结构规则
    table, _ = _lint(
        "## T\n\n| ID | 步骤 | 预期 | 级 |\n|---|---|---|---|\n"
        "| TC-T2 | 一步 | 横幅 | P0 |\n"
    )
    assert _findings(table, "LINT-C-008") == []

    cancelled, _ = _lint(_narrative(
        "~~TC-L17 已作废~~", "1. 一步\n\n**预期**：\n- 横幅展示\n",
    ))
    assert _findings(cancelled, "LINT-C-008") == []


# ── LINT-C-009 锚点覆盖 info ──────────────────────────────────────────────────

def test_009_all_unanchored_info_once_anchored_or_table_silent():
    unanchored, _ = _lint(_narrative(
        "TC-L18",
        "**前置**：\n- 授权到位\n\n1. 一步\n2. 二步\n\n"
        "**预期**：\n- 横幅展示\n- 状态刷新\n",
    ))
    findings = _findings(unanchored, "LINT-C-009")
    assert len(findings) == 1 and findings[0].severity == "info"

    anchored, _ = _lint(_narrative(
        "TC-L19",
        "**前置**：\n- 授权到位\n\n1. 一步\n\n"
        "**预期**：\n- 【UI 断言｜步骤1】横幅展示\n",
    ))
    assert _findings(anchored, "LINT-C-009") == []

    table, _ = _lint(
        "## T\n\n| ID | 步骤 | 预期 | 级 |\n|---|---|---|---|\n"
        "| TC-T3 | 一步 | 横幅 | P0 |\n"
    )
    assert _findings(table, "LINT-C-009") == []


# ── LINT-C-010 实测记录要素与下线符号 ─────────────────────────────────────────

def test_010_record_year_and_retired_symbol_scoping():
    undated, _ = _lint(_narrative(
        "TC-L20",
        "1. 一步\n\n**预期**：\n- 【UI 断言｜步骤1】横幅展示\n\n"
        "**实测记录（iPhone 16 Pro / iOS 26.4）**\n- 修复前轮：接管失败\n",
    ))
    assert len(_findings(undated, "LINT-C-010")) == 1

    dated, _ = _lint(_narrative(
        "TC-L21",
        "1. 一步\n\n**预期**：\n- 【UI 断言｜步骤1】横幅展示\n\n"
        "**实测记录（2026-09-16，iPhone 16 Pro / iOS 26.4）**\n"
        "- 历史音效 Glass1.caf 当轮实测仍可播放（原值冻结）\n",
    ))
    assert _findings(dated, "LINT-C-010") == []

    outside, _ = _lint(_narrative(
        "TC-L22",
        "1. 一步\n\n**预期**：\n- 【UI 断言｜步骤1】Glass1 入口可见\n",
    ))
    findings = _findings(outside, "LINT-C-010")
    assert len(findings) == 1 and "Glass1" in findings[0].message


# ── 聚合指标与多文档汇总 ──────────────────────────────────────────────────────

def test_metrics_reflect_doc_shape_and_cancelled_exclusion():
    doc = (
        "## 模块 M\n\n"
        "### TC-M1 完整夹具（FR-1）P0\n\n"
        "**前置**：\n- 通知授权已授予\n\n"
        "1. 第一步\n2. 第二步\n\n"
        "**预期**：\n"
        "- 【UI 断言｜步骤1】横幅展示\n"
        "- 状态字段刷新\n\n"
        "### TC-M2 缺预期夹具 P1\n\n"
        "1. 只有步骤\n\n"
        "### ~~TC-M3 作废夹具~~ P2\n\n"
        "1. 历史步骤\n\n"
        "## 模块 T\n\n"
        "| ID | 环境 | 步骤 | 预期 | 级 |\n"
        "|---|---|---|---|---|\n"
        "| TC-T9 | 🤖 | 表格一步 | 表格预期 | P1 |\n"
    )
    report, result = _lint(doc)
    assert len(result.cases) == 4
    m = report.metrics
    assert m["cases_total"] == 4
    assert m["cases_active"] == 3
    assert m["cases_cancelled"] == 1
    assert m["cases_table_form"] == 1
    assert m["oracle_missing_rate"] == round(1 / 3, 4)
    assert m["structure_pass_rate"] == round(1 - 1 / 3, 4)
    # 活跃断言 3 条（M1 两条 + 表格一条），仅 1 条带锚点
    assert m["anchor_coverage"] == round(1 / 3, 4)
    # 叙事活跃用例 M1/M2，仅 M1 有前置段
    assert m["precondition_coverage"] == 0.5
    assert m["knowledge_mix_in_rate"] == 0.0
    assert m["parse_dead_letters"] == 0
    assert m["finding_counts"]["error"] >= 1
    assert any(f.rule_id == "LINT-C-007" for f in report.findings)
    assert m["oracle_len_p50"] > 0 and m["oracle_len_p95"] >= m["oracle_len_p50"]


def test_aggregate_reports_unions_findings_and_recomputes_pass_rate():
    clean, _ = _lint(_narrative(
        "TC-G1",
        "**前置**：\n- 授权\n\n1. 一步\n\n"
        "**预期**：\n- 【UI 断言｜步骤1】横幅展示\n",
    ))
    broken, _ = _lint(_narrative("TC-G2", "1. 只有步骤\n"))
    agg = aggregate_reports([clean, broken])
    assert agg["docs"] == 2
    assert agg["cases_total"] == 2 and agg["cases_active"] == 2
    assert agg["structure_pass_rate"] == 0.5
    assert "LINT-C-002" in agg["rules_violated"]
    assert agg["finding_counts"]["error"] >= 1


# ── worker 装配（run_lint，不起进程、不碰 store） ─────────────────────────────

def test_run_lint_on_tmp_corpus_reports_status_and_shape(tmp_path):
    cases_dir = tmp_path / "cases"
    cases_dir.mkdir()
    (cases_dir / "good.md").write_text(
        _narrative(
            "TC-W1",
            "**前置**：\n- 通知授权已授予\n\n"
            "1. 一步\n\n**预期**：\n- 【UI 断言｜步骤1】横幅展示\n",
        ),
        encoding="utf-8",
    )
    (cases_dir / "bad.md").write_text(
        _narrative("TC-W2", "1. 一步\n\n**预期**\n- 缺冒号\n"),
        encoding="utf-8",
    )
    adapter = CaseDocumentAdapter([cases_dir])
    report = run_lint({"cases": adapter})

    assert report["status"] == "failed"  # error 级发现存在
    assert report["docs_seen"] == 2
    assert {d["external_id"] for d in report["docs"]} == {
        "cases/good.md", "cases/bad.md",
    }
    assert report["aggregate"]["finding_counts"]["error"] >= 1
    assert report["io_errors"] == []
    # 原文透传字段支撑了 001 行号定位
    bad_doc = next(d for d in report["docs"] if d["external_id"] == "cases/bad.md")
    assert any(f["rule_id"] == "LINT-C-001" and f["line"] for f in bad_doc["findings"])


def test_run_lint_clean_corpus_succeeds(tmp_path):
    cases_dir = tmp_path / "cases"
    cases_dir.mkdir()
    (cases_dir / "good.md").write_text(
        _narrative(
            "TC-W3",
            "**前置**：\n- 授权到位\n\n1. 一步\n\n"
            "**预期**：\n- 【UI 断言｜步骤1】横幅展示\n",
        ),
        encoding="utf-8",
    )
    report = run_lint({"cases": CaseDocumentAdapter([cases_dir])})
    assert report["status"] == "succeeded"
    assert report["aggregate"]["finding_counts"] == {"error": 0, "warning": 0, "info": 0}


def test_run_lint_without_adapter_raises():
    with pytest.raises(ValueError, match="cases 适配器"):
        run_lint({"cases": None})
