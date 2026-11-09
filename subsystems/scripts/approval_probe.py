#!/usr/bin/env python3
"""S2.5 人审回写端到端探针（docs/08 FR-TD-004 / FR-WR-001 / FR-UI-002 后端链路）。

与单测的分工：test_approval_* 用 tmp 仓钉死每条分支；本探针走**生产装配路径**
（load_profile → build_container → workers 审批服务 → outbox 派发 → MarkdownCaseRepository
真实落盘 → S2.1 解析器回读），在临时工作目录内跑完整故事，不碰 store/offline-demo、
不碰语料仓、不起端口、不访问网络。

故事线（确定性，无模型调用）：
  ① 灌 1 个缺预期用例 + 2 条 pending 草稿 → 队列分组正确；
  ② 人审 approved：s1 采纳、s2 行内修改 → 单事务落四件套，偏好样本 1 条；
  ③ AC1 铁证：审批前后正式 test_cases 表 raw_json 逐行快照完全一致；
  ④ profile 默认关写 → 派发 blocked，不烧 attempts、写根零文件；
  ⑤ 模拟 profile 开启外写（测试接缝）→ 同一 outbox 派发 confirmed，文件落独立写根；
  ⑥ 落盘 Markdown 经 S2.1 解析器无损 round-trip（显式类型标签保住断言类型）；
  ⑦ 重放幂等：再派发零扫描，适配器直写同键返回同 external_ref、manifest 不增生；
  ⑧ 第二个用例整案 rejected → 无 outbox、无文件、草稿 rejected；
  ⑨ kill switch 运行期收紧 → 适配器直写被 PolicyDenied 拦截，恢复后放行。

报告落 out/eval/approval-probe.{json,md}，回写样例落
out/eval/approval-probe-writeback-sample.md（out/ 已 gitignore）。
任一断言不符 exit 1。

用法（在 subsystems/ 下）：
  .venv/bin/python scripts/approval_probe.py
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages" / "workers" / "src"))

from testpilot_adapters.case_repo import MANIFEST_NAME  # noqa: E402
from testpilot_core.approval import (  # noqa: E402
    CASE_REPO_TARGET_SYSTEM,
    CaseDecisionRequest,
    SuggestionDecision,
)
from testpilot_core.enums import (  # noqa: E402
    ApprovalDecision,
    OracleType,
    OutboxStatus,
)
from testpilot_core.errors import PolicyDeniedError  # noqa: E402
from testpilot_core.models import CaseSelector, TestCase  # noqa: E402
from testpilot_core.oracle_mining import MinedOracle  # noqa: E402
from testpilot_core.testcase import parse_case_markdown  # noqa: E402
from testpilot_orchestrator.config_loader import load_profile  # noqa: E402
from testpilot_orchestrator.factory import build_container  # noqa: E402
from testpilot_workers.approval_service import (  # noqa: E402
    get_review_queue,
    review_case,
)
from testpilot_workers.outbox_dispatch import dispatch_pending_outbox  # noqa: E402

SUBSYSTEMS = Path(__file__).resolve().parents[1]
PROFILE = SUBSYSTEMS / "config" / "profiles" / "offline-demo.yaml"
EVAL_DIR = SUBSYSTEMS / "out" / "eval"

_DOC_URN = "urn:testpilot:fs-cases:document:cases/synthetic-cases.md"
_CASE_URN = "urn:testpilot:fs-cases:test_case:cases/synthetic-cases.md#TC-N2"
_CASE2_URN = "urn:testpilot:fs-cases:test_case:cases/synthetic-cases.md#TC-N3"
_FR1_URN = "urn:testpilot:fs-reqs:requirement:req.md#FR-1"
_FR2_URN = "urn:testpilot:fs-reqs:requirement:req.md#FR-2"


def _seed(store, case_urn: str, external_id: str, suggestions: list[MinedOracle]) -> None:
    store.upsert_test_case(
        _DOC_URN,
        TestCase(
            urn=case_urn, external_id=external_id, title=f"{external_id} 缺预期草稿",
            steps=["打开 App", "触发闹钟"], tags=["FR-1"],
            module="模块 A 叙事体", priority="P1", oracle_missing=True),
        content_hash=f"h-{external_id}",
    )
    for mined in suggestions:
        store.upsert_oracle_suggestion(
            _DOC_URN, case_urn, external_id, mined, content_hash=f"h-{external_id}")


def _formal_snapshot(store) -> dict[str, str]:
    """AC1 证据：正式用例库全量 raw_json 快照（审批流永不许改这张表）。"""
    rows = store.conn.execute("SELECT urn, raw_json FROM test_cases").fetchall()
    return {r["urn"]: r["raw_json"] for r in rows}


async def amain() -> int:
    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="approval-probe-", dir=EVAL_DIR))
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
    repo = container.get("case_repo")
    write_root = container.data_dir / "case-writeback"

    try:
        # ① 种子：TC-N2 两条草稿（确定性状态断言 + 模型 UI 断言）
        _seed(store, _CASE_URN, "TC-N2", [
            MinedOracle(type=OracleType.STATE_ASSERTION, expectation="落库一条 stopped 记录",
                        source_kind="requirement_ac", provenance="deterministic",
                        source_refs=[_FR1_URN], confidence=0.7),
            MinedOracle(type=OracleType.UI_ASSERTION, expectation="页面展示闹钟提醒卡片",
                        source_kind="model_proposed", provenance="model",
                        source_refs=[_FR2_URN], confidence=0.5),
        ])
        queue = get_review_queue(container)
        check("① 队列分组", len(queue) == 1 and len(queue[0].suggestions) == 2,
              f"queue_items={len(queue)} suggestions={len(queue[0].suggestions) if queue else 0}")
        # 队列按 URN 排序、与种子顺序无关：用期望文本反查，保证 adopt/modify 绑定确定
        by_expectation = {s.oracle.expectation: s for s in queue[0].suggestions}
        s1_urn = by_expectation["落库一条 stopped 记录"].urn  # 状态断言→采纳
        s2_urn = by_expectation["页面展示闹钟提醒卡片"].urn  # UI 断言→修改

        # ② 人审：采纳 s1、行内修改 s2（期望文本改写，类型保持 UI）
        before = _formal_snapshot(store)
        plan = review_case(container, CaseDecisionRequest(
            case_urn=_CASE_URN, decision=ApprovalDecision.APPROVED,
            reviewer="expert-zhang", reason="探针：采纳+修改各一条",
            items=[
                SuggestionDecision(suggestion_urn=s1_urn, action="adopt"),
                SuggestionDecision(suggestion_urn=s2_urn, action="modify",
                                   oracle_type=OracleType.UI_ASSERTION,
                                   expectation="前台弹出全屏提醒卡片并持续响铃"),
            ]))
        samples = store.list_preference_samples()
        outbox_row = store.get_outbox_record(plan.idempotency_key)
        check("② 审批四件套落库",
              outbox_row is not None and outbox_row.status == OutboxStatus.PENDING
              and len(plan.suggestion_updates) == 2 and len(samples) == 1,
              f"outbox={outbox_row.status.value if outbox_row else None} "
              f"updates={len(plan.suggestion_updates)} preference_samples={len(samples)}")
        check("② 偏好样本记录字段 diff",
              samples[0].diff.get("expectation", {}).get("to") == "前台弹出全屏提醒卡片并持续响铃",
              f"diff={json.dumps(samples[0].diff, ensure_ascii=False)}")
        check("② confirmed 置信度 +0.3 封顶",
              [(o.type.value, o.confidence) for o in plan.final_oracles] == [
                  ("state_assertion", 1.0), ("ui_assertion", 0.8)],
              f"final={[(o.type.value, o.confidence) for o in plan.final_oracles]}")
        check("② 评审后队列排空", get_review_queue(container) == [],
              f"left={len(get_review_queue(container))}")

        # ③ AC1：正式用例库零变更
        after = _formal_snapshot(store)
        check("③ AC1 正式 test_cases 零触碰", before == after,
              f"before_rows={len(before)} after_rows={len(after)}")

        # ④ profile 默认关写：派发 blocked，不烧 attempts、写根零文件
        blocked_report = await dispatch_pending_outbox(container)
        blocked_row = store.get_outbox_record(plan.idempotency_key)
        root_files = list(write_root.rglob("*")) if write_root.exists() else []
        check("④ kill switch 拦截派发",
              blocked_report["blocked"] == 1 and blocked_report["confirmed"] == 0
              and blocked_row.attempts == 0 and root_files == [],
              f"blocked={blocked_report['blocked']} attempts={blocked_row.attempts} "
              f"root_files={len(root_files)}")

        # ⑤ 测试接缝：模拟 profile 开启外写，同一 outbox 转正
        container.write_policy.profile_enabled = True
        confirmed_report = await dispatch_pending_outbox(container)
        confirmed_row = store.get_outbox_record(plan.idempotency_key)
        rel_path = "cases/synthetic-cases/TC-N2.md"
        target = write_root / rel_path
        check("⑤ 派发 confirmed 并落独立写根",
              confirmed_report["confirmed"] == 1 and confirmed_report["failed"] == 0
              and confirmed_row.status == OutboxStatus.CONFIRMED
              and confirmed_row.external_ref == f"{CASE_REPO_TARGET_SYSTEM}:{rel_path}"
              and target.exists(),
              f"confirmed={confirmed_report['confirmed']} ref={confirmed_row.external_ref}")
        check("⑤ 落盘字节=审批渲染字节",
              target.read_text(encoding="utf-8") == plan.markdown,
              f"path={target.relative_to(work)}")

        # ⑥ S2.1 解析器无损回读（生产读口径 list_cases + 纯函数双验证）
        readback = (await repo.list_cases(CaseSelector()))[0]
        parsed = parse_case_markdown(
            target.read_text(encoding="utf-8"),
            doc_external_id="cases/synthetic-cases.md")
        expected_pairs = [
            (OracleType.STATE_ASSERTION, "落库一条 stopped 记录"),
            (OracleType.UI_ASSERTION, "前台弹出全屏提醒卡片并持续响铃"),
        ]
        check("⑥ round-trip 类型与期望无损",
              [(o.type, o.expectation) for o in readback.oracles] == expected_pairs
              and [(o.type, o.expectation) for o in parsed.cases[0].oracles] == expected_pairs
              and readback.steps == ["打开 App", "触发闹钟"]
              and parsed.dead_letters == [],
              f"oracles={[(o.type.value, o.expectation) for o in readback.oracles]}")

        # ⑦ 重放幂等：队列已空零扫描；适配器直写同键同 ref、manifest 不增生
        drained = await dispatch_pending_outbox(container)
        replay_ref = await repo.write_approved_record(confirmed_row)
        manifest = json.loads((write_root / MANIFEST_NAME).read_text(encoding="utf-8"))
        md_files = list(write_root.glob("**/*.md"))
        check("⑦ 重放幂等不重复写",
              drained["scanned"] == 0 and replay_ref == confirmed_row.external_ref
              and len(manifest["entries"]) == 1 and len(md_files) == 1,
              f"scanned={drained['scanned']} manifest_entries={len(manifest['entries'])} "
              f"md_files={len(md_files)}")

        # ⑧ 整案拒绝：无 outbox 行、无新文件、草稿 rejected
        _seed(store, _CASE2_URN, "TC-N3", [
            MinedOracle(type=OracleType.MANUAL, expectation="人工确认提醒已关闭",
                        source_kind="existing_assertion", provenance="deterministic",
                        source_refs=[_FR1_URN], confidence=0.5)])
        n3_queue = get_review_queue(container)
        n3_urn = n3_queue[0].suggestions[0].urn
        outbox_before = len(store.list_outbox_records())
        reject_plan = review_case(container, CaseDecisionRequest(
            case_urn=_CASE2_URN, decision=ApprovalDecision.REJECTED,
            reviewer="expert-li", reason="探针：整案拒绝",
            items=[SuggestionDecision(suggestion_urn=n3_urn, action="reject")]))
        check("⑧ 整案拒绝无外部副作用",
              reject_plan.outbox is None
              and len(store.list_outbox_records()) == outbox_before
              and len(list(write_root.glob("**/*.md"))) == 1
              and get_review_queue(container) == [],
              f"outbox_rows={len(store.list_outbox_records())} "
              f"md_files={len(list(write_root.glob('**/*.md')))}")

        # ⑨ 运行期 kill switch 收紧→拦截→恢复放行（只收紧语义）
        container.write_policy.runtime_disabled = True
        blocked_direct = False
        try:
            await repo.write_approved_record(confirmed_row)
        except PolicyDeniedError:
            blocked_direct = True
        container.write_policy.runtime_disabled = False
        recovered_ref = await repo.write_approved_record(confirmed_row)
        check("⑨ kill switch 收紧即时生效且可恢复",
              blocked_direct and recovered_ref == confirmed_row.external_ref,
              f"blocked_direct={blocked_direct} recovered_ref={recovered_ref}")

        # 证据样例放到稳定路径供人工查看
        sample = EVAL_DIR / "approval-probe-writeback-sample.md"
        shutil.copyfile(target, sample)
    finally:
        store.close()

    passed = sum(1 for c in checks if c["passed"] == "✅")
    report = {
        "probe": "approval_probe",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "workdir": str(work),
        "write_root": str(write_root),
        "sample_markdown": str(EVAL_DIR / "approval-probe-writeback-sample.md"),
        "summary": {"passed": passed, "failed": len(failures), "total": len(checks)},
        "checks": checks,
    }
    (EVAL_DIR / "approval-probe.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_lines = [
        "# S2.5 人审回写探针报告（approval_probe）",
        "",
        f"生成时间：{report['generated_at']}",
        f"临时工作目录：`{work}`（hermetic，不碰 store/offline-demo 与语料仓）",
        f"回写样例：`{report['sample_markdown']}`",
        "",
        "| # | 检查 | 结果 | 证据 |",
        "|---|---|---|---|",
    ]
    md_lines += [
        f"| {i} | {c['name']} | {c['passed']} | {c['detail']} |"
        for i, c in enumerate(checks, start=1)]
    (EVAL_DIR / "approval-probe.md").write_text("\n".join(md_lines) + "\n", encoding="utf-8")

    print(f"工作目录：{work}")
    for c in checks:
        print(f"{c['passed']} {c['name']} — {c['detail']}")
    print(f"\n报告：out/eval/approval-probe.{{json,md}}；样例：out/eval/approval-probe-writeback-sample.md")
    if failures:
        print("❌ approval_probe 失败：")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("✅ approval_probe 全部通过")
    return 0


def main() -> None:
    sys.exit(asyncio.run(amain()))


if __name__ == "__main__":
    main()
