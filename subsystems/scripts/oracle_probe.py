#!/usr/bin/env python3
"""FR-TD-003 真档验证探针：oracle 四源挖掘（S2.4）。

对真实用例文档跑 core 零依赖挖掘链，报告：
  - 缺预期用例独立正则对账（不经过解析器）：原文无 ``**预期**`` 段的叙事块 vs
    解析器 oracle_missing 标记，双向比对防假绿（TC-A4/TC-N2 是天然样本）；
  - 确定性三源盘点（需求 AC / 自身断言；事故义务句与相似用例源依赖入库语料，
    由 worker 集成测试与 offline 库冒烟覆盖，本探针不接检索适配器）；
  - AC3 守卫：挖完仍无 ui/log/state 可执行 oracle 的用例必须 exploratory=True；
  - 可选 --mine-model：仅对 oracle_missing 用例发本机 Ollama 结构化请求，
    core 白名单逐行校验（bad_type/unknown_source/empty_text/bad_shape 进
    rejected 台账；整包形状错才算探针失败，行级拒入是 fail-closed 的正确行为）。

全程 hermetic：不触业务库、不写 store，仅报告落
out/eval/oracle-probe.{json,md}（gitignore）。语料仓路径必须由参数给出
（机器相关路径不写死进仓）：

  .venv/bin/python scripts/oracle_probe.py <用例.md 或目录> \
      --requirements <需求.md> [--mine-model]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

SUBSYSTEMS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SUBSYSTEMS / "packages" / "core" / "src"))
sys.path.insert(0, str(SUBSYSTEMS / "packages" / "adapters" / "src"))

from testpilot_core.oracle_mining import (  # noqa: E402
    OracleMiningValidationError,
    apply_model_oracles,
    build_oracle_messages,
    mine_deterministic,
    oracle_json_schema,
)
from testpilot_core.requirement import parse_requirement_markdown  # noqa: E402
from testpilot_core.testcase import parse_case_markdown  # noqa: E402

_REPORT_DIR = SUBSYSTEMS / "out" / "eval"
_HEADING_RE = re.compile(r"^###\s+(~~)?(TC-[A-Za-z0-9]+)")
_EXPECT_MARKER_RE = re.compile(r"^\*\*预期\*\*[^：:]*[：:]")
_CANCELLED_RE = re.compile(r"^###\s+~~")
# 表格体取消两种形态：① ID 带删除线 | ~~TC-B6~~ |；② ID 无删除线但末列
# 生命周期单元格标「取消」（V1 真档 TC-B6 即此形态，与需求 FR-7 同型，S2.3 踩过）
_TABLE_CANCEL_RE = re.compile(r"^\|\s*~~(TC-[A-Za-z0-9]+)~~")
_TABLE_LIFECYCLE_CANCEL_RE = re.compile(
    r"^\|\s*(TC-[A-Za-z0-9]+)\s*\|.*\|\s*取消\s*\|?\s*$")


def _raw_missing_outside_fences(text: str) -> tuple[set[str], set[str]]:
    """独立计数（不经过解析器）：返回 (缺预期叙事块 ID, 取消态块 ID)。

    只认叙事体 `### TC-` 块；表格体用例没有独立预期段概念，不参与对账。
    """
    missing: set[str] = set()
    cancelled: set[str] = set()
    inside = False
    current: str | None = None
    current_cancelled = False
    has_expect = False

    def close() -> None:
        if current is not None and not current_cancelled and not has_expect:
            missing.add(current)
        if current is not None and current_cancelled:
            cancelled.add(current)

    for line in text.splitlines():
        if line.strip().startswith("```"):
            inside = not inside
            continue
        if inside:
            continue
        table_cancel = _TABLE_CANCEL_RE.match(line)
        if table_cancel:
            cancelled.add(table_cancel.group(1))
            continue
        lifecycle_cancel = _TABLE_LIFECYCLE_CANCEL_RE.match(line)
        if lifecycle_cancel:
            cancelled.add(lifecycle_cancel.group(1))
            continue
        m = _HEADING_RE.match(line)
        if m:
            close()
            current_cancelled = bool(m.group(1)) or bool(_CANCELLED_RE.match(line))
            current = m.group(2)
            has_expect = False
            continue
        if current is not None and _EXPECT_MARKER_RE.match(line.strip()):
            has_expect = True
    close()
    return missing, cancelled


async def _try_model(case, spine_result, skeleton, base_url: str) -> dict:
    """真模型 hermetic 验收：不落库；行级拒入合法，整包形状错才算 rejected。"""
    from testpilot_adapters.ollama_model import OllamaModelGateway
    from testpilot_core.errors import AdapterUnavailableError

    gateway = OllamaModelGateway(base_url=base_url or "http://localhost:11434")
    try:
        envelope = await gateway.structured(
            build_oracle_messages(case, skeleton, [], spine_result.mined),
            oracle_json_schema(),
            key=f"oracle-probe:{case.external_id}",
            required_nonempty=("oracles",),
        )
    except AdapterUnavailableError as exc:
        return {"status": "degraded", "reason": str(exc), "model": "",
                "result": spine_result}
    finally:
        await gateway.aclose()

    if envelope.get("degraded") or not envelope.get("data"):
        return {"status": "degraded", "reason": "模型信封降级或空 data",
                "model": envelope.get("model", ""), "result": spine_result}
    try:
        result = apply_model_oracles(
            case, spine_result, envelope["data"], skeleton, [], [])
    except OracleMiningValidationError as exc:
        return {"status": "rejected", "reason": str(exc),
                "model": envelope.get("model", ""), "result": spine_result}
    return {"status": "accepted", "model": envelope.get("model", ""),
            "result": result}


def probe_file(path: Path, *, skeleton, mine_model: bool, base_url: str,
               external_id: str | None) -> dict:
    text = path.read_text(encoding="utf-8")
    doc_id = external_id or path.name
    parsed = parse_case_markdown(text, doc_external_id=doc_id)

    raw_missing, raw_cancelled = _raw_missing_outside_fences(text)
    parser_missing = {
        c.external_id for c in parsed.cases if c.oracle_missing
    }
    parser_cancelled = {
        c.external_id for c in parsed.cases if c.lifecycle == "cancelled"
    }
    reconciliation = {
        "raw_missing_cases": sorted(raw_missing),
        "parser_missing_cases": sorted(parser_missing),
        "missing_flag_disagreement": sorted(raw_missing ^ parser_missing),
        "raw_cancelled_cases": sorted(raw_cancelled),
        "cancelled_flag_disagreement": sorted(raw_cancelled ^ parser_cancelled),
        "parse_dead_letters": len(parsed.dead_letters),
    }

    case_reports = []
    for case in parsed.cases:
        if case.lifecycle == "cancelled":
            continue
        result = mine_deterministic(case, skeleton, [])
        model_out = None
        if mine_model and case.oracle_missing:
            model_out = asyncio.run(_try_model(case, result, skeleton, base_url))
            result = model_out.pop("result")
        ac3_violation = (
            not result.exploratory and not result.executable_oracles
        ) or (result.exploratory and result.executable_oracles)
        case_reports.append({
            "external_id": case.external_id,
            "title": case.title,
            "oracle_missing": case.oracle_missing,
            "exploratory": result.exploratory,
            "exploratory_reasons": result.exploratory_reasons,
            "ac3_violation": ac3_violation,
            "mined_total": len(result.mined),
            "executable_total": len(result.executable_oracles),
            "by_source_kind": _count(result.mined, "source_kind"),
            "by_type": _count(result.mined, "type"),
            "model": model_out,
            "model_rejected": [
                {"reason": r.reason, "detail": r.detail} for r in result.rejected
            ],
            "hallucination_rate": result.hallucination_rate,
            "mined": [o.model_dump() for o in result.mined],
        })

    return {
        "file": str(path),
        "external_id": doc_id,
        "reconciliation": reconciliation,
        "cases": case_reports,
    }


def _count(items, attr: str) -> dict:
    out: dict[str, int] = {}
    for item in items:
        value = getattr(item, attr)
        value = value.value if hasattr(value, "value") else value
        out[value] = out.get(value, 0) + 1
    return dict(sorted(out.items()))


def render_markdown(reports: list[dict]) -> str:
    lines = ["# FR-TD-003 oracle 四源挖掘探针（S2.4）", ""]
    for r in reports:
        recon = r["reconciliation"]
        lines += [
            f"## {Path(r['file']).name}（{r['external_id']}）", "",
            f"- 缺预期对账：原文 {recon['raw_missing_cases']} vs 解析器 "
            f"{recon['parser_missing_cases']}；分歧 {recon['missing_flag_disagreement']}",
            f"- 取消态对账：原文 {recon['raw_cancelled_cases']}；"
            f"分歧 {recon['cancelled_flag_disagreement']}；解析死信 {recon['parse_dead_letters']}",
        ]
        for c in r["cases"]:
            lines.append(
                f"- {c['external_id']}：missing={c['oracle_missing']} "
                f"exploratory={c['exploratory']}（{c['exploratory_reasons']}）；"
                f"挖 {c['mined_total']} 条/可执行 {c['executable_total']}；"
                f"来源 {c['by_source_kind']}；类型 {c['by_type']}"
                + (f"；AC3 违规！" if c["ac3_violation"] else "")
            )
            if c["model"]:
                m = c["model"]
                lines.append(
                    f"  - 模型补 oracle：{m['status']}（{m.get('model', '')}）；"
                    f"行级拒入 {len(c['model_rejected'])}，"
                    f"幻觉率 {c['hallucination_rate']}"
                )
                for rejected in c["model_rejected"]:
                    lines.append(f"    - rejected {rejected['reason']}: {rejected['detail']}")
        lines.append("")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="oracle 四源挖掘真档探针（FR-TD-003）")
    parser.add_argument("target", help="用例 .md 文件或目录（语料仓路径，不写死进仓）")
    parser.add_argument("--glob", default="**/*.md", help="目录模式，默认 **/*.md")
    parser.add_argument("--requirements", default="",
                        help="需求 .md（可选）：提供 AC 源与模型白名单 URN")
    parser.add_argument("--mine-model", action="store_true",
                        help="对 oracle_missing 用例附加本机 Ollama 验收（hermetic）")
    parser.add_argument("--base-url", default="", help="Ollama 地址，默认本机 11434")
    parser.add_argument("--external-id", default="",
                        help="单文件模式下覆盖 external_id（对齐 URN 归属）")
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
    if args.external_id and len(files) != 1:
        print("--external-id 仅支持单文件模式", file=sys.stderr)
        return 2

    skeleton = None
    if args.requirements:
        req_path = Path(args.requirements).expanduser()
        skeleton = parse_requirement_markdown(
            req_path.read_text(encoding="utf-8"),
            doc_external_id=req_path.name,
        )
        print(f"需求骨架已载入：{req_path.name}（{len(skeleton.active_features)} 活跃 FR，"
              f"{len(skeleton.acceptance)} AC）")

    reports = [
        probe_file(p, skeleton=skeleton, mine_model=args.mine_model,
                   base_url=args.base_url,
                   external_id=args.external_id or None)
        for p in files
    ]
    _REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (_REPORT_DIR / "oracle-probe.json").write_text(
        json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8")
    (_REPORT_DIR / "oracle-probe.md").write_text(
        render_markdown(reports), encoding="utf-8")

    failed = False
    for r in reports:
        recon = r["reconciliation"]
        print(
            f"{Path(r['file']).name}: 用例 {len(r['cases'])} 条，缺预期对账 "
            f"{len(recon['parser_missing_cases'])} 个，分歧 "
            f"{len(recon['missing_flag_disagreement'])}"
        )
        if recon["missing_flag_disagreement"] or recon["cancelled_flag_disagreement"]:
            failed = True
        for c in r["cases"]:
            if c["ac3_violation"]:
                failed = True
            if (c.get("model") or {}).get("status") == "rejected":
                failed = True
    print(f"报告：{_REPORT_DIR / 'oracle-probe.md'}")
    if failed:
        print("❌ 探针发现对账分歧/AC3 违规/模型整包被拒，详见报告", file=sys.stderr)
        return 1
    print("✅ oracle_probe 通过：缺预期对账一致，AC3 守卫成立，结构合法")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
