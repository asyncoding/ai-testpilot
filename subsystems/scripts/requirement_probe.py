#!/usr/bin/env python3
"""FR-TD-001 AC1 真档验证探针：需求 Markdown 骨架抽取（S2.2）。

对真实需求文档跑 core 零依赖抽取器，报告：
  - 功能点/规则/验收标准/模块盘点，缺口清单（缺 AC/缺优先级/取消/损坏行）；
  - 骨架经 pydantic 重新校验，验证结构合法率（FR-TD-001 AC2）；
  - 反假绿对账：用独立正则数原文表格里的 FR-/NFR- ID（剔除代码围栏），
    与解析结果 + 死信 hint 对账，漏解析直接失败；
  - 可选 --link-model：对本机 Ollama 发结构化链接请求，core 白名单校验
    （未知 FR ID/越界序号整包拒绝）；只做内存验证，不写任何库（hermetic）。

不触网（除可选模型链接外）、不写业务库；报告落 out/eval/requirement-probe.{json,md}
（gitignore）。语料仓路径必须由参数给出（机器相关路径不写死进仓）：

  .venv/bin/python scripts/requirement_probe.py <需求.md 或目录>
  .venv/bin/python scripts/requirement_probe.py <dir> --glob '**/*.md' --link-model
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from collections import Counter
from pathlib import Path

SUBSYSTEMS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SUBSYSTEMS / "packages" / "core" / "src"))
sys.path.insert(0, str(SUBSYSTEMS / "packages" / "adapters" / "src"))

from testpilot_core.models import RequirementSkeleton  # noqa: E402
from testpilot_core.requirement import (  # noqa: E402
    SkeletonValidationError,
    apply_linking,
    build_linking_messages,
    linking_json_schema,
    parse_requirement_markdown,
)

_REPORT_DIR = SUBSYSTEMS / "out" / "eval"
_FR_ROW_RE = re.compile(r"^\|\s*(?:~~)?(FR-\d+[a-z]?)(?:~~)?\b")
_NFR_ROW_RE = re.compile(r"^\|\s*(NFR-\d+)\b")
_ID_HINT_RE = re.compile(r"(?:FR|NFR)-\d+[a-z]?")


def _raw_ids_outside_fences(text: str, pattern: re.Pattern[str]) -> set[str]:
    """独立计数（不经过解析器）：剔除代码围栏行，防围栏假样本污染对账。"""
    ids: set[str] = set()
    inside = False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            inside = not inside
            continue
        if inside:
            continue
        m = pattern.match(line)
        if m:
            ids.add(m.group(1))
    return ids


def _reconcile(raw_ids: set[str], parsed_ids: set[str], dead_hints: set[str]) -> dict:
    return {
        "raw_markers": len(raw_ids),
        "parsed": len(parsed_ids),
        "missing_from_parser": sorted(raw_ids - parsed_ids - dead_hints),
        "extra_without_marker": sorted(parsed_ids - raw_ids),
    }


async def _try_model_linking(result, base_url: str) -> dict:
    """真模型 hermetic 验收：不落库，白名单失败按降级记录。"""
    from testpilot_adapters.ollama_model import OllamaModelGateway

    gateway = OllamaModelGateway(base_url=base_url)
    try:
        envelope = await gateway.structured(
            build_linking_messages(result),
            linking_json_schema(),
            key=f"requirement-probe:{result.external_id}",
            required_nonempty=("links",),
        )
    finally:
        await gateway.aclose()
    if envelope.get("degraded") or not envelope.get("data"):
        return {"status": "degraded", "reason": "模型信封降级或空 data", "model": envelope.get("model")}
    try:
        linked = apply_linking(result, envelope["data"])
    except SkeletonValidationError as exc:
        return {"status": "rejected", "reason": str(exc), "model": envelope.get("model")}
    covered = {fid for ac in linked.acceptance for fid in ac.linked_fr_ids}
    return {
        "status": "accepted",
        "model": envelope.get("model"),
        "linked_fr_ids": sorted(covered),
        "ac_missing_after_linking": [
            f.fr_id for f in linked.active_features if f.fr_id not in covered
        ],
    }


def probe_file(path: Path, *, link_model: bool = False, base_url: str = "") -> dict:
    text = path.read_text(encoding="utf-8")
    external_id = path.name
    result = parse_requirement_markdown(text, doc_external_id=external_id)

    raw_fr = _raw_ids_outside_fences(text, _FR_ROW_RE)
    raw_nfr = _raw_ids_outside_fences(text, _NFR_ROW_RE)
    dead_hints = {
        hint for d in result.dead_letters
        for hint in _ID_HINT_RE.findall(d.subject_hint or "")
    }
    fr_recon = _reconcile(raw_fr, {f.fr_id for f in result.features}, dead_hints)
    nfr_recon = _reconcile(raw_nfr, {r.rule_id for r in result.rules}, dead_hints)

    # AC2：骨架必须能通过 pydantic schema 重新校验（结构合法率 100%）
    skeleton = result.to_skeleton()
    RequirementSkeleton.model_validate(skeleton.model_dump())

    gap_kinds = Counter(g.kind for g in result.gaps)
    report = {
        "file": str(path),
        "fr_reconciliation": fr_recon,
        "nfr_reconciliation": nfr_recon,
        "modules": result.modules,
        "features_active": len(result.active_features),
        "features_cancelled": sum(
            1 for f in result.features if f.lifecycle == "cancelled"),
        "rules": len(result.rules),
        "acceptance_criteria": len(result.acceptance),
        "ac_present_false": sum(
            1 for c in skeleton.acceptance_criteria if not c.present),
        "gap_kinds": dict(gap_kinds),
        "gaps": [g.model_dump() for g in result.gaps],
        "dead_letters": [d.model_dump() for d in result.dead_letters],
        "linking_source": result.linking_source,
    }
    if link_model:
        report["model_linking"] = asyncio.run(
            _try_model_linking(result, base_url or "http://localhost:11434"))
    return report


def render_markdown(reports: list[dict]) -> str:
    lines = ["# FR-TD-001 需求骨架抽取探针（S2.2 AC1/AC2）", ""]
    for r in reports:
        fr, nfr = r["fr_reconciliation"], r["nfr_reconciliation"]
        lines += [
            f"## {Path(r['file']).name}", "",
            f"- FR 对账：原文标记 {fr['raw_markers']} → 解析 {fr['parsed']}；"
            f"漏解析 {len(fr['missing_from_parser'])}；无标记多出 {len(fr['extra_without_marker'])}",
            f"- NFR 对账：原文标记 {nfr['raw_markers']} → 解析 {nfr['parsed']}；"
            f"漏解析 {len(nfr['missing_from_parser'])}；无标记多出 {len(nfr['extra_without_marker'])}",
            f"- 模块：{r['modules']}",
            f"- 功能点：活跃 {r['features_active']} / 取消 {r['features_cancelled']}；"
            f"规则 {r['rules']}；验收标准 {r['acceptance_criteria']}（"
            f"显式标缺 present=False {r['ac_present_false']} 条）",
            f"- 缺口分类：{r['gap_kinds']}；链接来源：{r['linking_source']}",
            "- 缺 AC 功能点：" + (", ".join(
                g["subject"] for g in r["gaps"]
                if g["kind"] == "ac_missing_for_feature") or "无"),
            f"- 死信（{len(r['dead_letters'])}）：",
        ]
        for d in r["dead_letters"]:
            lines.append(f"  - L{d['line']} {d['subject_hint']}: {d['error']}")
        if "model_linking" in r:
            m = r["model_linking"]
            lines.append(f"- 模型链接验收：{m['status']}（{m.get('model', '')}）{m.get('reason', '')}")
            if m["status"] == "accepted":
                lines.append(
                    f"  - 覆盖 {m['linked_fr_ids']}；链接后仍缺 {m['ac_missing_after_linking']}")
        lines.append("")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="需求 Markdown 骨架抽取探针（FR-TD-001）")
    parser.add_argument("target", help="需求 .md 文件或目录（语料仓路径，不写死进仓）")
    parser.add_argument("--glob", default="**/*.md", help="目录模式，默认 **/*.md")
    parser.add_argument("--link-model", action="store_true",
                        help="附加本机 Ollama 链接验收（hermetic，不写库）")
    parser.add_argument("--base-url", default="", help="Ollama 地址，默认 http://localhost:11434")
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

    reports = [probe_file(p, link_model=args.link_model, base_url=args.base_url) for p in files]
    _REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (_REPORT_DIR / "requirement-probe.json").write_text(
        json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8")
    (_REPORT_DIR / "requirement-probe.md").write_text(
        render_markdown(reports), encoding="utf-8")

    failed = False
    for r in reports:
        fr, nfr = r["fr_reconciliation"], r["nfr_reconciliation"]
        print(
            f"{Path(r['file']).name}: FR {fr['raw_markers']}→{fr['parsed']}，"
            f"NFR {nfr['raw_markers']}→{nfr['parsed']}，缺口 {r['gap_kinds']}，"
            f"死信 {len(r['dead_letters'])}，标缺 {r['ac_present_false']}"
        )
        if fr["missing_from_parser"] or fr["extra_without_marker"] \
                or nfr["missing_from_parser"] or nfr["extra_without_marker"] \
                or r["dead_letters"]:
            failed = True
        if r.get("model_linking", {}).get("status") == "rejected":
            failed = True
    print(f"报告：{_REPORT_DIR / 'requirement-probe.md'}")
    if failed:
        print("❌ 探针发现漏解析/多出/死信/模型链接被拒，详见报告", file=sys.stderr)
        return 1
    print("✅ requirement_probe 全部通过：原文 FR/NFR 标记与解析一一对应，骨架 schema 合法")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
