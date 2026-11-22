#!/usr/bin/env python3
"""S2.6b 用例↔符号关系可浏览端到端探针（docs/08 FR-ING-004）。

与单测的分工：test_case_relations/store/building/knowledge_api 用 tmp 仓钉死
每条分支；本探针走**生产装配路径**（load_profile → build_container →
workers.relation_building → SQLite relations 独立 scope），在临时工作目录跑
完整故事，不碰 store/offline-demo、不碰语料仓、不起端口、不访问网络。

故事线（确定性，无模型调用）：
  ① 空用例库 fail-closed（ValueError，不造空关系）；
  ② 合成已知关系图：FR tag/确定性草稿需求+符号/模型低置信/测试点反推+邻接/
     取消用例/坏 URN/未知 tag/跨骨架歧义，独立写一份 ground truth 三元组；
  ③ precision/recall 双指标，SRS 门禁 AC3 ≥0.85，确定性构建器实测钉 1.0；
  ④ 低置信默认藏起、include_low 全量；
  ⑤ 证据 dossier：可解析 resolved、幽灵 URN 标 unresolved 不编造；
  ⑥ scope 隔离：摄入 calls 关系在整组替换后零误伤；
  ⑦ 指纹增量 skipped_unchanged → --full 重建；
  ⑧ 输入收缩后幽灵关系被整组清扫；
  ⑨ 诊断字段（unknown/ambiguous/unparseable）如实记账。

报告落 out/eval/relation-probe.{json,md}（out/ 已 gitignore）。任一断言不符 exit 1。

用法（在 subsystems/ 下）：
  .venv/bin/python scripts/relation_probe.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages" / "workers" / "src"))

from testpilot_adapters.store import bootstrap_store  # noqa: E402
from testpilot_core.case_relations import (  # noqa: E402
    CaseRelation,
    case_relation_urn,
    assemble_evidence_dossier,
)
from testpilot_core.enums import OracleType  # noqa: E402
from testpilot_core.models import TestCase, TestPoint  # noqa: E402
from testpilot_core.oracle_mining import MinedOracle  # noqa: E402
from testpilot_core.requirement import (  # noqa: E402
    RequirementFeature,
    RequirementParseResult,
)
from testpilot_orchestrator.config_loader import load_profile  # noqa: E402
from testpilot_orchestrator.factory import build_container  # noqa: E402
from testpilot_workers.relation_building import run_relation_building  # noqa: E402

SUBSYSTEMS = Path(__file__).resolve().parents[1]
PROFILE = SUBSYSTEMS / "config" / "profiles" / "offline-demo.yaml"
EVAL_DIR = SUBSYSTEMS / "out" / "eval"

_CASE_DOC = "urn:testpilot:fs-cases:document:cases/c.md"
_REQ_DOC_A = "urn:testpilot:fs-reqs:document:req-a.md"
_REQ_DOC_B = "urn:testpilot:fs-reqs:document:req-b.md"
_TC1 = "urn:testpilot:fs-cases:test_case:cases/c.md#TC-1"
_TC2 = "urn:testpilot:fs-cases:test_case:cases/c.md#TC-2"
_TCX = "urn:testpilot:fs-cases:test_case:cases/c.md#TC-X"
_FR1 = "urn:testpilot:fs-reqs:requirement:req-a.md#FR-1"
_FR2 = "urn:testpilot:fs-reqs:requirement:req-a.md#FR-2"
_AC1 = "urn:testpilot:fs-reqs:requirement:req-a.md#AC-1"
_SYM1 = "urn:testpilot:fs-docs:symbol:a.swift#AlarmStore.arm"
_SYM2 = "urn:testpilot:fs-docs:symbol:a.swift#MissionCoordinator.schedule"
# 故意不存在的草稿 URN：dossier fail-closed 测试用（真实草稿 URN 由
# suggestion_urn 内容寻址生成，这两个常量不对应任何库内行）
_MISSING_SUG1 = "urn:testpilot:fs-cases:oracle_suggestion:c.md#ghost-s1"
_MISSING_SUG2 = "urn:testpilot:fs-cases:oracle_suggestion:c.md#ghost-s2"
# AC3 门禁目标（docs/08 FR-ING-004 AC3：抽检准确率 ≥0.85）
AC3_GATE = 0.85


class _ShimContainer:
    def __init__(self, store):
        self._store = store

    def get(self, name):
        return self._store if name == "store" else None


def _seed_graph(store) -> None:
    # 两份骨架在 FR-3 上撞 ID（不同 URN）→ 裸 tag 消歧不了，fail-closed 不进
    # fr_index；FR-1/FR-2 保持唯一，正常 tag 路径不受影响（歧义与正常路径解耦）
    store.upsert_requirement_skeleton(
        _REQ_DOC_A,
        RequirementParseResult(
            doc_urn=_REQ_DOC_A, external_id="req-a.md",
            features=[
                RequirementFeature(urn=_FR1, fr_id="FR-1", text="前台响铃"),
                RequirementFeature(urn=_FR2, fr_id="FR-2", text="重复闹钟"),
                RequirementFeature(
                    urn="urn:testpilot:fs-reqs:requirement:req-a.md#FR-3",
                    fr_id="FR-3", text="req-a 的 FR-3"),
            ]),
        content_hash="h-req-a")
    store.upsert_requirement_skeleton(
        _REQ_DOC_B,
        RequirementParseResult(
            doc_urn=_REQ_DOC_B, external_id="req-b.md",
            features=[RequirementFeature(
                urn="urn:testpilot:fs-reqs:requirement:req-b.md#FR-3",
                fr_id="FR-3", text="req-b 的 FR-3（同名不同 URN）")]),
        content_hash="h-req-b")

    store.upsert_test_case(
        _CASE_DOC,
        TestCase(urn=_TC1, external_id="c.md#TC-1", title="前台响铃",
                 steps=["步骤"], tags=["FR-1", "FR-9"]),
        content_hash="h-c1")
    store.upsert_test_case(
        _CASE_DOC,
        TestCase(urn=_TC2, external_id="c.md#TC-2", title="重复闹钟",
                 steps=["步骤"], tags=["FR-2"]),
        content_hash="h-c2")
    store.upsert_test_case(
        _CASE_DOC,
        TestCase(urn=_TCX, external_id="c.md#TC-X", title="取消用例",
                 steps=["步骤"], tags=["FR-1"], lifecycle="cancelled"),
        content_hash="h-cx")

    # TC-1 确定性草稿：需求 AC 引用（0.80）+ 符号引用（0.70）
    store.upsert_oracle_suggestion(
        _CASE_DOC, _TC1, "c.md",
        MinedOracle(type=OracleType.UI_ASSERTION, expectation="全屏提醒",
                    source_refs=[_AC1, _SYM1], source_kind="requirement_ac"),
        content_hash="h-s1")
    # TC-2 模型草稿：符号引用（0.50 低置信）+ 一个坏 URN（记账不产关系）
    store.upsert_oracle_suggestion(
        _CASE_DOC, _TC2, "c.md",
        MinedOracle(type=OracleType.LOG_ASSERTION, expectation="日志含 arm",
                    source_refs=[_SYM1, "裸ID-不是URN"],
                    source_kind="model_proposed", provenance="model"),
        content_hash="h-s2")

    # 测试点：影响面反推 TC-1（0.60）；FR-2 邻接符号（TC-2 → SYM2，0.50 低置信）
    store.upsert_test_point(
        _REQ_DOC_A, "req-a.md",
        TestPoint(text="验证响铃主流程", fr_id="FR-1",
                  source_refs=[_FR1, _TC1], kind="functional"),
        content_hash="h-p1")
    store.upsert_test_point(
        _REQ_DOC_A, "req-a.md",
        TestPoint(text="重复闹钟边界", fr_id="FR-2",
                  source_refs=[_FR2, _SYM2], kind="boundary"),
        content_hash="h-p2")


def _expected_triples() -> set[tuple[str, str, str]]:
    """独立手写的 ground truth（不与构建器共享任何代码路径）。"""
    v = "case_validates_requirement"
    s = "case_references_symbol"
    return {
        # 三元组集合：路径①FR tag 0.90 与路径③测试点反推 0.60 同目标，合并为一条
        (_TC1, v, _FR1),   # FR tag 0.90（合并测试点影响面 0.60）
        (_TC1, v, _AC1),   # 确定性草稿需求引用 0.80
        (_TC1, s, _SYM1),  # 确定性草稿符号引用 0.70
        (_TC2, v, _FR2),   # FR tag 0.90
        (_TC2, s, _SYM1),  # 模型草稿符号 0.50（低置信）
        (_TC2, s, _SYM2),  # FR 邻接传递 0.50（低置信）
    }


def main() -> int:
    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="relation-probe-", dir=EVAL_DIR))
    checks: list[dict[str, str]] = []
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "passed": "✅" if ok else "❌", "detail": detail})
        if not ok:
            failures.append(f"{name}: {detail}")

    config = load_profile(PROFILE)
    config.paths["data_dir"] = str(work / "data")
    container = build_container(config, SUBSYSTEMS)
    store = container.get("store")
    main_store_closed = False

    try:
        # ① 空库 fail-closed
        empty_store = bootstrap_store(work / "empty-data")
        empty_rejected = False
        try:
            run_relation_building(_ShimContainer(empty_store))
        except ValueError:
            empty_rejected = True
        finally:
            empty_store.close()
        check("① 空用例库 fail-closed 不造空关系", empty_rejected,
              f"rejected={empty_rejected}")

        # ② 合成图构建
        _seed_graph(store)
        report = run_relation_building(container)
        relations = store.list_case_relations(include_low=True)
        actual = {(r.source_urn, r.kind, r.target_urn) for r in relations}
        expected = _expected_triples()
        check("② 构建产出关系数与 ground truth 一致",
              report["relations_total"] == len(relations) == len(expected),
              f"actual={len(actual)} expected={len(expected)}")

        # ③ precision/recall（AC3 ≥.85；确定性构建器钉 1.0）
        tp = len(actual & expected)
        precision = tp / len(actual) if actual else 0.0
        recall = tp / len(expected) if expected else 0.0
        check("③ AC3 precision/recall 过门且确定性构建无错配",
              precision >= AC3_GATE and recall >= AC3_GATE
              and precision == 1.0 and recall == 1.0,
              f"precision={precision:.3f} recall={recall:.3f} gate={AC3_GATE}")

        # ④ 低置信可见性阈值
        visible = store.list_case_relations()
        low = [r for r in relations if r.is_low_confidence]
        visible_targets = {r.target_urn for r in visible}
        # SYM1 同时存在确定性 0.70（可见）与模型 0.50（隐藏）两条不同 source 的关系
        check("④ 低置信默认藏起、include_low 全量",
              len(visible) == len(relations) - len(low)
              and _SYM1 in visible_targets and _SYM2 not in visible_targets,
              f"visible={len(visible)} low={len(low)} total={len(relations)}")

        # ⑤ 证据 dossier：resolved + 幽灵 unresolved
        fr_rel = next(r for r in relations
                      if r.source_urn == _TC1 and r.kind == "case_validates_requirement"
                      and r.target_urn == _FR1)
        rows = store.get_evidence_rows(fr_rel.evidence_urns)
        dossier = assemble_evidence_dossier(fr_rel, rows)
        ghost = CaseRelation(
            urn=case_relation_urn(_TC1, "case_references_symbol", _SYM2),
            source_urn=_TC1, target_urn=_SYM2,
            kind="case_references_symbol", confidence=0.5,
            evidence_urns=[_MISSING_SUG1, _MISSING_SUG2])  # 库内不存在的草稿 URN
        ghost_dossier = assemble_evidence_dossier(
            ghost, store.get_evidence_rows(ghost.evidence_urns))
        check("⑤ 证据 dossier 可解析且幽灵 URN 标 unresolved 不编造",
              dossier.complete and all(i.status == "resolved" for i in dossier.items)
              and not ghost_dossier.complete
              and all(i.status == "unresolved" and i.excerpt == ""
                      for i in ghost_dossier.items),
              f"complete={dossier.complete} ghost_complete={ghost_dossier.complete}")

        # ⑥ scope 隔离：摄入 calls 关系零误伤
        store.conn.execute(
            """INSERT INTO relations
               (urn, source_urn, target_urn, kind, confidence,
                evidence_json, doc_urn)
               VALUES (?,?,?,?,?, '{"lines": [], "urns": []}', ?)""",
            ("urn:testpilot:fs-docs:rel:a.swift#x->y", _SYM1,
             "urn:testpilot:fs-docs:symbol:a.swift#Other.arm",
             "calls", 0.9,
             "urn:testpilot:fs-docs:document:a.swift"))
        store.conn.commit()
        run_relation_building(container, full=True)
        calls_n = store.conn.execute(
            "SELECT COUNT(*) AS n FROM relations WHERE kind='calls'").fetchone()["n"]
        scoped_n = store.count_case_relations()
        check("⑥ 整组替换 scope 隔离，摄入 calls 关系零误伤",
              calls_n == 1 and scoped_n == len(expected),
              f"calls={calls_n} scoped={scoped_n}")

        # ⑦ 增量指纹
        skipped = run_relation_building(container)
        check("⑦ 输入未变 skipped_unchanged，--full 强制重建",
              skipped["status"] == "skipped_unchanged" and skipped["replaced"] == 0,
              json.dumps({k: skipped[k] for k in ("status", "replaced")},
                         ensure_ascii=False))

        # ⑧ 幽灵清扫：删掉 TC-2 的草稿与点后重建，其关系应消失
        store.conn.execute("DELETE FROM oracle_suggestions WHERE case_urn=?", (_TC2,))
        store.conn.execute("DELETE FROM test_points WHERE fr_id='FR-2'")
        store.conn.commit()
        shrunk = run_relation_building(container, full=True)
        remaining = store.list_case_relations(include_low=True)
        tc2_left = [r for r in remaining if r.source_urn == _TC2]
        check("⑧ 输入收缩后幽灵关系整组清扫",
              shrunk["relations_total"] == len(remaining)
              and {r.target_urn for r in tc2_left} == {_FR2},
              f"total={shrunk['relations_total']} tc2_left={len(tc2_left)}")

        # ⑨ 诊断字段：FR-3 跨骨架歧义（无 case 引用，仅诊断）、FR-9 未知、坏 URN
        check("⑨ 未知 tag/歧义/坏 URN 诊断如实记账",
              report["unknown_fr_tags"] == ["FR-9"]
              and report["ambiguous_fr_tags"] == ["FR-3"]
              and report["unparseable_refs"] == ["裸ID-不是URN"]
              and report["cases_cancelled"] == 1,
              json.dumps({k: report[k] for k in
                          ("unknown_fr_tags", "ambiguous_fr_tags",
                           "unparseable_refs", "cases_cancelled")},
                         ensure_ascii=False))

        store.close()
        main_store_closed = True

        # ⑩ 重开库：scope 关系持久化无损
        reopened = bootstrap_store(config.paths["data_dir"])
        try:
            reopened_n = reopened.count_case_relations()
            reopened_hash = reopened.get_meta("case-relations-hash:v2")
        finally:
            reopened.close()
        check("⑩ 重开库关系与指纹持久化无损",
              reopened_n == shrunk["relations_total"] and bool(reopened_hash),
              f"relations={reopened_n} hash_present={bool(reopened_hash)}")
    finally:
        if not main_store_closed:
            store.close()

    passed = sum(1 for c in checks if c["passed"] == "✅")
    report_doc = {
        "probe": "relation_probe",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "workdir": str(work),
        "ac3_gate": AC3_GATE,
        "summary": {"passed": passed, "failed": len(failures), "total": len(checks)},
        "metrics": {"precision": precision, "recall": recall},
        "checks": checks,
    }
    (EVAL_DIR / "relation-probe.json").write_text(
        json.dumps(report_doc, ensure_ascii=False, indent=2), encoding="utf-8")
    md_lines = [
        "# S2.6b 用例关系探针报告（relation_probe / FR-ING-004）",
        "",
        f"生成时间：{report_doc['generated_at']}",
        f"工作目录：`{work}`",
        "",
        f"AC3 门禁：precision/recall ≥ {AC3_GATE}（实测 {precision:.3f}/{recall:.3f}）",
        "",
        "| # | 检查 | 结果 | 证据 |",
        "|---|---|---|---|",
    ]
    md_lines += [
        f"| {i} | {c['name']} | {c['passed']} | {c['detail']} |"
        for i, c in enumerate(checks, 1)
    ]
    (EVAL_DIR / "relation-probe.md").write_text(
        "\n".join(md_lines) + "\n", encoding="utf-8")

    for c in checks:
        print(f"{c['passed']} {c['name']} — {c['detail']}")
    print(f"报告：{EVAL_DIR / 'relation-probe.json'}")
    if failures:
        print("❌ relation_probe 存在失败检查", file=sys.stderr)
        return 1
    print("✅ relation_probe 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
