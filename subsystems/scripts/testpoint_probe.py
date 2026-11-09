#!/usr/bin/env python3
"""FR-TD-002 真档验证探针：测试点生成 + 专家盲标评测（S2.3）。

对真实需求文档跑 core 零依赖生成链，报告：
  - 确定性脊柱盘点（functional/rule/acceptance、P0/P1 分布），并与原文 FR
    标记做独立正则对账（剔除代码围栏、取消态单列），防假绿；
  - 可选 --gen-model：对本机 Ollama 发结构化补点请求，core 白名单逐行校验
    （未知 fr_id / 白名单外 URN / 脊柱保留 kind 进 rejected 台账）；
  - 可选 --expert：载入**先于 AI 产出封存**的专家盲标清单，算覆盖率/精确率/
    漏点/P0-P1 漏点/幻觉率与四项门禁（docs/06 §3.2 POC 阈值）。

全程 hermetic：不触业务库、不写 store（证据白名单只含需求自身 URN），
仅报告落 out/eval/testpoint-probe.{json,md}（gitignore）。语料仓路径必须由
参数给出（机器相关路径不写死进仓）：

  .venv/bin/python scripts/testpoint_probe.py <需求.md 或目录>
  .venv/bin/python scripts/testpoint_probe.py <dir> --glob '**/*.md' --gen-model
  .venv/bin/python scripts/testpoint_probe.py <需求.md> --expert expert.json
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

from testpilot_core.testpoint import (  # noqa: E402
    TestPointValidationError,
    apply_model_points,
    build_testpoint_messages,
    deterministic_result,
    deterministic_test_points,
    testpoint_json_schema,
)
from testpilot_core.testpoint_eval import (  # noqa: E402
    BlindOrderError,
    ExpertBlindList,
    evaluate_blind,
    seal_expert_manifest,
)
from testpilot_core.requirement import parse_requirement_markdown  # noqa: E402

_REPORT_DIR = SUBSYSTEMS / "out" / "eval"
_FR_ROW_RE = re.compile(r"^\|\s*(?:~~)?(FR-\d+[a-z]?)(?:~~)?\b")
# 注意：\b 不能放在闭合 ~~ 之后——~ 与空格都是非词字符，该处不存在词边界
_FR_CANCELLED_RE = re.compile(r"^\|\s*~~(FR-\d+[a-z]?)~~")
# 行内取消的第二种形态：ID 不带删除线，但生命周期单元格为「取消」
# （真档 V1 的 FR-7 即此形态，S2.2 解析器按取消态处理，不产脊柱点）
_CANCEL_CELL_RE = re.compile(r"\|\s*取消\s*\|")
_ID_HINT_RE = re.compile(r"(?:FR|NFR)-\d+[a-z]?")


def _raw_fr_outside_fences(text: str) -> tuple[set[str], set[str]]:
    """独立计数（不经过解析器）：返回 (活跃 FR 标记, 取消态 FR 标记)。"""
    active: set[str] = set()
    cancelled: set[str] = set()
    inside = False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            inside = not inside
            continue
        if inside:
            continue
        strike = _FR_CANCELLED_RE.match(line)
        if strike:
            cancelled.add(strike.group(1))
            continue
        m = _FR_ROW_RE.match(line)
        if m:
            # ID 无删除线但生命周期单元格标「取消」：与解析器取消态口径对齐
            if _CANCEL_CELL_RE.search(line):
                cancelled.add(m.group(1))
            else:
                active.add(m.group(1))
    return active, cancelled


async def _try_model_generation(result, spine, base_url: str) -> dict:
    """真模型 hermetic 验收：不落库；整包形状非法按 rejected 记录。

    返回的 generation 是活对象（hallucination_rate 等 property 不进 model_dump，
    必须保留对象由调用方读属性）。
    """
    from testpilot_adapters.ollama_model import OllamaModelGateway
    from testpilot_core.errors import AdapterUnavailableError

    gateway = OllamaModelGateway(base_url=base_url or "http://localhost:11434")
    try:
        envelope = await gateway.structured(
            build_testpoint_messages(result, [], spine),
            testpoint_json_schema(),
            key=f"testpoint-probe:{result.external_id}",
            required_nonempty=("points",),
        )
    except AdapterUnavailableError as exc:
        return {"status": "degraded", "reason": str(exc), "model": "",
                "generation": deterministic_result(result)}
    finally:
        await gateway.aclose()

    if envelope.get("degraded") or not envelope.get("data"):
        return {"status": "degraded", "reason": "模型信封降级或空 data",
                "model": envelope.get("model", ""),
                "generation": deterministic_result(result)}
    try:
        generation = apply_model_points(result, spine, envelope["data"], [])
    except TestPointValidationError as exc:
        return {"status": "rejected", "reason": str(exc),
                "model": envelope.get("model", ""),
                "generation": deterministic_result(result)}
    return {"status": "accepted", "model": envelope.get("model", ""),
            "generation": generation}


def probe_file(
    path: Path, *, gen_model: bool, base_url: str,
    expert: ExpertBlindList | None, external_id: str | None,
) -> dict:
    text = path.read_text(encoding="utf-8")
    doc_id = external_id or path.name
    result = parse_requirement_markdown(text, doc_external_id=doc_id)
    spine = deterministic_test_points(result)

    raw_active, raw_cancelled = _raw_fr_outside_fences(text)
    # 损坏行进解析死信（如缺列 FR-7），对账时与 requirement_probe 同口径剔除
    dead_hints = {
        hint for d in result.dead_letters
        for hint in _ID_HINT_RE.findall(d.subject_hint or "")
    }
    spine_fr = {p.fr_id for p in spine if p.kind == "functional"}
    reconciliation = {
        "raw_active_fr": sorted(raw_active),
        "raw_cancelled_fr": sorted(raw_cancelled),
        "parse_dead_letter_hints": sorted(dead_hints),
        "spine_functional_fr": sorted(spine_fr),
        # 每个活跃 FR 恰好一条 functional 点；取消态/损坏行不得产点
        "missing_spine_point": sorted(raw_active - spine_fr - dead_hints),
        "cancelled_leaked_into_spine": sorted(raw_cancelled & spine_fr),
        "spine_without_marker": sorted(
            spine_fr - raw_active - raw_cancelled - dead_hints),
    }

    kind_counter = Counter(p.kind for p in spine)
    priority_counter = Counter(p.priority for p in spine)
    report: dict = {
        "file": str(path),
        "external_id": doc_id,
        "fr_reconciliation": reconciliation,
        "spine": {
            "total": len(spine),
            "by_kind": dict(kind_counter),
            "p0": priority_counter.get("P0", 0),
            "p1": priority_counter.get("P1", 0),
        },
        "points": [p.model_dump() for p in spine],
    }

    if gen_model:
        model_out = asyncio.run(_try_model_generation(result, spine, base_url))
        generation = model_out.pop("generation")  # 活对象，property 不经过序列化
        report["model_generation"] = model_out
        report["points"] = [p.model_dump() for p in generation.points]
        report["model_summary"] = {
            "generation_source": generation.generation_source,
            "model_points": len(generation.model_points),
            "rejected": [
                {"fr_id": r.fr_id, "reason": r.reason} for r in generation.rejected
            ],
            "deduped": generation.deduped,
            "hallucination_rate": generation.hallucination_rate,
        }
    else:
        generation = deterministic_result(result)

    if expert is not None:
        try:
            blind = evaluate_blind(generation, expert)
        except BlindOrderError as exc:
            report["blind_eval"] = {"status": "skipped", "reason": str(exc)}
        else:
            report["blind_eval"] = {"status": "evaluated", **blind.model_dump(
                exclude={"missed", "p0p1_misses", "hallucinations"})}
            report["blind_eval"]["missed"] = [
                {"expert_id": e.expert_id, "fr_id": e.fr_id, "priority": e.priority}
                for e in blind.missed
            ]
            report["blind_eval"]["p0p1_misses"] = [
                e.expert_id for e in blind.p0p1_misses
            ]
            report["blind_eval"]["hallucination_rows"] = len(blind.hallucinations)
    return report


def render_markdown(reports: list[dict]) -> str:
    lines = ["# FR-TD-002 测试点生成 + 专家盲标探针（S2.3）", ""]
    for r in reports:
        recon = r["fr_reconciliation"]
        spine = r["spine"]
        lines += [
            f"## {Path(r['file']).name}（{r['external_id']}）", "",
            f"- FR 对账：原文活跃 {len(recon['raw_active_fr'])} / 取消 "
            f"{len(recon['raw_cancelled_fr'])}；脊柱 functional "
            f"{len(recon['spine_functional_fr'])}；漏点 {recon['missing_spine_point']}；"
            f"取消态泄漏 {recon['cancelled_leaked_into_spine']}；"
            f"无标记多出 {recon['spine_without_marker']}",
            f"- 脊柱 {spine['total']} 点：{spine['by_kind']}；"
            f"P0={spine['p0']} P1={spine['p1']}",
        ]
        if "model_generation" in r:
            m = r["model_generation"]
            s = r["model_summary"]
            lines.append(
                f"- 模型补点：{m['status']}（{m.get('model', '')}）；"
                f"模型点 {s['model_points']}，拒入 {len(s['rejected'])}，"
                f"去重 {s['deduped']}，幻觉率 {s['hallucination_rate']}"
            )
            for rejected in s["rejected"]:
                lines.append(
                    f"  - rejected {rejected['fr_id']}: {rejected['reason']}")
        if "blind_eval" in r:
            b = r["blind_eval"]
            if b["status"] != "evaluated":
                lines.append(f"- 盲标评测跳过：{b['reason']}")
            else:
                gate = b["gate"]
                lines.append(
                    f"- 盲标（seal={b['expert_seal_hash'][:10]}…）："
                    f"覆盖率 {b['coverage']}（{b['expert_covered']}/"
                    f"{b['expert_must_test']}），精确率 {b['precision']}，"
                    f"幻觉率 {b['hallucination_rate']}，漏点 "
                    f"{[m['expert_id'] for m in b['missed']]}，"
                    f"P0/P1 漏点 {b['p0p1_misses']}"
                )
                lines.append(f"  - 门禁：{gate}，passed={gate['coverage_ok'] and gate['precision_ok'] and gate['p0p1_miss_zero'] and gate['hallucination_ok']}")
        lines.append("")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="测试点生成 + 专家盲标真档探针（FR-TD-002）")
    parser.add_argument("target", help="需求 .md 文件或目录（语料仓路径，不写死进仓）")
    parser.add_argument("--glob", default="**/*.md", help="目录模式，默认 **/*.md")
    parser.add_argument("--gen-model", action="store_true",
                        help="附加本机 Ollama 补点验收（hermetic，不写库）")
    parser.add_argument("--base-url", default="", help="Ollama 地址，默认 http://localhost:11434")
    parser.add_argument("--expert", default="", help="专家盲标清单 JSON（须先于 AI 产出封存）")
    parser.add_argument("--external-id", default="",
                        help="单文件模式下覆盖 external_id（对齐专家清单归属）")
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

    expert: ExpertBlindList | None = None
    if args.expert:
        expert_path = Path(args.expert).expanduser()
        expert = ExpertBlindList.model_validate(
            json.loads(expert_path.read_text(encoding="utf-8")))
        print(f"专家盲标清单已封存：seal={seal_expert_manifest(expert)} "
              f"（{len(expert.points)} 点，封存于 {expert.sealed_at}）")

    reports = [
        probe_file(
            p, gen_model=args.gen_model, base_url=args.base_url,
            expert=expert, external_id=args.external_id or None,
        )
        for p in files
    ]
    _REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (_REPORT_DIR / "testpoint-probe.json").write_text(
        json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8")
    (_REPORT_DIR / "testpoint-probe.md").write_text(
        render_markdown(reports), encoding="utf-8")

    failed = False
    for r in reports:
        recon = r["fr_reconciliation"]
        print(
            f"{Path(r['file']).name}: 脊柱 {r['spine']['total']} 点 "
            f"{r['spine']['by_kind']}，FR 对账漏点 "
            f"{len(recon['missing_spine_point'])}，取消泄漏 "
            f"{len(recon['cancelled_leaked_into_spine'])}"
        )
        if recon["missing_spine_point"] or recon["cancelled_leaked_into_spine"] \
                or recon["spine_without_marker"]:
            failed = True
        if r.get("model_generation", {}).get("status") == "rejected":
            failed = True
        blind = r.get("blind_eval")
        if blind and blind["status"] == "evaluated":
            gate = blind["gate"]
            if not all(gate.values()):
                failed = True
    print(f"报告：{_REPORT_DIR / 'testpoint-probe.md'}")
    if failed:
        print("❌ 探针发现对账缺口/模型整包被拒/盲标门禁未过，详见报告", file=sys.stderr)
        return 1
    print("✅ testpoint_probe 通过：FR 对账一致，结构合法，盲标门禁（如有）全过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
