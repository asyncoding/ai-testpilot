#!/usr/bin/env python3
"""S3.4 失败归因端到端探针（FR-EX-007，A22）。

与单测分工：test_attribution* 钉死每条规则分支；本探针走**完整故事线**——
加载盲标夹具 → 引擎逐例归因 → 混淆矩阵与四门禁 → workers 组包服务，
验证：零端口、零网络、零真机状态、零 xcodebuild，纯 hermetic。

故事线（12 项）：
  ① 规则版本与候选闭集（五候选 + needs_human）；
  ②–⑤ AC1 四历史原型：90→30 过期 / 授权竞态 / 真机能力缺失 / flaky；
  ⑥ 产品改动与用例过期的分界（变更记录覆盖符号）；
  ⑦ 不对称护栏：缺陷案挂环境噪声仍判真缺陷；
  ⑧ AC4 三种 needs_human：薄证据 / 双 0.9 冲突 / 护栏拦截；
  ⑨ 全量 40 例混淆矩阵 + 四门禁实测值（准确率/真缺陷 recall/误误导/证据充分率）；
  ⑩ 误误导口径：缺陷→flaky 计数、缺陷→needs_human 弃权不算误导；
  ⑪ workers 组包服务：Null 提供者必 needs_human，假件 flaky 证据链闭环；
  ⑫ 封存哈希稳定性 + 零除守卫（缺席类 precision/recall 为 None 不伪造 0）。

报告落 out/eval/attribution-probe.{json,md}（out/ 已 gitignore）。任一断言不符 exit 1。

用法（在 subsystems/ 下）：
  .venv/bin/python scripts/attribution_probe.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages" / "workers" / "src"))

from testpilot_core.attribution import (  # noqa: E402
    ATTRIBUTION_RULE_VERSION,
    RerunRecord,
    attribute_failure,
)
from testpilot_core.attribution_eval import (  # noqa: E402
    CATEGORIES,
    LabeledManifest,
    evaluate_attribution,
    misleading_rate,
    per_class_metrics,
    real_defect_recall,
    seal_manifest,
)
from testpilot_core.enums import (  # noqa: E402
    CaseStatus,
    FailureCategory,
    RunStatus,
)
from testpilot_core.models import (  # noqa: E402
    CaseResult,
    EnvironmentInfo,
    TestRun,
)
from testpilot_workers.attribution_service import (  # noqa: E402
    AttributionService,
    NullEvidenceProvider,
)

SUBSYSTEMS = Path(__file__).resolve().parents[1]
FIXTURE = SUBSYSTEMS / "tests" / "fixtures" / "attribution" / "labeled_cases.json"
EVAL_DIR = SUBSYSTEMS / "out" / "eval"
_COMMIT = "c2ffee01"


class _FlakyProvider:
    async def reruns(self, case, run):
        return [
            RerunRecord(run_id="run-a", commit_sha=_COMMIT, status=CaseStatus.FAILED),
            RerunRecord(run_id="run-b", commit_sha=_COMMIT, status=CaseStatus.PASSED),
        ]

    async def assertion_check(self, case, run):
        return None

    async def log_signals(self, case, run):
        return []

    async def diff_signal(self, case, run):
        return None

    async def environment_signals(self, case, run):
        return []

    async def change_records(self, case, run):
        return []


def _case_by_id(manifest: LabeledManifest, case_id: str):
    return next(c for c in manifest.cases if c.case_id == case_id)


def _run() -> TestRun:
    return TestRun(
        run_id="run-a22",
        idempotency_key="idem-a22",
        project="ios-demo",
        commit_sha=_COMMIT,
        status=RunStatus.FAILED,
        environment=EnvironmentInfo(provider="ios-simulator", env_id="sim-a22"),
        case_results=[
            CaseResult(external_case_id="Case/flaky()", status=CaseStatus.FAILED)
        ],
    )


async def _service_checks() -> list[tuple[str, bool, str]]:
    out: list[tuple[str, bool, str]] = []
    null_verdict = await AttributionService(NullEvidenceProvider()).attribute_case(
        _run(), _run().case_results[0]
    )
    out.append((
        "组包服务：无证据提供者 → needs_human（AC4 不硬归因）",
        null_verdict.category == FailureCategory.NEEDS_HUMAN,
        f"category={null_verdict.category.value}, refs={null_verdict.evidence_refs}",
    ))
    flaky_verdict = await AttributionService(_FlakyProvider()).attribute_case(
        _run(), _run().case_results[0]
    )
    out.append((
        "组包服务：假件 flaky 证据链 → flaky 且账本可回溯",
        flaky_verdict.category == FailureCategory.FLAKY
        and flaky_verdict.evidence_refs == ["ev-rerun"],
        f"category={flaky_verdict.category.value}, refs={flaky_verdict.evidence_refs}",
    ))
    return out


def main() -> int:
    checks: list[dict[str, str]] = []
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "passed": "✅" if ok else "❌", "detail": detail})
        if not ok:
            failures.append(name)

    manifest = LabeledManifest.model_validate_json(FIXTURE.read_text())
    by_id = {c.case_id: c for c in manifest.cases}

    # ① 规则版本与候选闭集
    check(
        "① 规则版本存在且候选闭集为六类",
        bool(ATTRIBUTION_RULE_VERSION)
        and set(CATEGORIES) == set(FailureCategory),
        f"version={ATTRIBUTION_RULE_VERSION}, categories={len(CATEGORIES)}",
    )

    # ② AC1 原型一：90→30 用例过期
    v = attribute_failure(by_id["OUTDATED-01"].evidence)
    check(
        "② AC1 原型：90s→30s 断言失配 → case_outdated",
        v.category == FailureCategory.CASE_OUTDATED and v.confidence == 0.85,
        f"{v.category.value}({v.confidence}) refs={v.evidence_refs}",
    )

    # ③ AC1 原型二：授权竞态
    v = attribute_failure(by_id["ENV-01"].evidence)
    check(
        "③ AC1 原型：授权竞态 + 同族 os_log → environment",
        v.category == FailureCategory.ENVIRONMENT
        and {"ev-env-1", "ev-log-1"} <= set(v.evidence_refs),
        f"{v.category.value}({v.confidence}) refs={v.evidence_refs}",
    )

    # ④ AC1 原型三：真机能力缺失
    v = attribute_failure(by_id["ENV-03"].evidence)
    check(
        "④ AC1 原型：模拟器不暴露真机行为 → environment + 未覆盖层声明",
        v.category == FailureCategory.ENVIRONMENT
        and "ios.device" in v.uncovered_layers,
        f"{v.category.value}, uncovered={v.uncovered_layers}",
    )

    # ⑤ AC1 原型四：flaky
    v = attribute_failure(by_id["FLAKY-01"].evidence)
    check(
        "⑤ AC1 原型：同 commit 时过时不过 → flaky",
        v.category == FailureCategory.FLAKY and v.confidence == 0.9,
        f"{v.category.value}({v.confidence})",
    )

    # ⑥ 产品改动分界
    v = attribute_failure(by_id["PRODUCT-01"].evidence)
    check(
        "⑥ 断言失配 + 变更记录覆盖符号 → product_change（区别于用例过期）",
        v.category == FailureCategory.PRODUCT_CHANGE
        and "ev-change-1" in v.evidence_refs,
        f"{v.category.value} refs={v.evidence_refs}",
    )

    # ⑦ 不对称护栏
    v = attribute_failure(by_id["RD-DIFF-09"].evidence)
    check(
        "⑦ 真缺陷挂环境噪声诱饵仍判 real_defect（护栏不放缺陷）",
        v.category == FailureCategory.REAL_DEFECT and v.confidence == 0.9,
        f"{v.category.value}({v.confidence}) candidates="
        f"{[c.category.value for c in v.candidates]}",
    )

    # ⑧ AC4 needs_human 三场景
    human_ids = ["HUMAN-01", "HUMAN-02", "HUMAN-03"]
    human_ok = True
    human_detail = []
    for cid in human_ids:
        v = attribute_failure(by_id[cid].evidence)
        human_detail.append(f"{cid}→{v.category.value}")
        human_ok &= v.category == FailureCategory.NEEDS_HUMAN
        if cid != "HUMAN-01":
            human_ok &= bool(v.candidates)  # 冲突场景保留候选给人审
    check("⑧ AC4 三种证据不足/冲突 → needs_human 且冲突保留候选", human_ok,
          "; ".join(human_detail))

    # ⑨ 全量混淆矩阵 + 四门禁
    report = evaluate_attribution(manifest)
    gate_map = {g.name: g for g in report.gates}
    check(
        "⑨ 40 例盲标四门禁全过（准确率/真缺陷 recall/误误导/证据充分率）",
        report.all_gates_passed,
        f"n={report.n} acc={report.accuracy:.3f} recall={report.real_defect_recall:.3f} "
        f"misleading={report.misleading_rate:.3f} sufficiency={report.evidence_sufficiency:.3f}",
    )
    check(
        "⑨b 混淆矩阵 36 格齐全且总数=40",
        len(report.confusion) == 36 and sum(report.confusion.values()) == 40,
        f"cells={len(report.confusion)} sum={sum(report.confusion.values())}",
    )

    # ⑩ 误误导口径
    labels = [FailureCategory.REAL_DEFECT] * 3
    preds_misled = [
        FailureCategory.FLAKY,           # 误导
        FailureCategory.NEEDS_HUMAN,     # 弃权：不算
        FailureCategory.REAL_DEFECT,     # 正确
    ]
    check(
        "⑩ 误误导率只数非缺陷收口；needs_human 弃权不计",
        misleading_rate(labels, preds_misled) == 1 / 3
        and real_defect_recall(labels, preds_misled) == 1 / 3,
        f"misleading={misleading_rate(labels, preds_misled):.3f}",
    )

    # ⑪ workers 组包服务
    for name, ok, detail in asyncio.run(_service_checks()):
        check(name, ok, detail)

    # ⑫ 封存哈希 + 零除守卫
    seal_ok = seal_manifest(manifest) == report.manifest_seal and len(
        seal_manifest(manifest)
    ) == 16
    metrics = per_class_metrics(
        [FailureCategory.FLAKY], [FailureCategory.FLAKY]
    )
    env_metric = next(m for m in metrics if m.category == FailureCategory.ENVIRONMENT)
    check(
        "⑫ 封存哈希稳定可复现；缺席类 precision/recall=None 不伪造 0",
        seal_ok and env_metric.precision is None and env_metric.recall is None,
        f"seal={seal_manifest(manifest)} env_metric=({env_metric.precision},"
        f"{env_metric.recall})",
    )

    # ── 报告落盘 ────────────────────────────────────────────────────────────
    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    report_doc = {
        "probe": "attribution_probe",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "manifest_seal": report.manifest_seal,
        "rule_version": ATTRIBUTION_RULE_VERSION,
        "summary": {
            "passed": sum(1 for c in checks if c["passed"] == "✅"),
            "failed": len(failures),
            "total": len(checks),
        },
        "metrics": {
            "n": report.n,
            "accuracy": report.accuracy,
            "real_defect_recall": report.real_defect_recall,
            "misleading_rate": report.misleading_rate,
            "abstention_rate": report.abstention_rate,
            "evidence_sufficiency": report.evidence_sufficiency,
            "gates": [g.model_dump() for g in report.gates],
        },
        "checks": checks,
    }
    (EVAL_DIR / "attribution-probe.json").write_text(
        json.dumps(report_doc, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    md_lines = [
        "# S3.4 失败归因探针报告（attribution_probe / FR-EX-007）",
        "",
        f"生成时间：{report_doc['generated_at']}",
        f"盲标封存：`{report.manifest_seal}`（规则版本 {ATTRIBUTION_RULE_VERSION}）",
        "",
        "| # | 检查 | 结果 | 证据 |",
        "|---|---|---|---|",
    ]
    md_lines += [
        f"| {i} | {c['name']} | {c['passed']} | {c['detail']} |"
        for i, c in enumerate(checks, 1)
    ]
    (EVAL_DIR / "attribution-probe.md").write_text(
        "\n".join(md_lines) + "\n", encoding="utf-8"
    )

    for c in checks:
        print(f"{c['passed']} {c['name']} — {c['detail']}")
    print(f"报告：{EVAL_DIR / 'attribution-probe.json'}")
    if failures:
        print("❌ attribution_probe 存在失败检查", file=sys.stderr)
        return 1
    print("✅ attribution_probe 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
