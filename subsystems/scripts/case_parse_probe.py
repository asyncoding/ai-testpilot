#!/usr/bin/env python3
"""FR-CON-003 AC1 真档验证探针：用例 Markdown 全量解析（S2.1）。

对真实用例文档跑 core 零依赖解析器，报告：
  - 解析用例数、按模块/优先级/生命周期分布、oracle 四类分布；
  - oracle_missing 清单（FR-CON-003：缺预期不丢弃、显式标记）；
  - 死信清单（AC2：损坏条目不阻断整批）；
  - 反假绿对账：用独立正则数原文里的 ``### TC`` 标题与表格 ``| TC-`` 行，
    与解析结果的 external_id 集合比对，漏解析直接失败。

不触网、不调模型、不写库；报告落 out/eval/case-parse-probe.{json,md}（gitignore）。
语料仓路径必须由参数给出（机器相关路径不写死进仓）：

  .venv/bin/python scripts/case_parse_probe.py <用例.md 或目录>
  .venv/bin/python scripts/case_parse_probe.py <dir> --glob '**/*.md'
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

SUBSYSTEMS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SUBSYSTEMS / "packages" / "core" / "src"))

from testpilot_core.testcase import parse_case_markdown  # noqa: E402

_REPORT_DIR = SUBSYSTEMS / "out" / "eval"
_TC_HEADING = re.compile(r"^###\s+(?:~~)?(TC-[0-9A-Za-z]+)", re.MULTILINE)
_TC_TABLE_ROW = re.compile(r"^\|\s*(?:~~)?(TC-[0-9A-Za-z]+)\b", re.MULTILINE)


def _raw_tc_ids_outside_fences(text: str) -> set[str]:
    """独立计数（不经过解析器）：剔除代码围栏行，防围栏假样本污染对账。"""
    ids: set[str] = set()
    inside = False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            inside = not inside
            continue
        if inside:
            continue
        m = _TC_HEADING.match(line)
        if m:
            ids.add(m.group(1))
        m = _TC_TABLE_ROW.match(line)
        if m:
            ids.add(m.group(1))
    return ids


def probe_file(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    external_id = path.name
    result = parse_case_markdown(text, doc_external_id=external_id)

    raw_ids = _raw_tc_ids_outside_fences(text)
    parsed_ids = {c.external_id for c in result.cases}
    # 进了死信的损坏条目（如列数不符的表格行）是显式隔离，不算漏解析
    dead_hint_ids = {d.case_id_hint for d in result.dead_letters if d.case_id_hint}
    missing = sorted(raw_ids - parsed_ids - dead_hint_ids)
    extra = sorted(parsed_ids - raw_ids)

    modules = Counter(c.module or "(无模块)" for c in result.cases)
    priorities = Counter(c.priority or "(无优先级)" for c in result.cases)
    lifecycles = Counter(c.lifecycle for c in result.cases)
    oracle_types = Counter(o.type for c in result.cases for o in c.oracles)

    return {
        "file": str(path),
        "raw_tc_markers": len(raw_ids),
        "parsed_cases": len(result.cases),
        "missing_from_parser": missing,
        "extra_without_marker": extra,
        "modules": dict(modules),
        "priorities": dict(priorities),
        "lifecycles": dict(lifecycles),
        "oracle_types": dict(oracle_types),
        "oracle_missing": [
            {"id": c.external_id, "line": c.source_line, "title": c.title}
            for c in result.cases if c.oracle_missing
        ],
        "cancelled": [c.external_id for c in result.cases if c.lifecycle == "cancelled"],
        "dead_letters": [d.model_dump() for d in result.dead_letters],
        "cases": [
            {
                "id": c.external_id, "module": c.module, "priority": c.priority,
                "lifecycle": c.lifecycle, "line": c.source_line,
                "steps": len(c.steps), "oracles": len(c.oracles),
                "oracle_missing": c.oracle_missing,
            }
            for c in result.cases
        ],
    }


def render_markdown(reports: list[dict]) -> str:
    lines = ["# FR-CON-003 用例文档解析探针（S2.1 AC1）", ""]
    total_raw = total_parsed = total_dead = 0
    for r in reports:
        total_raw += r["raw_tc_markers"]
        total_parsed += r["parsed_cases"]
        total_dead += len(r["dead_letters"])
        lines += [
            f"## {Path(r['file']).name}",
            "",
            f"- 原文 TC 标记（独立正则计数）：{r['raw_tc_markers']}；"
            f"解析用例：{r['parsed_cases']}；漏解析：{len(r['missing_from_parser'])}；"
            f"无标记多出：{len(r['extra_without_marker'])}",
            f"- 生命周期：{r['lifecycles']}；优先级：{r['priorities']}",
            f"- oracle 类型分布：{r['oracle_types']}",
            f"- oracle_missing（{len(r['oracle_missing'])}）："
            + (", ".join(f"{x['id']}@L{x['line']}" for x in r["oracle_missing"]) or "无"),
            f"- 取消态（{len(r['cancelled'])}）：{', '.join(r['cancelled']) or '无'}",
            f"- 死信（{len(r['dead_letters'])}）：",
        ]
        for d in r["dead_letters"]:
            lines.append(f"  - L{d['line']} {d['case_id_hint']}: {d['error']} | {d['snippet']}")
        if r["missing_from_parser"]:
            lines.append(f"- ❌ 漏解析：{r['missing_from_parser']}")
        lines.append("")
    lines += [
        "## 汇总", "",
        f"- 文件 {len(reports)} 个；原文标记 {total_raw}；解析 {total_parsed}；死信 {total_dead}",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="用例 Markdown 全量解析探针（FR-CON-003 AC1）")
    parser.add_argument("target", help="用例 .md 文件或目录（语料仓路径，不写死进仓）")
    parser.add_argument("--glob", default="**/*.md", help="目录模式，默认 **/*.md")
    args = parser.parse_args()

    target = Path(args.target).expanduser()
    if not target.exists():
        print(f"目标不存在: {target}", file=sys.stderr)
        return 2
    files = sorted(target.glob(args.glob)) if target.is_dir() else [target]
    files = [p for p in files if p.is_file()]
    if not files:
        print(f"未匹配到 Markdown: {target}", file=sys.stderr)
        return 2

    reports = [probe_file(p) for p in files]
    _REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (_REPORT_DIR / "case-parse-probe.json").write_text(
        json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8")
    (_REPORT_DIR / "case-parse-probe.md").write_text(
        render_markdown(reports), encoding="utf-8")

    failed = False
    for r in reports:
        print(
            f"{Path(r['file']).name}: 标记 {r['raw_tc_markers']} → 解析 {r['parsed_cases']}，"
            f"缺预期 {len(r['oracle_missing'])}，取消 {len(r['cancelled'])}，"
            f"死信 {len(r['dead_letters'])}，漏解析 {len(r['missing_from_parser'])}"
        )
        if r["missing_from_parser"] or r["extra_without_marker"] or r["dead_letters"]:
            failed = True
    print(f"报告：{_REPORT_DIR / 'case-parse-probe.md'}")
    if failed:
        print("❌ 探针发现漏解析/无标记多出/死信，详见报告", file=sys.stderr)
        return 1
    print("✅ case_parse_probe 全部通过：原文 TC 标记与解析用例一一对应，零死信")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
