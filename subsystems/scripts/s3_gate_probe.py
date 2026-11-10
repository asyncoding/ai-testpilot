#!/usr/bin/env python3
"""S3.5 S3 五门禁总验探针（A23）：执行与失败归因纵切的收口门禁。

对应 docs/07 §10 / docs/06 §3.3 场景③ 的五条 POC 门禁，一条探针全部跑齐：
  ① 执行成功率 ≥.95——runner 任务成功产出结构化 TestRun 的比例（基础设施口径，
     非用例通过率）；error/timeout/canceled 单列，不混进成功率分母；
  ② 归因准确率 ≥.7 且真缺陷 recall ≥.95、误误导率 ≤.02（40 例封存盲标矩阵）；
  ③ 证据充分率 ≥.9——hermetic 口径为引擎非弃权率（1−needs_human 弃权率）；
     docs/06 的生产口径是「人工抽检第三人可复核比例」，本探针在报告中注明
     该人工环节不在 hermetic 范围，另附装配链路真实证据 refs 观察值作旁证；
  ④ 幂等正确性 100%——run/账本/job/预算/取消重放零重复；
  ⑤ 重跑预算遵守 100%——超日配额拒入且不留幻影（无账本/无 job/预算不增）。

盲标纪律留痕：40 例 LabeledManifest 载入后**先封存（sha256[:16]）并写封存副本
到 out/eval，再启动 evaluate_attribution**；探针同时反证空清单守卫活体，
并做红证对照（故意把一条真缺陷预测翻成 flaky，证明 recall/误导门禁真有牙）。

全程 hermetic：执行段走生产装配路径（load_profile→build_container→
execution_service），但 data_dir 指向 tempfile 临时库，不碰 store/offline-demo、
不起端口、不触真机/模拟器、无网络无模型调用（录制 runner + 录制 scm）。
报告落 out/eval/s3-gate-probe.{json,md}（out/ 已 gitignore），重复执行幂等。

用法（在 subsystems/ 下）：
  .venv/bin/python scripts/s3_gate_probe.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages" / "workers" / "src"))

from testpilot_adapters.store import bootstrap_store  # noqa: E402
from testpilot_core import SCHEMA_VERSION  # noqa: E402
from testpilot_core.attribution_eval import (  # noqa: E402
    ACCURACY_GATE,
    EVIDENCE_SUFFICIENCY_GATE,
    MISLEADING_RATE_GATE,
    REAL_DEFECT_RECALL_GATE,
    LabeledManifest,
    evaluate_attribution,
    misleading_rate,
    real_defect_recall,
    seal_manifest,
)
from testpilot_core.enums import FailureCategory, RunStatus  # noqa: E402
from testpilot_core.execution import run_id_for  # noqa: E402
from testpilot_core.models import CaseSelector, TestExecutionRequest  # noqa: E402
from testpilot_orchestrator.config_loader import load_profile  # noqa: E402
from testpilot_orchestrator.factory import build_container  # noqa: E402
from testpilot_workers import execution_service as svc  # noqa: E402
from testpilot_workers.execution_service import RerunQuotaExceededError  # noqa: E402

SUBSYSTEMS = Path(__file__).resolve().parents[1]
PROFILE = SUBSYSTEMS / "config" / "profiles" / "offline-demo.yaml"
FIXTURE = SUBSYSTEMS / "tests" / "fixtures" / "attribution" / "labeled_cases.json"
EVAL_DIR = SUBSYSTEMS / "out" / "eval"

# 门①批量执行规模：12 笔同 project/commit 的录制 run，既统计结构化产出率，
# 又让 StoreBackedEvidenceProvider 的 rerun seam 凭账本历史形成跨 run 归因。
BATCH_N = 12
BATCH_PROJECT = "probe-s3gate-batch"
BUDGET_PROJECT = "probe-s3gate-budget"
CANCEL_PROJECT = "probe-s3gate-cancel"
ERROR_PROJECT = "probe-s3gate-err"
BUDGET_KEY = f"{BUDGET_PROJECT}:rerun"


class _SlowRunner:
    """submit 立即拿句柄、get_run 卡住：模拟 xcodebuild 子进程挂死（超时路径）。"""

    def __init__(self) -> None:
        self.canceled: list[str] = []

    async def submit(self, request):
        return "slow-1"

    async def get_run(self, run_id):
        await asyncio.sleep(0.3)
        raise AssertionError("超时路径不应返回结果")

    async def cancel(self, run_id):
        self.canceled.append(run_id)


def _req(request_id: str, *, rerun_key: str | None = None,
         hint: str | None = "xcode_hostless",
         project: str = BATCH_PROJECT) -> TestExecutionRequest:
    return TestExecutionRequest(
        request_id=request_id,
        idempotency_key=f"idem-{request_id}",
        project=project,
        selector=CaseSelector(test_ids=["T/t1"], fixture_hint=hint),
        commit_sha="deadbeef",
        daily_budget_key=rerun_key,
    )


async def _execution_story(check) -> dict:
    """门①④⑤的生产装配故事，全部跑在 tempfile 临时库。"""
    work = Path(tempfile.mkdtemp(prefix="s3-gate-", dir=EVAL_DIR))
    config = load_profile(PROFILE)
    config.paths["data_dir"] = str(work / "data")
    config.limits.daily_rerun_quota = 2
    container = build_container(config, SUBSYSTEMS)
    store = container.get("store")
    evidence: dict = {"workdir": str(work)}
    try:
        # ⓪ 冷启动
        empty = await svc.run_once(container)
        check("⓪ 冷启动：空队列 run_once=None 且执行账本为空",
              empty is None and svc.list_runs(container) == [],
              f"run_once={empty} runs={len(svc.list_runs(container))}")

        # ── 门① 批量执行：12 笔录制 run 全部产出结构化 TestRun ──────────────
        for i in range(BATCH_N):
            await svc.submit_execution(container, _req(f"batch-{i:02d}"))
        outcomes: list[dict] = []
        while (summary := await svc.run_once(container)) is not None:
            outcomes.append(summary)
        batch_rows = store.list_test_runs(project=BATCH_PROJECT)
        structured = [r for r in batch_rows if store.row_to_test_run(r) is not None]
        rate = len(structured) / BATCH_N
        sample = store.row_to_test_run(structured[0])
        structure_ok = (
            len(outcomes) == BATCH_N
            and len(batch_rows) == BATCH_N
            and len(sample.case_results) == 2
            and {a.type.value for a in sample.artifacts} == {"xcresult"}
            and sample.environment.provider == "ios-simulator"
            and bool(sample.uncovered_layers)
        )
        check(
            f"门禁① 执行成功率：{len(structured)}/{BATCH_N} 结构化产出"
            f"（{rate:.2f} ≥ 0.95），用例/制品/环境/未覆盖层结构齐全",
            rate >= 0.95 and structure_ok,
            f"rate={rate:.2f} outcomes={len(outcomes)} "
            f"cases={len(sample.case_results)} artifacts={[a.type.value for a in sample.artifacts]} "
            f"uncovered={sample.uncovered_layers}",
        )
        evidence["execution"] = {
            "batch_n": BATCH_N,
            "structured_n": len(structured),
            "success_rate": rate,
            "by_outcome": dict(sorted(
                (o["outcome"], sum(1 for x in outcomes if x["outcome"] == o["outcome"]))
                for o in outcomes)),
        }

        # 门③旁证：真实装配链路上的归因（StoreBacked 凭 test_runs 账本历史）。
        # 首跑只有自身一条观察 → needs_human；第 2 笔起同 commit 两次失败且消息
        # 属缺陷族（XCTAssert）→ real_defect 0.75，证据 refs 可回溯。
        assembled: list[dict] = []
        for row in structured:
            run = store.row_to_test_run(row)
            for v in run.attributions:
                assembled.append({
                    "run_id": row["run_id"], "category": v.category.value,
                    "confidence": v.confidence, "evidence_refs": v.evidence_refs,
                })
        first_verdict = assembled[0]
        later = assembled[1:]
        assembled_ok = (
            len(assembled) == BATCH_N
            and first_verdict["category"] == FailureCategory.NEEDS_HUMAN.value
            and first_verdict["evidence_refs"] == ["ev-rerun"]
            and all(v["category"] == FailureCategory.REAL_DEFECT.value
                    and v["confidence"] == 0.75 and v["evidence_refs"] == ["ev-rerun"]
                    for v in later)
        )
        non_abstain = sum(
            1 for v in assembled if v["category"] != FailureCategory.NEEDS_HUMAN.value)
        check(
            "门③旁证 装配链路真实归因：首跑 needs_human、第 2 笔起凭账本历史 "
            "real_defect 0.75（ev-rerun 可回溯），非弃权观察值仅作旁证不进门禁",
            assembled_ok,
            f"verdicts={len(assembled)} 首={first_verdict['category']} "
            f"后续集合={sorted({v['category'] for v in later})} "
            f"装配侧非弃权={non_abstain}/{len(assembled)}",
        )
        evidence["assembled_attribution"] = {
            "verdicts": assembled,
            "non_abstention_observation": non_abstain / len(assembled),
            "caliber_note": "装配链路观察值；门③以封存 40 例盲标的引擎非弃权率为准",
        }

        # ── 门④-a 提交幂等：终态后同幂等键重放，run/job/账本零增生 ──────────
        jobs_before = store.conn.execute(
            "SELECT COUNT(*) n FROM jobs").fetchone()["n"]
        runs_before = len(store.list_test_runs(project=BATCH_PROJECT))
        replay = await svc.submit_execution(container, _req("batch-00"))
        jobs_after = store.conn.execute(
            "SELECT COUNT(*) n FROM jobs").fetchone()["n"]
        runs_after = len(store.list_test_runs(project=BATCH_PROJECT))
        check(
            "门禁④-a 提交幂等：同幂等键重放 replayed=True 且 job/run 账本零增生",
            replay == {"run_id": run_id_for("idem-batch-00"),
                       "status": RunStatus.FAILED.value, "replayed": True}
            and jobs_before == jobs_after and runs_before == runs_after,
            f"replayed={replay['replayed']} jobs {jobs_before}→{jobs_after} "
            f"runs {runs_before}→{runs_after}",
        )

        # ── 门④-b 取消幂等：两次 request_cancel 同标一单，worker 不碰适配器 ──
        cancel_submit = await svc.submit_execution(
            container, _req("req-cancel", project=CANCEL_PROJECT))
        ack1 = await svc.request_cancel(container, cancel_submit["run_id"])
        ack2 = await svc.request_cancel(container, cancel_submit["run_id"])
        cancel_jobs = store.conn.execute(
            "SELECT COUNT(*) n, SUM(cancel_requested) flag FROM jobs "
            "WHERE idempotency_key=?", ("idem-req-cancel",)).fetchone()
        cancel_summary = await svc.run_once(container)
        cancel_row = store.get_test_run_row(cancel_submit["run_id"])
        cancel_replay = await svc.submit_execution(
            container, _req("req-cancel", project=CANCEL_PROJECT))
        jobs_after_cancel = store.conn.execute(
            "SELECT COUNT(*) n FROM jobs").fetchone()["n"]
        check(
            "门禁④-b 取消幂等：两次取消同标一单、worker canceled 收口、重放零增生",
            ack1["cancel_requested"] and ack2["cancel_requested"]
            and cancel_jobs["n"] == 1 and cancel_jobs["flag"] == 1
            and cancel_summary["outcome"] == RunStatus.CANCELED.value
            and cancel_row["status"] == "canceled"
            and cancel_replay["replayed"] is True
            and jobs_after_cancel == jobs_after + 1,
            f"ack=({ack1['cancel_requested']},{ack2['cancel_requested']}) "
            f"jobs={cancel_jobs['n']} flag={cancel_jobs['flag']} "
            f"outcome={cancel_summary['outcome']} replay={cancel_replay['replayed']}",
        )

        # ── 门⑤ 重跑预算：quota=2，第三笔拒入且零幻影 ───────────────────────
        await svc.submit_execution(
            container, _req("first-run", project=BUDGET_PROJECT))  # 首跑不占配额
        await svc.submit_execution(
            container, _req("rerun-1", rerun_key=BUDGET_KEY, project=BUDGET_PROJECT))
        await svc.submit_execution(
            container, _req("rerun-2", rerun_key=BUDGET_KEY, project=BUDGET_PROJECT))
        denied = False
        try:
            await svc.submit_execution(
                container, _req("rerun-3", rerun_key=BUDGET_KEY, project=BUDGET_PROJECT))
        except RerunQuotaExceededError:
            denied = True
        today = datetime.now(timezone.utc).date().isoformat()
        budget_n = store.count_budget_today(BUDGET_KEY, today)
        phantom = store.get_test_run_row(run_id_for("idem-rerun-3"))
        budget_jobs = store.conn.execute(
            "SELECT COUNT(*) n FROM jobs WHERE idempotency_key IN (?,?,?)",
            ("idem-rerun-1", "idem-rerun-2", "idem-rerun-3")).fetchone()["n"]
        # 已入账的重跑请求重放：走幂等短路，不重复扣减
        rerun_replay = await svc.submit_execution(
            container, _req("rerun-2", rerun_key=BUDGET_KEY, project=BUDGET_PROJECT))
        budget_n_after_replay = store.count_budget_today(BUDGET_KEY, today)
        check(
            "门禁⑤ 重跑预算遵守：第三笔拒入零幻影、预算账恰好 2 行、重放不重复扣减",
            denied and budget_n == budget_n_after_replay == 2
            and phantom is None and budget_jobs == 2
            and rerun_replay["replayed"] is True,
            f"denied={denied} budget={budget_n}→{budget_n_after_replay} "
            f"phantom={phantom is not None} budget_jobs={budget_jobs}",
        )
        evidence["budget"] = {
            "quota": 2, "ledger_rows": budget_n, "denied": denied,
            "phantom_row": phantom is not None,
        }
        # 排空预算/首跑遗留任务，避免污染后续故事的 FIFO 认领
        while (await svc.run_once(container)) is not None:
            pass

        # ── 门①配套：异常收口单列（error/timeout 不产出结构化 TestRun）──────
        err_submit = await svc.submit_execution(
            container, _req("req-missing-fixture", hint="no-such-fixture",
                            project=ERROR_PROJECT))
        err_summary = await svc.run_once(container)
        err_row = store.get_test_run_row(err_submit["run_id"])

        original_runner = container.adapters["runner"]
        slow = _SlowRunner()
        container.adapters["runner"] = slow
        config.limits.request_timeout_s = 0.05
        timeout_submit = await svc.submit_execution(
            container, _req("req-timeout", project=ERROR_PROJECT))
        timeout_summary = await svc.run_once(container)
        timeout_row = store.get_test_run_row(timeout_submit["run_id"])
        timeout_job = store.conn.execute(
            "SELECT status FROM jobs WHERE idempotency_key=?",
            ("idem-req-timeout",)).fetchone()
        config.limits.request_timeout_s = 60
        container.adapters["runner"] = original_runner
        exception_ok = (
            err_summary["outcome"] == RunStatus.ERROR.value
            and err_row["status"] == "error" and not err_row["raw_json"]
            and timeout_summary["outcome"] == RunStatus.TIMEOUT.value
            and timeout_row["status"] == "timeout" and not timeout_row["raw_json"]
            and timeout_job["status"] == "failed" and slow.canceled == ["slow-1"]
        )
        check(
            "门①配套 异常收口单列：error/timeout 各 1 笔均无结构化 TestRun，"
            "不混进成功率分母，适配器 cancel 回调到位",
            exception_ok,
            f"error={err_summary['outcome']}(raw={bool(err_row['raw_json'])}) "
            f"timeout={timeout_summary['outcome']}(raw={bool(timeout_row['raw_json'])}, "
            f"job={timeout_job['status']}) canceled={slow.canceled}",
        )
        evidence["exception_outcomes"] = {
            "error": 1, "timeout": 1, "canceled": 1,
            "caliber_note": "基础设施异常单列；门①成功率分母只含录制批量任务",
        }

        # ── 持久化与版本 ─────────────────────────────────────────────────────
        version_ok = store.get_meta("schema_version") == str(SCHEMA_VERSION) == "15"
        total_before = len(store.list_test_runs())
        data_dir = config.paths["data_dir"]
        store.close()
        reopened = bootstrap_store(data_dir)
        try:
            total_after = len(reopened.list_test_runs())
        finally:
            reopened.close()
        check("持久化：重开库账本无损且 schema v15 钉版（零 schema 变更）",
              version_ok and total_before == total_after,
              f"version={SCHEMA_VERSION} rows {total_before}→{total_after}")
        evidence["schema_version"] = SCHEMA_VERSION
        evidence["total_runs"] = total_after
    finally:
        try:
            store.close()
        except Exception:  # noqa: BLE001 - 持久化检查已关库时忽略
            pass
    return evidence


def _render_markdown(doc: dict) -> str:
    lines = [
        "# S3.5 S3 五门禁总验探针报告（A23，hermetic 机制门禁）", "",
        f"- 生成时间：{doc['generated_at']}",
        f"- 工作目录：`{doc['workdir']}`（tempfile 临时库，不碰 store/offline-demo）",
        f"- 盲标夹具：`tests/fixtures/attribution/labeled_cases.json`"
        f"（{doc['manifest']['n']} 例），封存哈希 `{doc['manifest']['seal']}`",
        f"- 规则版本：{doc['rule_version']}；Schema：v{doc['schema_version']}",
        "- 口径注记：门③ hermetic 口径=引擎非弃权率（1−needs_human 弃权率）；"
        "docs/06 生产口径为人工抽检第三人可复核比例，属上线后人工环节。", "",
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
    for item in doc["checks"]:
        lines.append(f"- {item['passed']} {item['name']}：{item['detail']}")
    ev = doc["eval"]
    lines += [
        "", "## 40 例盲标归因指标（门②③）", "",
        f"- n={ev['n']}，accuracy={ev['accuracy']:.3f}（门 {ACCURACY_GATE}）",
        f"- real_defect_recall={ev['real_defect_recall']:.3f}"
        f"（门 {REAL_DEFECT_RECALL_GATE}）",
        f"- misleading_rate={ev['misleading_rate']:.3f}"
        f"（门 {MISLEADING_RATE_GATE}，needs_human 弃权不计误导）",
        f"- abstention_rate={ev['abstention_rate']:.3f}，"
        f"evidence_sufficiency={ev['evidence_sufficiency']:.3f}"
        f"（门 {EVIDENCE_SUFFICIENCY_GATE}）",
        "", "## 执行段证据（门①④⑤，生产装配临时库）", "",
        f"- 批量：{doc['execution']['structured_n']}/{doc['execution']['batch_n']} "
        f"结构化产出，rate={doc['execution']['success_rate']:.2f}，"
        f"outcome 分布={doc['execution']['by_outcome']}",
        f"- 异常单列：{doc['exception_outcomes']}",
        f"- 预算：{doc['budget']}",
        f"- 装配链路归因旁证：{doc['assembled_attribution']['non_abstention_observation']:.3f}"
        "（非门禁口径，见上注记）",
        "",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    checks: list[dict[str, str]] = []
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "passed": "✅" if ok else "❌", "detail": detail})
        if not ok:
            failures.append(name)

    EVAL_DIR.mkdir(parents=True, exist_ok=True)

    # ── 第 0 步：盲标清单载入即封存，封存副本先于评测落盘（盲序留痕）──────────
    manifest = LabeledManifest.model_validate_json(FIXTURE.read_text(encoding="utf-8"))
    seal = seal_manifest(manifest)
    sealed_at = datetime.now(timezone.utc).isoformat()
    (EVAL_DIR / "s3-gate-sealed-attribution.json").write_text(
        json.dumps({
            "sealed_before_evaluation": True,
            "sealed_at": sealed_at,
            "seal_hash": seal,
            "manifest": manifest.model_dump(),
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    check(
        "盲序封存：40 例归因盲标先封存再评测，封存副本先于报告落盘",
        len(seal) == 16 and len(manifest.cases) == 40,
        f"seal={seal}，cases={len(manifest.cases)}",
    )

    # 封存之后才启动评测
    report = evaluate_attribution(manifest)
    gate_map = {g.name: g for g in report.gates}
    check(
        "门禁② 归因三指标：准确率/真缺陷 recall/误误导率全部过门",
        gate_map["归因准确率"].passed
        and gate_map["真缺陷recall"].passed
        and gate_map["误误导率"].passed,
        f"acc={report.accuracy:.3f}(门{ACCURACY_GATE}) "
        f"recall={report.real_defect_recall:.3f}(门{REAL_DEFECT_RECALL_GATE}) "
        f"misleading={report.misleading_rate:.3f}(门{MISLEADING_RATE_GATE})",
    )
    check(
        "混淆矩阵 36 格齐全且总数=40，矩阵零格显式列 0",
        len(report.confusion) == 36 and sum(report.confusion.values()) == 40,
        f"cells={len(report.confusion)} sum={sum(report.confusion.values())}",
    )
    check(
        f"门禁③ 证据充分率：引擎非弃权率 {report.evidence_sufficiency:.3f} "
        f"≥ {EVIDENCE_SUFFICIENCY_GATE}（人工抽检口径见报告注记）",
        gate_map["证据充分率"].passed,
        f"sufficiency={report.evidence_sufficiency:.3f} "
        f"abstention={report.abstention_rate:.3f}",
    )

    # 红证对照：故意把一条真缺陷预测翻成 flaky（非缺陷收口），两个门禁必须被咬
    tampered_labels = [FailureCategory.REAL_DEFECT] * 3
    tampered_preds = [
        FailureCategory.FLAKY,           # 故意误导：缺陷收口成 flaky
        FailureCategory.REAL_DEFECT,
        FailureCategory.REAL_DEFECT,
    ]
    red_misleading = misleading_rate(tampered_labels, tampered_preds)
    red_recall = real_defect_recall(tampered_labels, tampered_preds)
    check(
        "红证对照：翻转 1 条缺陷预测后 recall 跌破 .95 且误导率超 .02（门禁有牙）",
        red_recall < REAL_DEFECT_RECALL_GATE and red_misleading > MISLEADING_RATE_GATE,
        f"red_recall={red_recall:.3f} red_misleading={red_misleading:.3f}",
    )

    # 空清单守卫活体：门禁不允许在空标注集上跑出"全过"
    empty_guard_alive = False
    try:
        evaluate_attribution(LabeledManifest())
    except ValueError:
        empty_guard_alive = True
    check("空清单守卫活体：空标注集评测被 ValueError 拒绝",
          empty_guard_alive, f"guard={'触发' if empty_guard_alive else '未触发'}")

    # 评测重放幂等 + 封存哈希稳定
    report_2 = evaluate_attribution(manifest)
    check(
        "评测重放幂等：同封存清单两次评测逐字段相等，重封哈希稳定",
        report.model_dump() == report_2.model_dump()
        and seal_manifest(manifest.model_copy(deep=True)) == seal
        and report.manifest_seal == seal,
        f"seal={seal}",
    )

    # ── 执行段（门①④⑤，生产装配 + 临时库）──────────────────────────────────
    evidence = asyncio.run(_execution_story(check))

    # ── 五门禁汇总表（docs/07 §10 顺序与口径）────────────────────────────────
    exec_ev = evidence["execution"]
    exc_ev = evidence["exception_outcomes"]
    gates = [
        {
            "index": 1, "name": "执行成功率", "threshold": "≥0.95",
            "actual": f"{exec_ev['structured_n']}/{exec_ev['batch_n']}="
                      f"{exec_ev['success_rate']:.2f}；异常单列 "
                      f"error={exc_ev['error']}/timeout={exc_ev['timeout']}"
                      f"/canceled={exc_ev['canceled']}（不进分母）",
            "result": "✅" if exec_ev["success_rate"] >= 0.95 else "❌",
        },
        {
            "index": 2,
            "name": "归因准确率 / 真缺陷 recall / 误误导率",
            "threshold": f"≥{ACCURACY_GATE} / ≥{REAL_DEFECT_RECALL_GATE} "
                         f"/ ≤{MISLEADING_RATE_GATE}",
            "actual": f"{report.accuracy:.3f} / {report.real_defect_recall:.3f} "
                      f"/ {report.misleading_rate:.3f}",
            "result": "✅" if (gate_map["归因准确率"].passed
                              and gate_map["真缺陷recall"].passed
                              and gate_map["误误导率"].passed) else "❌",
        },
        {
            "index": 3, "name": "证据充分率",
            "threshold": f"≥{EVIDENCE_SUFFICIENCY_GATE}",
            "actual": f"{report.evidence_sufficiency:.3f}"
                      f"（弃权率 {report.abstention_rate:.3f}）",
            "result": "✅" if gate_map["证据充分率"].passed else "❌",
        },
        {
            "index": 4, "name": "幂等正确性", "threshold": "100%",
            "actual": "提交/取消/预算重放零增生（run/job/账本/预算四路）",
            "result": "✅" if all(c["passed"] == "✅" for c in checks
                                  if c["name"].startswith("门禁④")) else "❌",
        },
        {
            "index": 5, "name": "重跑预算遵守", "threshold": "100%",
            "actual": f"配额 {evidence['budget']['quota']}，超配额拒入 "
                      f"{evidence['budget']['denied']} 笔、零幻影、"
                      f"账本 {evidence['budget']['ledger_rows']} 行",
            "result": "✅" if (evidence["budget"]["denied"]
                              and not evidence["budget"]["phantom_row"]
                              and evidence["budget"]["ledger_rows"] == 2) else "❌",
        },
    ]
    all_gates_ok = all(g["result"] == "✅" for g in gates)

    report_doc = {
        "probe": "s3_gate_probe",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "workdir": evidence["workdir"],
        "schema_version": evidence["schema_version"],
        "rule_version": report.rule_version,
        "manifest": {"fixture": str(FIXTURE.relative_to(SUBSYSTEMS)),
                     "n": report.n, "seal": seal},
        "gates": gates,
        "all_gates_passed": all_gates_ok,
        "eval": report.model_dump(mode="json"),
        "red_contrast": {
            "tampered_recall": red_recall,
            "tampered_misleading": red_misleading,
        },
        **{k: v for k, v in evidence.items() if k != "workdir"},
        "checks": checks,
    }
    (EVAL_DIR / "s3-gate-probe.json").write_text(
        json.dumps(report_doc, ensure_ascii=False, indent=2), encoding="utf-8")
    (EVAL_DIR / "s3-gate-probe.md").write_text(
        _render_markdown(report_doc), encoding="utf-8")

    for item in checks:
        print(f"{item['passed']} {item['name']} — {item['detail']}")
    print(f"报告：{EVAL_DIR / 's3-gate-probe.md'}")
    if failures or not all_gates_ok:
        print("❌ s3_gate_probe 存在失败检查或门禁未过", file=sys.stderr)
        return 1
    print(
        "✅ s3_gate_probe 全部通过：S3 五门禁总验"
        "（执行成功率/归因三指标/证据充分率/幂等正确性/重跑预算）"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
