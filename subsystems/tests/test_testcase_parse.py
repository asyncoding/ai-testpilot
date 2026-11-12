"""用例 Markdown 解析纯函数测试（docs/08 FR-CON-003，S2.1）。

夹具 tests/fixtures/cases/synthetic-cases.md 是自建小样（不拷 V1 原文），
覆盖叙事体/表格体两种形态、缺预期、取消态、损坏行、非用例表、代码围栏。
"""

from __future__ import annotations

from pathlib import Path

from testpilot_core.enums import OracleType
from testpilot_core.testcase import classify_oracle, parse_case_markdown

FIXTURE = Path(__file__).parent / "fixtures" / "cases" / "synthetic-cases.md"
DOC_ID = "cases/synthetic-cases.md"


def _parse():
    return parse_case_markdown(
        FIXTURE.read_text(encoding="utf-8"),
        doc_external_id=DOC_ID, version="hash12",
    )


def test_fixture_parses_all_cases_and_modules():
    result = _parse()
    by_id = {c.external_id: c for c in result.cases}
    # 叙事 3（含 1 取消）+ B 表 2 有效（损坏行进死信）+ C 表 2 = 7
    assert set(by_id) == {
        "TC-N1", "TC-N2", "TC-N3", "TC-B1", "TC-B6", "TC-C1", "TC-C2",
    }
    assert by_id["TC-N1"].module.startswith("1. 模块 A")
    assert by_id["TC-B1"].module.startswith("2. 模块 B")
    assert by_id["TC-C2"].module.startswith("3. 模块 C")
    # 非用例表（套件/结果）与围栏内 TC-FAKE 均不产生用例
    assert "TC-FAKE" not in by_id
    assert all(c.version == "hash12" for c in result.cases)


def test_narrative_steps_join_wrapped_lines_and_keep_source_line():
    case = next(c for c in _parse().cases if c.external_id == "TC-N1")
    assert len(case.steps) == 2  # 续行并入第 1 步，不产生第 3 步
    assert "续行说明应并入同一步骤" in case.steps[0]
    assert case.source_line is not None and case.source_line > 0
    assert case.priority == "P0"
    assert "FR-9" in case.tags and "FR-10" in case.tags
    # 实测记录段不被收进 oracle
    assert all("历史证据" not in o.expectation for o in case.oracles)


def test_oracle_classification_covers_four_types_with_manual_fallback():
    case = next(c for c in _parse().cases if c.external_id == "TC-N1")
    types = {o.type for o in case.oracles}
    assert types == {
        OracleType.LOG_ASSERTION,
        OracleType.UI_ASSERTION,
        OracleType.STATE_ASSERTION,
        OracleType.MANUAL,
    }
    assert classify_oracle("一条看不出信号的普通预期") is OracleType.MANUAL
    # 每条解析出的 oracle 都回链来源文档
    assert all(o.source_refs for o in case.oracles)


def test_missing_expectation_is_flagged_not_dropped():
    case = next(c for c in _parse().cases if c.external_id == "TC-N2")
    assert case.oracle_missing is True
    assert case.steps and not case.oracles
    assert case.lifecycle == "active"


def test_cancelled_cases_kept_in_both_formats():
    by_id = {c.external_id: c for c in _parse().cases}
    assert by_id["TC-N3"].lifecycle == "cancelled"  # 叙事体删除线标题
    assert by_id["TC-B6"].lifecycle == "cancelled"  # 表格体删除线 + 优先级=取消
    assert by_id["TC-B6"].priority == ""
    assert by_id["TC-N1"].lifecycle == "active"


def test_table_shapes_map_columns_and_env_tags():
    by_id = {c.external_id: c for c in _parse().cases}
    b1 = by_id["TC-B1"]
    assert b1.steps == ["前台到点响铃"]
    assert b1.oracles[0].type is OracleType.UI_ASSERTION
    assert b1.priority == "P0"
    c1 = by_id["TC-C1"]  # 五列形态：环境列单独映射
    assert c1.tags == ["env:🤖"]
    assert "今天分桶" in c1.oracles[0].expectation
    assert by_id["TC-C2"].tags == ["env:📱"]


def test_malformed_table_row_goes_to_dead_letter_without_blocking_batch():
    result = _parse()
    assert len(result.dead_letters) == 1
    error = result.dead_letters[0]
    assert error.doc_external_id == DOC_ID
    assert error.line is not None
    assert "列数" in error.error
    # 同表的 TC-B1/TC-B6 仍正常解析（不阻断整批，FR-CON-003 AC2）
    parsed_ids = {c.external_id for c in result.cases}
    assert {"TC-B1", "TC-B6"} <= parsed_ids


