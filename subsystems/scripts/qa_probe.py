#!/usr/bin/env python3
"""S1.4 离线问答端到端探针（docs/08 FR-QA-001～004，offline-demo profile 专用）。

与单测的分工：单测用 fake/ tmp 仓验证节点与图的每条分支；本探针用生产装配路径
（build_container → run_ingestion 真实入库 → build_qa_service 起 AsyncSqliteSaver）
跑四类问题，验证「录制模型 + 真实 FTS 检索 + 真实 sqlite 落库」整条链路。

四类问题（确定性，全靠录制夹具，不访问网络/真模型）：
  ① code   代码类问题引用离线语料不存在的代码块 → 重生成 1 次仍失败 → 未知
           （无引用结论率=0 的守卫链；三态中的 unknown）；
  ② incident 事故类 → L2 文档逐字证词与 L1 当前结论双段、URN 不交叉（FR-QA-003）；
  ③ unanswerable 模型降级信封（data=None）→ 未知/model_unavailable；
  ④ chitchat 闲聊在 ACL 节点收口 → 未知/out_of_scope，零生成零检索留痕。

自证（行数差，不是存在性断言）：qa_runs 恰好 +4、feedback 恰好 +2；
孤儿反馈（幽灵 run_id）必须被 NotFoundError 拒掉且不留行。任一不符 exit 1。

用法（在 subsystems/ 下）：
  TESTPILOT_PROFILE=config/profiles/offline-demo.yaml \\
    .venv/bin/python scripts/qa_probe.py
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages" / "workers" / "src"))

from testpilot_core.enums import FeedbackKind, FeedbackReason, QaStatus  # noqa: E402
from testpilot_core.errors import NotFoundError  # noqa: E402
from testpilot_core.models import AskRequest  # noqa: E402
from testpilot_orchestrator.qa_graph.service import build_qa_service  # noqa: E402
from testpilot_workers._bootstrap import bootstrap  # noqa: E402
from testpilot_workers.ingestion import run_ingestion  # noqa: E402


@dataclass
class QaCase:
    name: str
    case_key: str
    question: str
    expect_status: QaStatus
    expect_reason: str = ""


CASES = [
    QaCase(
        name="code 守卫链（引用白名单外代码块→重生成耗尽→未知）",
        case_key="stale_code",
        question="C.keep() 方法的实现逻辑是什么？",
        expect_status=QaStatus.UNKNOWN,
        expect_reason="citation_failed_exhausted",
    ),
    QaCase(
        name="incident 事故双段（L2 逐字证词 + L1 当前结论）",
        # 题面刻意同时覆盖两份语料的三元串（侧边键/停钟/兜底/kill-backup），
        # 让 L2 sidebar-fallback 与 L1 kill-backup 同时进证据集
        case_key="kill_backup",
        question="侧边键停钟失效的兜底机制是怎么演进的？现在 kill-backup 多少秒？",
        expect_status=QaStatus.ANSWERED,
    ),
    QaCase(
        name="unanswerable 模型降级信封→未知",
        case_key="unanswerable",
        question="kill-backup 的告警阈值如何在生产环境配置？",
        expect_status=QaStatus.UNKNOWN,
        expect_reason="model_unavailable",
    ),
    QaCase(
        name="chitchat 闲聊 ACL 收口",
        case_key="chitchat",
        question="你好，讲个笑话",
        expect_status=QaStatus.UNKNOWN,
        expect_reason="out_of_scope",
    ),
]


def _count(store, table: str) -> int:
    row = store.conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
    return int(row["n"])


async def amain() -> int:
    container = bootstrap("qa-probe")
    store = container.get("store")
    failures: list[str] = []

    # 1) 确定性入库：full=True 忽略 hash 对账，探针重复运行结果一致（仍幂等）
    report = await run_ingestion(container, trigger="qa-probe", full=True)
    print(f"[ingest] docs seen={report.seen} chunks={report.chunks_total} "
          f"created={report.created} updated={report.updated} failed={report.failed}")
    if report.failed:
        failures.append(f"入库存在失败文档 {report.failed} 个")

    service = await build_qa_service(container)
    runs_before = _count(store, "qa_runs")
    feedback_before = _count(store, "feedback")
    run_ids: list[str] = []
    try:
        # 2) 四类问题逐条核对状态/原因，并展示三态标注与引用
        for case in CASES:
            resp = await service.ask(
                AskRequest(question=case.question,
                           project=container.config.project.name,
                           branch=container.config.project.default_branch),
                case_key=case.case_key,
            )
            run_ids.append(resp.run_id)
            print(f"\n=== {case.name} ===")
            print(f"  run_id={resp.run_id} status={resp.status.value} "
                  f"reason={resp.unknown_reason or '-'} intent={resp.intent or '-'}")
            if resp.answer:
                for c in resp.answer.citations:
                    print(f"  cite [{c.version_state}:{c.version_ref or '-'}] {c.urn}")
                for h in resp.answer.history:
                    print(f"  history(L2) {h.doc_urn}: {h.text[:48]}…")
            if resp.status != case.expect_status:
                failures.append(
                    f"{case.name}: 状态 {resp.status.value} ≠ {case.expect_status.value}")
            if case.expect_reason and resp.unknown_reason != case.expect_reason:
                failures.append(
                    f"{case.name}: 原因 {resp.unknown_reason!r} ≠ {case.expect_reason!r}")

        # 3) FR-QA-003 结构守卫：incident 回答必须双段且历史/引用 URN 不交叉
        incident = await service.get_run(run_ids[1])
        payload = incident["answer_payload"]
        markdown = payload.get("markdown", "")
        if "## 历史证词（逐字引用，未经模型改写）" not in markdown \
                or "## 当前结论" not in markdown:
            failures.append("incident：缺少历史证词/当前结论双段标题")
        history_docs = {h["doc_urn"] for h in payload.get("history", [])}
        cite_docs = {c["urn"] for c in payload.get("citations", [])}
        if not history_docs:
            failures.append("incident：L2 历史证词为空（检索未召回 sidebar-fallback）")
        if history_docs & cite_docs:
            failures.append(f"incident：历史/引用 URN 交叉 {history_docs & cite_docs}")

        # 4) FR-QA-004 反馈：两条合法反馈落库 + 一条孤儿反馈被拒
        fb_id_1 = await service.record_feedback(run_ids[1], FeedbackKind.USEFUL)
        fb_id_2 = await service.record_feedback(
            run_ids[0], FeedbackKind.NOT_USEFUL, reason=FeedbackReason.STALE_REFERENCE,
            detail="探针：引用了库内不存在的代码块", model_version="recorded-local")
        try:
            await service.record_feedback("run-ghost-never-exists", FeedbackKind.USEFUL)
            failures.append("孤儿反馈未被 NotFoundError 拦截")
        except NotFoundError:
            pass
        print(f"\n[feedback] 写入 id={fb_id_1},{fb_id_2}；幽灵 run 已被拒绝")
    finally:
        await service.aclose()

    # 5) 行数差自证（在连接关闭后查，顺带验证 checkpoint/账本连接均正常收口）
    runs_after = _count(store, "qa_runs")
    feedback_after = _count(store, "feedback")
    print(f"\n[ledger] qa_runs {runs_before}→{runs_after}（Δ={runs_after - runs_before}），"
          f"feedback {feedback_before}→{feedback_after}（Δ={feedback_after - feedback_before}）")
    if runs_after - runs_before != len(CASES):
        failures.append(f"qa_runs 行数差 {runs_after - runs_before} ≠ {len(CASES)}")
    if feedback_after - feedback_before != 2:
        failures.append(f"feedback 行数差 {feedback_after - feedback_before} ≠ 2")
    ghost = store.conn.execute(
        "SELECT COUNT(*) AS n FROM feedback WHERE run_id=?", ("run-ghost-never-exists",)
    ).fetchone()["n"]
    if ghost != 0:
        failures.append("孤儿反馈在库里留下了行")

    store.close()
    print("\n" + ("✅ qa_probe 全部通过" if not failures else "❌ qa_probe 失败："))
    for f in failures:
        print(f"  - {f}")
    return 1 if failures else 0


def main() -> None:
    sys.exit(asyncio.run(amain()))


if __name__ == "__main__":
    main()
