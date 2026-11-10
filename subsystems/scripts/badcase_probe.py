#!/usr/bin/env python3
"""S2.6c 反馈/bad case 闭环端到端探针（docs/08 FR-EV-005）。

与单测的分工：test_badcase_core/store/service/api 用 tmp 库钉死每条分支；
本探针走**生产装配路径**（load_profile → build_container →
workers.badcase_service → SQLite bad_cases），在临时工作目录跑完整故事，
不碰 store/offline-demo、不起端口、不访问网络、无模型调用（纯确定性）。

故事线：
  ① 空反馈巡检返回全零报告（巡检语义，不报错）；
  ② 合成 qa_runs/feedback（👍 跳过、👎 投影、孤儿 run 防御），四元组快照
     从 qa_runs 关联补齐；
  ③ 幂等重放：已入账跳过，台账不增生；
  ④ 内容寻址 ID 独立验算（同 source/source_ref 指纹稳定）；
  ⑤ 全生命周期：分诊 accept → 转 golden（题集冻结+溯源+状态翻转）→
     幽灵回归 run 拒入 → fail 不闭环 → pass 闭环 fixed；
  ⑥ 非法迁移 fail-closed（promoted 再分诊、终态再迁移均拒）；
  ⑦ reject 路径不产生 golden 幻影；
  ⑧ 闭环统计：各态计数/闭环率/中位周期；
  ⑨ v13→v14 老库迁移钉版（DROP 新表+版本打回，重开自动补建、老反馈无损）；
  ⑩ 重开库台账/审计历史持久化无损。

报告落 out/eval/badcase-probe.{json,md}（out/ 已 gitignore）。任一断言不符 exit 1。

用法（在 subsystems/ 下）：
  .venv/bin/python scripts/badcase_probe.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages" / "workers" / "src"))

from testpilot_adapters.store import bootstrap_store  # noqa: E402
from testpilot_core import SCHEMA_VERSION  # noqa: E402
from testpilot_core.badcase import (  # noqa: E402
    GOLDEN_LOCKED_VERSION,
    BadCaseError,
    bad_case_id,
    new_bad_case,
)
from testpilot_core.enums import FeedbackKind, FeedbackReason  # noqa: E402
from testpilot_core.errors import NotFoundError  # noqa: E402
from testpilot_core.models import EvalRun, EvalVersion, Feedback  # noqa: E402
from testpilot_orchestrator.config_loader import load_profile  # noqa: E402
from testpilot_orchestrator.factory import build_container  # noqa: E402
from testpilot_workers import badcase_service as svc  # noqa: E402

SUBSYSTEMS = Path(__file__).resolve().parents[1]
PROFILE = SUBSYSTEMS / "config" / "profiles" / "offline-demo.yaml"
EVAL_DIR = SUBSYSTEMS / "out" / "eval"


class _ShimContainer:
    def __init__(self, store):
        self._store = store

    def get(self, name):
        return self._store if name == "store" else None


def _seed_qa_run(store, run_id: str, *, question: str) -> None:
    store.insert_qa_run(record={
        "run_id": run_id, "question": question, "status": "succeeded",
        "data_version": "d7", "model_version": "m3",
        "prompt_version": "p2", "params_version": "x1"})


def _seed_feedback(store, *, run_id: str, kind: FeedbackKind,
                   reason: FeedbackReason | None = None,
                   detail: str = "") -> int:
    return store.add_feedback(Feedback(
        run_id=run_id, target_urn=f"urn:testpilot:qa:run:{run_id}",
        kind=kind, reason=reason, detail=detail, model_version="m3"))


def main() -> int:
    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="badcase-probe-", dir=EVAL_DIR))
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
        # ① 空反馈巡检
        empty_report = svc.ingest_feedback(container)
        check("① 空反馈库巡检返回全零报告不报错",
              empty_report["scanned"] == 0 and empty_report["ingested"] == 0
              and empty_report["bad_case_ids"] == [],
              json.dumps(empty_report, ensure_ascii=False))

        # ② 合成反馈：1 条 👍、2 条 👎（其中 1 条孤儿 run）
        _seed_qa_run(store, "run-1", question="闹钟响铃时如何快速停掉？")
        _seed_qa_run(store, "run-2", question="重复闹钟的入口在哪？")
        fb_useful = _seed_feedback(
            store, run_id="run-1", kind=FeedbackKind.USEFUL, detail="赞")
        fb_bad1 = _seed_feedback(
            store, run_id="run-1", kind=FeedbackKind.NOT_USEFUL,
            reason=FeedbackReason.STALE_REFERENCE, detail="引用旧版停闹钟逻辑")
        fb_bad2 = _seed_feedback(
            store, run_id="run-2", kind=FeedbackKind.NOT_USEFUL,
            reason=FeedbackReason.FACTUAL_ERROR, detail="入口答错")
        fb_orphan = _seed_feedback(
            store, run_id="run-orphan", kind=FeedbackKind.NOT_USEFUL,
            reason=FeedbackReason.OTHER, detail="qa_run 缺失的防御分支")
        report = svc.ingest_feedback(container)
        cases = store.list_bad_cases()
        orphan_case = store.get_bad_case_by_feedback(str(fb_orphan))
        check("② 👎 投影/👍 跳过/孤儿防御，快照从 qa_runs 补齐",
              report["scanned"] == 4 and report["ingested"] == 3
              and report["skipped_useful"] == 1 and len(cases) == 3
              and orphan_case is not None and orphan_case.question == ""
              and orphan_case.snapshot.model_version == "m3",
              json.dumps({k: report[k] for k in
                          ("scanned", "ingested", "skipped_useful")},
                         ensure_ascii=False))
        c1 = store.get_bad_case_by_feedback(str(fb_bad1))
        snap_ok = c1.snapshot.model_dump() == {
            "data_version": "d7", "model_version": "m3",
            "prompt_version": "p2", "params_version": "x1"}
        check("②b 四元组快照与 question 来自 qa_runs 关联",
              snap_ok and c1.question == "闹钟响铃时如何快速停掉？"
              and c1.reason == "stale_reference",
              json.dumps(c1.snapshot.model_dump(), ensure_ascii=False))

        # ③ 幂等重放
        replay = svc.ingest_feedback(container)
        check("③ 巡检重放幂等：已入账全跳过、台账不增生",
              replay["ingested"] == 0 and replay["skipped_duplicate"] == 3
              and len(store.list_bad_cases()) == 3,
              json.dumps({k: replay[k] for k in
                          ("ingested", "skipped_duplicate")}, ensure_ascii=False))

        # ④ 内容寻址 ID 独立验算（不与服务共享调用路径）
        expected_id = bad_case_id(
            "qa_feedback", f"run-1#feedback-{fb_bad1}")
        useful_no_case = store.get_bad_case_by_feedback(str(fb_useful)) is None
        check("④ bad_case_id 内容寻址确定且 👍 无台账条目",
              c1.bad_case_id == expected_id and useful_no_case,
              f"actual={c1.bad_case_id} expected={expected_id}")

        # ⑤ 全生命周期（c1：accept → promote → 回归 fail → pass）
        accepted = svc.triage_case(
            container, c1.bad_case_id, decision="accept", actor="qa-li",
            reason="factual_error", note="本地复现")
        promoted_bundle = svc.promote_case(
            container, c1.bad_case_id, actor="qa-li", note="纳入回归集")
        golden_id = promoted_bundle["bad_case"].golden_case_id
        eval_row = store.conn.execute(
            "SELECT * FROM eval_cases WHERE case_id=?", (golden_id,)).fetchone()
        gt = json.loads(eval_row["ground_truth"])
        ghost_blocked = False
        try:
            svc.associate_regression_run(
                container, c1.bad_case_id, eval_run_id="eval-ghost",
                outcome="pass", actor="ci-bot")
        except NotFoundError:
            ghost_blocked = True
        store.add_eval_run(EvalRun(
            eval_run_id="eval-42",
            version=EvalVersion(data_version="d8", model_version="m4",
                                prompt_version="p3", params_version="x1")))
        failed = svc.associate_regression_run(
            container, c1.bad_case_id, eval_run_id="eval-42",
            outcome="fail", actor="ci-bot")
        store.add_eval_run(EvalRun(
            eval_run_id="eval-43",
            version=EvalVersion(data_version="d9", model_version="m4",
                                prompt_version="p3", params_version="x1")))
        fixed = svc.associate_regression_run(
            container, c1.bad_case_id, eval_run_id="eval-43",
            outcome="pass", actor="ci-bot")
        check("⑤ 全生命周期：golden 冻结/幽灵拒入/fail 不闭环/pass 闭环",
              accepted.status == "accepted"
              and promoted_bundle["bad_case"].status == "promoted"
              and eval_row["kind"] == "bad_case"
              and eval_row["locked_version"] == GOLDEN_LOCKED_VERSION
              and gt["provenance"]["bad_case_id"] == c1.bad_case_id
              and ghost_blocked
              and failed.status == "regression_failed" and failed.closed_at is None
              and fixed.status == "fixed" and fixed.closed_at is not None
              and fixed.regression_eval_run_id == "eval-43",
              f"ghost_blocked={ghost_blocked} final={fixed.status}")

        # ⑥ 非法迁移 fail-closed
        re_triage_blocked = False
        try:
            svc.triage_case(container, c1.bad_case_id,
                            decision="reject", actor="qa-li")
        except BadCaseError:
            re_triage_blocked = True
        c2 = store.get_bad_case_by_feedback(str(fb_bad2))
        rejected = svc.triage_case(
            container, c2.bad_case_id, decision="reject", actor="qa-wang",
            note="与本工程无关")
        terminal_blocked = False
        try:
            svc.associate_regression_run(
                container, c2.bad_case_id, eval_run_id="eval-43",
                outcome="pass", actor="ci-bot")
        except BadCaseError:
            terminal_blocked = True
        check("⑥ 非法迁移 fail-closed：promoted 不可再分诊、rejected 终态不可迁移",
              re_triage_blocked and rejected.is_closed and terminal_blocked,
              f"re_triage={re_triage_blocked} terminal={terminal_blocked}")

        # ⑦ reject 无 golden 幻影
        bad_case_rows = store.conn.execute(
            "SELECT COUNT(*) AS n FROM eval_cases WHERE kind='bad_case'"
        ).fetchone()["n"]
        check("⑦ 仅 accepted→promoted 产生 golden，reject 路径零幻影",
              bad_case_rows == 1 and rejected.golden_case_id == "",
              f"bad_case_eval_rows={bad_case_rows}")

        # ⑧ 闭环统计
        stats = svc.bad_case_stats(container)
        check("⑧ 统计口径：总量/各态/闭环率/中位周期可算",
              stats["total"] == 3
              and stats["by_status"]["fixed"] == 1
              and stats["by_status"]["rejected"] == 1
              and stats["by_status"]["open"] == 1
              and stats["closed_count"] == 2 and stats["open_count"] == 1
              and stats["closure_rate"] == round(2 / 3, 6)
              and stats["median_cycle_hours"] is not None,
              json.dumps(stats, ensure_ascii=False))

        store.close()
        main_store_closed = True

        # ⑨ v13→v14 迁移钉版（独立 legacy 库，不污染主库）
        legacy = bootstrap_store(work / "legacy-data")
        try:
            legacy.add_feedback(Feedback(
                run_id="run-old", target_urn="urn:testpilot:qa:run:run-old",
                kind=FeedbackKind.NOT_USEFUL, reason=FeedbackReason.OFF_TOPIC))
            legacy.conn.execute("DROP TABLE bad_cases")
            legacy.conn.execute(
                "UPDATE meta SET value='13' WHERE key='schema_version'")
            legacy.conn.commit()
        finally:
            legacy.close()
        upgraded = bootstrap_store(work / "legacy-data")
        try:
            tables = {r["name"] for r in upgraded.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            old_fb = upgraded.feedback_summary()
            version_ok = upgraded.get_meta("schema_version") == str(SCHEMA_VERSION) == "14"
            # 升级后立即可写
            upgraded.insert_bad_case(new_bad_case(
                source="manual_review", source_ref="INC-9#c1",
                title="迁移后写入冒烟",
                at=datetime(2026, 11, 10, tzinfo=timezone.utc)))
            write_ok = upgraded.list_bad_cases() != []
        finally:
            upgraded.close()
        check("⑨ v13→v14 自动补建 bad_cases、老反馈无损、升级后可写",
              version_ok and "bad_cases" in tables
              and any(r["kind"] == "not_useful" for r in old_fb) and write_ok,
              f"version={SCHEMA_VERSION} tables={'bad_cases' in tables} write={write_ok}")

        # ⑩ 重开主库：台账与审计历史无损
        reopened = bootstrap_store(config.paths["data_dir"])
        try:
            re_cases = reopened.list_bad_cases()
            re_c1 = reopened.get_bad_case(c1.bad_case_id)
            history_statuses = [e.to_status for e in re_c1.history]
        finally:
            reopened.close()
        check("⑩ 重开库台账/状态/审计历史持久化无损",
              len(re_cases) == 3 and re_c1.status == "fixed"
              and history_statuses[0] == "open" and history_statuses[-1] == "fixed"
              and len(history_statuses) >= 5,
              f"cases={len(re_cases)} history={history_statuses}")
    finally:
        if not main_store_closed:
            store.close()

    passed = sum(1 for c in checks if c["passed"] == "✅")
    report_doc = {
        "probe": "badcase_probe",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "workdir": str(work),
        "schema_version": SCHEMA_VERSION,
        "summary": {"passed": passed, "failed": len(failures), "total": len(checks)},
        "checks": checks,
    }
    (EVAL_DIR / "badcase-probe.json").write_text(
        json.dumps(report_doc, ensure_ascii=False, indent=2), encoding="utf-8")
    md_lines = [
        "# S2.6c bad case 闭环探针报告（badcase_probe / FR-EV-005）",
        "",
        f"生成时间：{report_doc['generated_at']}",
        f"工作目录：`{work}`",
        f"Schema 版本：v{SCHEMA_VERSION}",
        "",
        "| # | 检查 | 结果 | 证据 |",
        "|---|---|---|---|",
    ]
    md_lines += [
        f"| {i} | {c['name']} | {c['passed']} | {c['detail']} |"
        for i, c in enumerate(checks, 1)
    ]
    (EVAL_DIR / "badcase-probe.md").write_text(
        "\n".join(md_lines) + "\n", encoding="utf-8")

    for c in checks:
        print(f"{c['passed']} {c['name']} — {c['detail']}")
    print(f"报告：{EVAL_DIR / 'badcase-probe.json'}")
    if failures:
        print("❌ badcase_probe 存在失败检查", file=sys.stderr)
        return 1
    print("✅ badcase_probe 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
