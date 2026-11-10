#!/usr/bin/env python3
"""S2.6d 五门禁统一探针（A18）：合成夹具上的确定性机制总验。

对应 docs/06 §3.2 场景② 的五条 POC 门禁，一条探针全部跑齐：
  1. 结构合法率 100%（需求/用例解析零死信、schema 字段齐全）；
  2. 测试点覆盖率 ≥ .75（专家盲标，专家先封存再跑 AI）；
  3. P0/P1 漏点 = 0；
  4. 幻觉点率 ≤ .05（纯确定性脊柱下应为 0）；
  5. oracle 有效率 ≥ .7（专家判「可执行且正确」，探索性用例不进分母）。

盲标纪律留痕：两份专家清单（测试点 + oracle）从 tests/fixtures/s2_gate 载入后，
**先封存（哈希）并写封存副本到 out/eval，再启动任何解析/生成**，文件 mtime 顺序
即「专家先于 AI」的流程证据；探针同时反证 BlindOrderError/OracleBlindError 守卫
活体，防止门禁代码被悄悄绕过。

全程 hermetic：只依赖 core 纯函数与仓内合成夹具（不含语料仓 V1 原文），不触库、
不起端口、不调模型；报告落 out/eval/s2-gate-probe.{json,md}（gitignore）。
真档版（5 个真实需求单元、语料仓只读路径参数化）的手动命令见 README S2.6d 节。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

SUBSYSTEMS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SUBSYSTEMS / "packages" / "core" / "src"))

from testpilot_core.oracle_eval import (  # noqa: E402
    ORACLE_EFFECTIVENESS_GATE,
    OracleBlindError,
    OracleExpertList,
    evaluate_oracles,
    seal_oracle_manifest,
)
from testpilot_core.oracle_mining import mine_deterministic  # noqa: E402
from testpilot_core.requirement import parse_requirement_markdown  # noqa: E402
from testpilot_core.testcase import parse_case_markdown  # noqa: E402
from testpilot_core.testpoint import deterministic_result  # noqa: E402
from testpilot_core.testpoint_eval import (  # noqa: E402
    COVERAGE_GATE,
    HALLUCINATION_GATE,
    PRECISION_GATE,
    BlindOrderError,
    ExpertBlindList,
    evaluate_blind,
    seal_expert_manifest,
)

FIXTURE_DIR = SUBSYSTEMS / "tests" / "fixtures" / "s2_gate"
EVAL_DIR = SUBSYSTEMS / "out" / "eval"
REQ_DOC_ID = "s2_gate/gate-requirements.md"
CASE_DOC_ID = "s2_gate/gate-cases.md"


def _run_pipeline():
    """完整确定性链：需求解析 → 脊柱测试点 → 用例解析 → oracle 四源挖掘。"""
    req_text = (FIXTURE_DIR / "gate-requirements.md").read_text(encoding="utf-8")
    case_text = (FIXTURE_DIR / "gate-cases.md").read_text(encoding="utf-8")

    skeleton = parse_requirement_markdown(req_text, doc_external_id=REQ_DOC_ID)
    generation = deterministic_result(skeleton)
    blind_report = evaluate_blind(generation, _TP_MANIFEST)

    parsed_cases = parse_case_markdown(case_text, doc_external_id=CASE_DOC_ID)
    mining = [
        mine_deterministic(case, skeleton, []) for case in parsed_cases.cases
    ]
    oracle_report = evaluate_oracles(
        parsed_cases.cases, mining, _ORACLE_MANIFEST
    )
    return skeleton, generation, blind_report, parsed_cases, mining, oracle_report


# 模块级占位：main 里先封存再赋值，_run_pipeline 只在封存之后被调用
_TP_MANIFEST: ExpertBlindList | None = None
_ORACLE_MANIFEST: OracleExpertList | None = None


def _count(items, attr: str) -> dict:
    out: dict[str, int] = {}
    for item in items:
        value = getattr(item, attr)
        value = value.value if hasattr(value, "value") else value
        out[value] = out.get(value, 0) + 1
    return dict(sorted(out.items()))


def _render_markdown(doc: dict) -> str:
    lines = [
        "# S2.6d 五门禁统一探针报告（A18，合成夹具机制门禁）", "",
        f"- 生成时间：{doc['generated_at']}",
        f"- 需求夹具：{doc['req_doc']}；用例夹具：{doc['case_doc']}",
        f"- 专家封存：testpoints={doc['seals']['testpoints']}，"
        f"oracles={doc['seals']['oracles']}（封存副本先于管线落盘）", "",
        "## 五门禁汇总", "",
        "| # | 门禁 | 阈值 | 实测 | 结果 |",
        "|---|---|---|---|---|",
    ]
    for gate in doc["gates"]:
        lines.append(
            f"| {gate['index']} | {gate['name']} | {gate['threshold']} "
            f"| {gate['actual']} | {gate['result']} |"
        )
    lines += ["", "## 检查项", ""]
    for check in doc["checks"]:
        lines.append(f"- {check['passed']} {check['name']}：{check['detail']}")

    oracle = doc["oracle_report"]
    lines += ["", "## oracle 盲评逐用例判定", ""]
    for verdict in oracle["verdicts"]:
        lines.append(
            f"- {verdict['case_external_id']}：exploratory={verdict['exploratory']}，"
            f"参评={verdict['evaluated']}，挖出类型={verdict['mined_types']}，"
            f"有效={verdict['valid_types']}，无效={verdict['invalid_types']}，"
            f"缺失={verdict['missing_types']}，manual={verdict['manual_count']}"
        )
    lines += [
        "", "## 测试点盲标指标", "",
        f"- coverage={doc['blind_report']['coverage']}，"
        f"precision={doc['blind_report']['precision']}，"
        f"P0/P1 漏点={len(doc['blind_report']['p0p1_misses'])}，"
        f"幻觉率={doc['blind_report']['hallucination_rate']}",
        "",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    global _TP_MANIFEST, _ORACLE_MANIFEST

    checks: list[dict] = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "passed": "✅" if ok else "❌", "detail": detail})

    EVAL_DIR.mkdir(parents=True, exist_ok=True)

    # ── 第 0 步：专家清单载入即封存，封存副本先于任何 AI 管线落盘（盲序留痕）──
    tp_manifest = ExpertBlindList.model_validate(
        json.loads((FIXTURE_DIR / "gate-expert-testpoints.json").read_text("utf-8"))
    )
    oracle_manifest = OracleExpertList.model_validate(
        json.loads((FIXTURE_DIR / "gate-expert-oracles.json").read_text("utf-8"))
    )
    tp_seal = seal_expert_manifest(tp_manifest)
    oracle_seal = seal_oracle_manifest(oracle_manifest)
    sealed_at = datetime.now(timezone.utc).isoformat()
    for name, manifest, seal in (
        ("testpoints", tp_manifest, tp_seal),
        ("oracles", oracle_manifest, oracle_seal),
    ):
        (EVAL_DIR / f"s2-gate-sealed-{name}.json").write_text(
            json.dumps(
                {
                    "sealed_before_pipeline": True,
                    "sealed_at": sealed_at,
                    "seal_hash": seal,
                    "manifest": manifest.model_dump(),
                },
                ensure_ascii=False, indent=2,
            ),
            encoding="utf-8",
        )
    check(
        "① 盲序封存：两份专家清单先封存再跑管线",
        len(tp_seal) == 16 and len(oracle_seal) == 16
        and len(tp_manifest.points) == 11 and len(oracle_manifest.cases) == 4,
        f"testpoints seal={tp_seal}（{len(tp_manifest.points)} 点），"
        f"oracles seal={oracle_seal}（{len(oracle_manifest.cases)} 用例）",
    )

    # 封存之后才允许管线看到专家清单
    _TP_MANIFEST = tp_manifest
    _ORACLE_MANIFEST = oracle_manifest
    skeleton, generation, blind_report, parsed_cases, mining, oracle_report = (
        _run_pipeline()
    )

    # ── 门禁 1：结构合法率 100% ──
    ac_links = [(a.index, a.linked_fr_ids) for a in skeleton.acceptance]
    expected_links = [(0, ["FR-1"]), (1, ["FR-2", "FR-3"]),
                      (2, ["FR-4"]), (3, [])]
    check(
        "② 结构合法-需求文档：零死信且 FR/NFR/AC 结构与夹具设计一致",
        len(skeleton.dead_letters) == 0
        and len(skeleton.active_features) == 5
        and len(skeleton.rules) == 1
        and ac_links == expected_links,
        f"死信={len(skeleton.dead_letters)}，活跃 FR={len(skeleton.active_features)}，"
        f"NFR={len(skeleton.rules)}，AC 链接={ac_links}",
    )
    # 解析器先摘表格块再扫叙事块，用例顺序不保证与文档先后一致，按集合比对
    case_ids = sorted(c.external_id for c in parsed_cases.cases)
    missing_flags = {c.external_id: c.oracle_missing for c in parsed_cases.cases}
    check(
        "③ 结构合法-用例文档：零死信、4 用例全 active、缺预期标记精确",
        len(parsed_cases.dead_letters) == 0
        and case_ids == ["TC-G1", "TC-G2", "TC-G3", "TC-G4"]
        and all(c.lifecycle == "active" for c in parsed_cases.cases)
        and missing_flags == {
            "TC-G1": False, "TC-G2": False, "TC-G3": True, "TC-G4": False
        },
        f"死信={len(parsed_cases.dead_letters)}，用例={case_ids}，"
        f"oracle_missing={missing_flags}",
    )

    # ── 门禁 2/3/4：覆盖 ≥.75、P0/P1 漏点=0、幻觉 ≤.05（精确率同表 ≥.8 一并卡）──
    check(
        "④ 确定性脊柱：11 个测试点且无模型参与",
        len(generation.points) == 11 and generation.spine_count == 11
        and generation.generation_source == "deterministic"
        and generation.hallucination_rate == 0.0,
        f"点数={len(generation.points)}，脊柱={generation.spine_count}，"
        f"来源={generation.generation_source}，幻觉率={generation.hallucination_rate}",
    )
    check(
        "⑤ 盲标四门禁：覆盖/精确率/P0P1 漏点/幻觉全部过门",
        blind_report.coverage == 1.0 and blind_report.coverage >= COVERAGE_GATE
        and blind_report.precision == 1.0 and blind_report.precision >= PRECISION_GATE
        and not blind_report.p0p1_misses
        and blind_report.hallucination_rate == 0.0
        and blind_report.hallucination_rate <= HALLUCINATION_GATE
        and blind_report.gate.passed
        and blind_report.expert_seal_hash == tp_seal,
        f"coverage={blind_report.coverage}（门 {COVERAGE_GATE}），"
        f"precision={blind_report.precision}（门 {PRECISION_GATE}），"
        f"P0/P1 漏点={len(blind_report.p0p1_misses)}，"
        f"幻觉率={blind_report.hallucination_rate}（门 {HALLUCINATION_GATE}）",
    )

    # ── 挖掘侧盘点 + AC3 守卫 ──
    mining_summary = []
    ac3_violations = []
    for case, result in zip(parsed_cases.cases, mining):
        ac3_bad = result.exploratory == bool(result.executable_oracles)
        if ac3_bad:
            ac3_violations.append(case.external_id)
        mining_summary.append({
            "external_id": case.external_id,
            "oracle_missing": case.oracle_missing,
            "exploratory": result.exploratory,
            "exploratory_reasons": result.exploratory_reasons,
            "mined_total": len(result.mined),
            "executable_total": len(result.executable_oracles),
            "by_source_kind": _count(result.mined, "source_kind"),
            "by_type": _count(result.mined, "type"),
        })
    check(
        "⑥ AC3 守卫：探索性标记与「无可执行 oracle」严格互斥",
        not ac3_violations,
        f"违规用例={ac3_violations or '无'}；盘点={json.dumps(mining_summary, ensure_ascii=False)}",
    )
    g3 = next(r for r in mining_summary if r["external_id"] == "TC-G3")
    g3_result = next(r for c, r in zip(parsed_cases.cases, mining)
                     if c.external_id == "TC-G3")
    g3_ac_sources = {
        o.type.value for o in g3_result.mined
        if o.source_kind == "requirement_ac"
    }
    check(
        "⑦ 缺预期补挖：TC-G3 经需求 AC 源补出可执行 UI oracle（挖掘覆盖率 1/1）",
        g3["oracle_missing"] and not g3["exploratory"]
        and g3["executable_total"] == 1 and g3_ac_sources == {"ui_assertion"},
        f"TC-G3 补挖类型={sorted(g3_ac_sources)}，"
        f"oracle_missing={g3['oracle_missing']}，exploratory={g3['exploratory']}",
    )

    # ── 门禁 5：oracle 有效率 ≥.7 ──
    check(
        "⑧ oracle 有效率：挖出的可执行 oracle 全部被专家判为可执行且正确",
        oracle_report.effective_rate == 1.0
        and oracle_report.effective_rate >= ORACLE_EFFECTIVENESS_GATE
        and oracle_report.invalid_total == 0
        and oracle_report.missing_total == 0
        and oracle_report.coverage_complete
        and oracle_report.expert_seal_hash == oracle_seal,
        f"有效率={oracle_report.effective_rate}"
        f"（门 {ORACLE_EFFECTIVENESS_GATE}），有效={oracle_report.valid_total}，"
        f"无效={oracle_report.invalid_total}，缺失={oracle_report.missing_total}",
    )
    check(
        "⑨ 探索性用例正常出口：TC-G4 不进分母，四分类盘点含人工类",
        oracle_report.exploratory_excluded == 1
        and oracle_report.evaluated_cases == 3
        and oracle_report.by_type.get("manual") == 1
        and set(oracle_report.by_type) == {
            "ui_assertion", "log_assertion", "state_assertion", "manual"
        },
        f"排除={oracle_report.exploratory_excluded}，参评="
        f"{oracle_report.evaluated_cases}，四分类={oracle_report.by_type}",
    )

    # ── 重放幂等：同输入整条链重跑，评测结果逐字段相等 ──
    _, _, blind_report_2, _, _, oracle_report_2 = _run_pipeline()
    check(
        "⑩ 重放幂等：同夹具重跑，两份盲评报告逐字段相等",
        blind_report.model_dump() == blind_report_2.model_dump()
        and oracle_report.model_dump() == oracle_report_2.model_dump(),
        "测试点盲评与 oracle 盲评重放结果均相等（内容寻址 + 确定性脊柱）",
    )

    # ── 盲序守卫活体：空清单/缺判定必须被拒，防门禁被绕过 ──
    blind_guard_alive = False
    try:
        evaluate_blind(
            generation,
            ExpertBlindList(
                doc_external_id=REQ_DOC_ID, sealed_at=sealed_at, points=[]
            ),
        )
    except BlindOrderError:
        blind_guard_alive = True
    oracle_guard_alive = False
    try:
        trimmed = oracle_manifest.model_copy(deep=True)
        trimmed.cases = trimmed.cases[:3]  # 故意漏掉 TC-G4
        evaluate_oracles(parsed_cases.cases, mining, trimmed)
    except OracleBlindError:
        oracle_guard_alive = True
    check(
        "⑪ 盲序守卫活体：空专家清单/漏评用例分别被 BlindOrder/OracleBlind 拒绝",
        blind_guard_alive and oracle_guard_alive,
        f"BlindOrderError={'触发' if blind_guard_alive else '未触发'}，"
        f"OracleBlindError={'触发' if oracle_guard_alive else '未触发'}",
    )
    check(
        "⑫ 封存哈希稳定：同清单重封哈希相等且与报告/封存副本一致",
        seal_expert_manifest(tp_manifest.model_copy(deep=True)) == tp_seal
        and seal_oracle_manifest(oracle_manifest.model_copy(deep=True)) == oracle_seal,
        f"testpoints={tp_seal}，oracles={oracle_seal}",
    )

    passed = sum(1 for c in checks if c["passed"] == "✅")
    gates = [
        {"index": 1, "name": "结构合法率", "threshold": "100%",
         "actual": f"需求死信 {len(skeleton.dead_letters)}、用例死信 "
                   f"{len(parsed_cases.dead_letters)}（均应为 0）",
         "result": "✅" if passed == len(checks) else "❌"},
        {"index": 2, "name": "测试点覆盖率", "threshold": f"≥{COVERAGE_GATE}",
         "actual": str(blind_report.coverage),
         "result": "✅" if blind_report.coverage >= COVERAGE_GATE else "❌"},
        {"index": 3, "name": "P0/P1 漏点", "threshold": "=0",
         "actual": str(len(blind_report.p0p1_misses)),
         "result": "✅" if not blind_report.p0p1_misses else "❌"},
        {"index": 4, "name": "幻觉点率", "threshold": f"≤{HALLUCINATION_GATE}",
         "actual": str(blind_report.hallucination_rate),
         "result": "✅" if blind_report.hallucination_rate <= HALLUCINATION_GATE else "❌"},
        {"index": 5, "name": "oracle 有效率",
         "threshold": f"≥{ORACLE_EFFECTIVENESS_GATE}",
         "actual": str(oracle_report.effective_rate),
         "result": "✅" if oracle_report.gate_ok else "❌"},
    ]
    report_doc = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "fixture_dir": str(FIXTURE_DIR.relative_to(SUBSYSTEMS)),
        "req_doc": REQ_DOC_ID,
        "case_doc": CASE_DOC_ID,
        "seals": {"testpoints": tp_seal, "oracles": oracle_seal},
        "gates": gates,
        "checks": checks,
        "blind_report": blind_report.model_dump(mode="json"),
        "oracle_report": oracle_report.model_dump(mode="json"),
        "mining_summary": mining_summary,
    }
    (EVAL_DIR / "s2-gate-probe.json").write_text(
        json.dumps(report_doc, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (EVAL_DIR / "s2-gate-probe.md").write_text(
        _render_markdown(report_doc), encoding="utf-8"
    )

    for item in checks:
        print(f"{item['passed']} {item['name']}：{item['detail']}")
    print(f"报告：{EVAL_DIR / 's2-gate-probe.md'}")
    if passed != len(checks):
        print("❌ s2_gate_probe 存在失败检查", file=sys.stderr)
        return 1
    print(
        "✅ s2_gate_probe 全部通过：五门禁（结构合法/覆盖/P0P1 漏点/幻觉/oracle 有效率）"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
