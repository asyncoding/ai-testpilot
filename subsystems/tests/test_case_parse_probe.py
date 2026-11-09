"""case_parse_probe 探针的纯函数测试（不依赖语料仓，真档验证由手工跑探针完成）。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

SUBSYSTEMS = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "case_parse_probe", SUBSYSTEMS / "scripts" / "case_parse_probe.py")
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)


def test_raw_marker_count_skips_code_fences():
    text = (
        "### TC-A1 真用例 P0\n\n1. 一步\n\n"
        "```markdown\n### TC-FAKE 围栏假标题\n| TC-FAKE | x |\n```\n\n"
        "| ID | 步骤要点 | 预期 | 优先级 |\n|---|---|---|---|\n"
        "| TC-B1 | 表格真行 | 预期 | P0 |\n"
    )
    ids = probe._raw_tc_ids_outside_fences(text)
    assert ids == {"TC-A1", "TC-B1"}  # 围栏内 TC-FAKE 不计入独立对账


def test_probe_file_flags_missing_and_dead_hints(tmp_path):
    # 含一个损坏行（死信带 hint）与一个围栏假样本：都不应算漏解析
    doc = tmp_path / "c.md"
    doc.write_text(
        "## M\n\n### TC-A1 标题 P0\n\n1. 一步\n\n**预期**：\n\n- 一个预期\n\n"
        "| ID | 步骤要点 | 预期 | 优先级 |\n|---|---|---|---|\n"
        "| TC-B1 | 好行 | 好预期 | P0 |\n"
        "| TC-B2 | 缺列 | P1 |\n"
        "```\n### TC-FAKE\n```\n",
        encoding="utf-8",
    )
    report = probe.probe_file(doc)
    assert report["raw_tc_markers"] == 3  # A1/B1/B2，围栏内 FAKE 不计
    assert report["parsed_cases"] == 2   # B2 列数损坏进死信
    assert report["missing_from_parser"] == []
    assert report["extra_without_marker"] == []
    assert len(report["dead_letters"]) == 1
    assert report["dead_letters"][0]["case_id_hint"] == "TC-B2"


def test_render_markdown_contains_summary(tmp_path):
    doc = tmp_path / "c.md"
    doc.write_text("## M\n\n### TC-A1 标题 P0\n\n1. 一步\n", encoding="utf-8")
    md = probe.render_markdown([probe.probe_file(doc)])
    assert "FR-CON-003" in md and "原文标记 1" in md and "TC-A1" in md
