"""requirement_probe 探针的纯函数测试（不依赖语料仓，真档验证手工跑探针）。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

SUBSYSTEMS = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "requirement_probe", SUBSYSTEMS / "scripts" / "requirement_probe.py")
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)


def test_raw_marker_counts_skip_code_fences():
    text = (
        "## 1. 功能需求（FR）\n\n"
        "| ID | 需求 | 优先级 |\n|---|---|---|\n"
        "| FR-1 | 真需求 | P0 |\n"
        "| NFR-1 | 真规则在 NFR 表也会被 FR 正则跳过 |\n\n"
        "```markdown\n| FR-FAKE | 围栏假需求 | P0 |\n```\n"
    )
    fr_ids = probe._raw_ids_outside_fences(text, probe._FR_ROW_RE)
    nfr_ids = probe._raw_ids_outside_fences(text, probe._NFR_ROW_RE)
    assert fr_ids == {"FR-1"}       # 围栏假样本不计入独立对账
    # 原始计数刻意与节无关：行首 NFR- 就计数；它在解析侧进死信 hint，对账仍为 0 漏解析
    assert nfr_ids == {"NFR-1"}


def test_probe_file_reconciles_dead_letter_hint_and_validates_skeleton(tmp_path):
    doc = tmp_path / "r.md"
    doc.write_text(
        "## 1. 功能需求（FR）\n\n"
        "| ID | 需求 | 优先级 |\n|---|---|---|\n"
        "| FR-1 | 好行 | P0 |\n"
        "| FR-2 | 缺列 |\n"
        "```\n| FR-FAKE | 围栏 |\n```\n",
        encoding="utf-8",
    )
    report = probe.probe_file(doc)
    fr = report["fr_reconciliation"]
    # 原文 2 个 FR 标记（围栏不计），解析 1 个，FR-2 进死信 hint，漏解析为 0
    assert fr["raw_markers"] == 2 and fr["parsed"] == 1
    assert fr["missing_from_parser"] == [] and fr["extra_without_marker"] == []
    assert len(report["dead_letters"]) == 1
    assert report["dead_letters"][0]["subject_hint"] == "FR-2"
    assert report["features_active"] == 1


def test_render_markdown_contains_summary(tmp_path):
    doc = tmp_path / "r.md"
    doc.write_text(
        "## 1. 功能需求（FR）\n\n"
        "| ID | 需求 | 优先级 |\n|---|---|---|\n| FR-1 | 好行 | P0 |\n",
        encoding="utf-8",
    )
    md = probe.render_markdown([probe.probe_file(doc)])
    assert "FR-TD-001" in md and "FR 对账" in md and "FR-1" in md