def test_heading_without_steps_or_oracles_is_dead_letter():
    text = "## 模块 X\n\n### TC-X9 空壳用例 P0\n\n## 模块 Y\n"
    result = parse_case_markdown(text, doc_external_id="d.md")
    assert result.cases == []
    assert len(result.dead_letters) == 1
    assert result.dead_letters[0].case_id_hint == "TC-X9"


def test_urn_stable_across_reparse_and_idempotent():
    text = "## M\n\n### TC-S1 标题 P0\n\n1. 一步\n\n**预期**：\n\n- 一个状态预期\n"
    first = parse_case_markdown(text, doc_external_id="d.md")
    second = parse_case_markdown(text, doc_external_id="d.md")
    assert first.cases[0].urn == second.cases[0].urn
    assert first.cases[0].urn.startswith("urn:testpilot:fs-cases:test_case:d.md#TC-S1")


def test_expectation_marker_with_parenthetical_note_still_collected():
    # 回归（真档 TC-A7 实证）：**预期**（回归说明）： 夹注形态曾让整段预期丢失、
    # 用例被误标 oracle_missing
    text = (
        "## M\n\n### TC-A7 无头冷启动（FR-2b）P0\n\n"
        "1. 杀进程后锁屏等响\n\n"
        "**预期**（2026-09-18 快速路径已真机通过，gate 版待回归）：\n"
        "- 日志出现 `HEADLESS recovery`\n"
        "- 主观无空档，需要人工听感\n"
    )
    case = parse_case_markdown(text, doc_external_id="d.md").cases[0]
    assert case.oracle_missing is False
    assert len(case.oracles) == 2
    assert any(o.type is OracleType.LOG_ASSERTION for o in case.oracles)
    assert any(o.type is OracleType.MANUAL for o in case.oracles)


# ── L3-4 红证：classify_oracle 词表扩词（W2 盲评回流，原句取自冻结 W2 运行） ──

def test_l3_4_w2_ac_sentences_no_longer_fall_back_to_manual():
    # 三句在 L3-4 之前全部兜底 manual，把 W2-AC-3/AC-6 整个用例单元打成探索性
    assert classify_oracle(
        "（FR-1、FR-5）新建起床闹钟默认数学题简单 3 道；扫码任务未注册码时不可选，"
        "并提示先注册。"
    ) is OracleType.UI_ASSERTION  # 「提示」是可自动化的界面信号
    assert classify_oracle(
        "（FR-5、FR-6）数学中等 5 道：答错可重答本题且不扣已对题，同一题连错 3 次"
        "换同难度新题；全对即完成。"
    ) is OracleType.STATE_ASSERTION  # 答错/重答/难度 表达可断言的判题状态
    assert classify_oracle(
        "（FR-3、FR-3a）任务中途杀掉 App 再进接管页，数学已对题与摇晃次数恢复；"
        "中途不能把扫码切成数学题（FR-10 降级除外）。"
    ) is OracleType.STATE_ASSERTION  # 次数/恢复 是可断言的数据事实
    # 防退化：原本就能认的状态句不得因扩词改判
    assert classify_oracle(
        "（FR-7、NFR-1）数学题目与答案只在本次响铃会话内，完成后不留记录；"
        "断网也能出题、判题、完成。"
    ) is OracleType.STATE_ASSERTION


def test_l3_4_manual_cues_still_outrank_keyword_expansion():
    # 保守优先级不变：人工/主观信号必须压过 UI/STATE 关键词，不能因扩词抢走 manual
    assert classify_oracle("出现提示后由人工亲自确认手感") is OracleType.MANUAL
    assert classify_oracle("主观判断难度是否合理") is OracleType.MANUAL
    # 无任何信号的普通预期仍然兜底 manual，不硬判成可自动断言类型
    assert classify_oracle("一条看不出信号的普通预期") is OracleType.MANUAL


def test_indented_numbered_sublist_under_expectation_is_not_a_step():
    # 对齐 V1 TC-A1 真实形态：预期子弹下缩进的 1./2. 平台约束
    text = (
        "## M\n\n### TC-A1 标题 P0\n\n"
        "1. 顶格步骤一\n\n**预期**：\n\n"
        "- 一条日志预期：`RINGER armed`\n"
        "  1. 缩进子项一\n"
        "  2. 缩进子项二\n"
        "- 另一条状态预期\n"
    )
    case = parse_case_markdown(text, doc_external_id="d.md").cases[0]
    assert len(case.steps) == 1
    assert len(case.oracles) == 2
    assert "缩进子项一" in case.oracles[0].expectation
