"""人审服务 + outbox 派发 worker 集成测试（S2.5 FR-TD-004 / FR-WR-001/003）。

不依赖完整容器：shim 只暴露 worker 实际使用的 get("store")/get("case_repo")，
全部状态落在 tmp SQLite，快速且无外部进程。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from testpilot_adapters.case_repo import MarkdownCaseRepository
from testpilot_adapters.store import bootstrap_store
from testpilot_core.approval import (
    CASE_REPO_TARGET_SYSTEM,
    CaseDecisionRequest,
    SuggestionDecision,
)
from testpilot_core.enums import (
    ActionLevel,
    ApprovalDecision,
    OracleType,
    OutboxStatus,
)
from testpilot_core.errors import NotFoundError, PolicyDeniedError
from testpilot_core.models import Approval, OutboxRecord, TestCase
from testpilot_core.oracle_mining import MinedOracle
from testpilot_workers.approval_service import get_review_queue, review_case
from testpilot_workers.outbox_dispatch import dispatch_pending_outbox

_NOW = datetime(2026, 11, 9, 8, 30, tzinfo=timezone.utc)
_DOC_URN = "urn:testpilot:fs-cases:document:cases/synthetic-cases.md"
_CASE_URN = "urn:testpilot:fs-cases:test_case:cases/synthetic-cases.md#TC-N2"
_CASE_URN_B = "urn:testpilot:fs-cases:test_case:cases/synthetic-cases.md#TC-B2"
_FR1_URN = "urn:testpilot:fs-reqs:requirement:req.md#FR-1"


class _ShimContainer:
    """worker 只依赖 get(name) 的最小容器，避免拉起整套 profile 装配。"""

    def __init__(self, store, case_repo=None):
        self._adapters = {"store": store}
        if case_repo is not None:
            self._adapters["case_repo"] = case_repo

    def get(self, name):
        return self._adapters.get(name)


def _case(urn: str = _CASE_URN, external_id: str = "TC-N2", **kw) -> TestCase:
    base = dict(
        urn=urn, external_id=external_id, title="只有步骤没有预期",
        steps=["打开 App", "触发闹钟"], tags=["FR-1"],
        module="模块 A", priority="P1", oracle_missing=True,
    )
    base.update(kw)
    return TestCase(**base)


def _mined(expectation: str = "落库一条 stopped 记录",
           otype=OracleType.STATE_ASSERTION) -> MinedOracle:
    return MinedOracle(
        type=otype, expectation=expectation,
        source_kind="requirement_ac", provenance="deterministic",
        source_refs=[_FR1_URN], confidence=0.5)


def _seed(store, *, case_urn=_CASE_URN, external_id="TC-N2", count=1):
    store.upsert_test_case(_DOC_URN, _case(case_urn, external_id), content_hash="h1")
    suggestions = []
    for i in range(count):
        mined = _mined(f"第 {i + 1} 条预期")
        store.upsert_oracle_suggestion(
            _DOC_URN, case_urn, external_id, mined, content_hash="h1")
        suggestions.append(store.list_pending_suggestions_for_case(case_urn)[i]["urn"])
    return suggestions


def _request(case_urn, items, *, decision=ApprovalDecision.APPROVED,
             reviewer="expert-zhang", reason="评审通过"):
    return CaseDecisionRequest(
        case_urn=case_urn, decision=decision, reviewer=reviewer,
        reason=reason, items=items)


def _formal_snapshot(store) -> list[tuple]:
    return [
        tuple(r) for r in store.conn.execute(
            "SELECT urn, raw_json FROM test_cases ORDER BY urn").fetchall()
    ]


# ── 队列组装 ────────────────────────────────────────────────────────────────

def test_queue_only_contains_pending_grouped_by_case(tmp_path):
    store = bootstrap_store(tmp_path)
    try:
        urns_a = _seed(store, count=2)
        _seed(store, case_urn=_CASE_URN_B, external_id="TC-B2")
        # 案 A 评掉一条：队列里案 A 只剩 1 条，案 B 不受影响
        store.mark_suggestion_reviewed(
            urns_a[0], status="approved", approval_id="ap-old",
            reviewer="expert-zhang", reason="早前评审",
            reviewed_at=_NOW.isoformat())

        queue = get_review_queue(_ShimContainer(store))
        assert [i.external_id for i in queue] == ["TC-B2", "TC-N2"]
        item_a = next(i for i in queue if i.external_id == "TC-N2")
        assert len(item_a.suggestions) == 1
        assert item_a.suggestions[0].urn == urns_a[1]
        assert item_a.oracle_missing is True
    finally:
        store.close()


def test_review_unknown_case_is_not_found(tmp_path):
    store = bootstrap_store(tmp_path)
    try:
        req = _request(
            "urn:ghost",
            [SuggestionDecision(suggestion_urn="urn:x:s1", action="adopt")])
        with pytest.raises(NotFoundError, match="待审用例不存在"):
            review_case(_ShimContainer(store), req)
    finally:
        store.close()


# ── 人审落账：四件套 + AC1 正式库零触碰 ─────────────────────────────────────

def test_adopt_persists_four_pieces_and_never_touches_formal_library(tmp_path):
    store = bootstrap_store(tmp_path)
    try:
        urns = _seed(store)
        before = _formal_snapshot(store)

        plan = review_case(
            _ShimContainer(store),
            _request(_CASE_URN, [
                SuggestionDecision(suggestion_urn=urns[0], action="adopt")]))

        # ① Approval：R2 + approved + 评审人/时间齐
        approval = store.get_approval(plan.approval.approval_id)
        assert approval is not None
        assert approval.action_level is ActionLevel.R2_REVIEWED_WRITE
        assert approval.decision is ApprovalDecision.APPROVED
        assert approval.reviewer == "expert-zhang"
        # ② outbox：pending 待派发，幂等键内容寻址，回滚说明非空
        record = store.get_outbox_record(plan.idempotency_key)
        assert record is not None and record.status is OutboxStatus.PENDING
        assert record.rollback_note and record.approval_id == approval.approval_id
        assert record.request_body["markdown"] == plan.markdown
        # ③ 草稿状态翻转 + 四人审列
        row = store.conn.execute(
            "SELECT status, approval_id, reviewed_by, decision_reason "
            "FROM oracle_suggestions WHERE urn=?", (urns[0],)).fetchone()
        assert row["status"] == "approved"
        assert row["approval_id"] == approval.approval_id
        assert row["reviewed_by"] == "expert-zhang"
        # ④ 采纳不产生偏好样本
        assert store.list_preference_samples() == []
        # AC1：正式 test_cases 表逐行 raw_json 零变化
        assert _formal_snapshot(store) == before
    finally:
        store.close()


def test_modify_persists_preference_sample_and_reject_has_no_outbox(tmp_path):
    store = bootstrap_store(tmp_path)
    try:
        urns = _seed(store, count=2)

        modify_plan = review_case(_ShimContainer(store), _request(
            _CASE_URN,
            [SuggestionDecision(
                suggestion_urn=urns[0], action="modify",
                oracle_type=OracleType.LOG_ASSERTION, expectation="os_log 出现响铃行"),
             SuggestionDecision(suggestion_urn=urns[1], action="reject")],
            reason="第一条改类型，第二条方向不对"))
        samples = store.list_preference_samples()
        assert len(samples) == 1
        assert samples[0].diff["type"] == {"from": "state_assertion",
                                           "to": "log_assertion"}
        assert modify_plan.outbox is not None  # 仍有一条接受 → 有回写

        # 第二个用例整案拒绝：无 outbox、无样本，草稿全 rejected
        urns_b = _seed(store, case_urn=_CASE_URN_B, external_id="TC-B2")
        reject_plan = review_case(_ShimContainer(store), _request(
            _CASE_URN_B,
            [SuggestionDecision(suggestion_urn=u, action="reject") for u in urns_b],
            decision=ApprovalDecision.REJECTED, reason="整案方向错误"))
        assert reject_plan.outbox is None
        assert reject_plan.approval.decision is ApprovalDecision.REJECTED
        # 已评审草稿不再出现在待审队列
        assert [r for r in store.list_review_queue_rows()
                if r["case_urn"] == _CASE_URN_B] == []
        pending_b = store.conn.execute(
            "SELECT status FROM oracle_suggestions WHERE case_urn=?",
            (_CASE_URN_B,)).fetchall()
        assert {r["status"] for r in pending_b} == {"rejected"}
    finally:
        store.close()


def test_persist_is_atomic_double_review_rolls_back_everything(tmp_path):
    store = bootstrap_store(tmp_path)
    try:
        urns = _seed(store)
        plan = review_case(
            _ShimContainer(store),
            _request(_CASE_URN, [
                SuggestionDecision(suggestion_urn=urns[0], action="adopt")]))
        approvals_after_first = store.conn.execute(
            "SELECT COUNT(*) c FROM approvals").fetchone()["c"]

        # 重复落同一份计划（模拟双击穿透到持久层）：pending 守卫触发，整事务回滚，
        # 不允许出现「第二条 Approval 已插、草稿翻转失败」的交错态
        with pytest.raises(ValueError, match="草稿不存在或已评审"):
            store.persist_case_decision(
                plan, reviewed_at=_NOW.isoformat())
        assert store.conn.execute(
            "SELECT COUNT(*) c FROM approvals").fetchone()["c"] == approvals_after_first
        assert store.conn.execute(
            "SELECT COUNT(*) c FROM outbox").fetchone()["c"] == 1

        # 正常的第二次人审请求则在 core 层就被拦住（无 pending 可审）
        with pytest.raises(Exception, match="没有待审草稿"):
            review_case(
                _ShimContainer(store),
                _request(_CASE_URN, [
                    SuggestionDecision(suggestion_urn=urns[0], action="adopt")]))
    finally:
        store.close()


# ── outbox 派发 ─────────────────────────────────────────────────────────────

def _seed_approved_outbox(store, *, key="case-writeback:cases/x.md#TC-N2#aaa111bbb222"):
    store.save_approval(Approval(
        approval_id="ap-wb", action_level=ActionLevel.R2_REVIEWED_WRITE,
        target_system=CASE_REPO_TARGET_SYSTEM, request_summary="回写",
        decision=ApprovalDecision.APPROVED, reviewer="expert-zhang",
        decided_at=_NOW))
    record = OutboxRecord(
        idempotency_key=key, action_level=ActionLevel.R2_REVIEWED_WRITE,
        target_system=CASE_REPO_TARGET_SYSTEM,
        request_body={
            "doc_external_id": "cases/synthetic-cases.md",
            "case_external_id": "TC-N2",
            "markdown": "## 模块 A\n\n### TC-N2 只有步骤没有预期（FR-1） P1\n",
        },
        approval_id="ap-wb",
        rollback_note="删除写根文件并重置草稿",
    )
    store.add_outbox(record)
    return record


def test_dispatch_blocked_by_kill_switch_leaves_pending(tmp_path):
    store = bootstrap_store(tmp_path)
    repo = MarkdownCaseRepository(
        tmp_path / "writeback",
        approval_provider=store.get_approval, kill_switch=lambda: True)
    try:
        _seed_approved_outbox(store)
        report = asyncio.run(dispatch_pending_outbox(_ShimContainer(store, repo)))

        assert report["status"] == "succeeded" and report["blocked"] == 1
        assert report["confirmed"] == 0 and report["scanned"] == 1
        assert report["results"][0]["outcome"] == "blocked"
        record = store.pending_outbox_records()
        assert len(record) == 1 and record[0].attempts == 0  # 不烧重试次数
        assert list((tmp_path / "writeback").rglob("*")) == []  # 零外部副作用
    finally:
        store.close()


def test_dispatch_confirmed_then_queue_drains(tmp_path):
    store = bootstrap_store(tmp_path)
    repo = MarkdownCaseRepository(
        tmp_path / "writeback",
        approval_provider=store.get_approval, kill_switch=lambda: False)
    try:
        record = _seed_approved_outbox(store)
        report = asyncio.run(dispatch_pending_outbox(_ShimContainer(store, repo)))

        assert report["confirmed"] == 1 and report["dispatched"] == 1
        assert report["results"][0]["external_ref"].endswith(
            "cases/synthetic-cases/TC-N2.md")
        got = store.get_outbox_record(record.idempotency_key)
        assert got.status is OutboxStatus.CONFIRMED
        assert got.external_ref and got.attempts == 1 and got.error == ""
        assert (tmp_path / "writeback" / "cases/synthetic-cases/TC-N2.md").exists()

        # 队列已排空：再跑一次零扫描，不重复写
        second = asyncio.run(dispatch_pending_outbox(_ShimContainer(store, repo)))
        assert second["scanned"] == 0 and second["confirmed"] == 0
    finally:
        store.close()


class _FlakyRepo:
    """首轮磁盘故障、次轮恢复的适配器替身。"""

    def __init__(self, real: MarkdownCaseRepository):
        self.real = real
        self.calls = 0

    async def write_approved_record(self, record):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("磁盘暂时只读")
        return await self.real.write_approved_record(record)


def test_dispatch_failure_is_retryable_with_same_key(tmp_path):
    store = bootstrap_store(tmp_path)
    real = MarkdownCaseRepository(
        tmp_path / "writeback",
        approval_provider=store.get_approval, kill_switch=lambda: False)
    flaky = _FlakyRepo(real)
    try:
        record = _seed_approved_outbox(store)

        first = asyncio.run(dispatch_pending_outbox(_ShimContainer(store, flaky)))
        assert first["status"] == "partial" and first["failed"] == 1
        got = store.get_outbox_record(record.idempotency_key)
        assert got.status is OutboxStatus.FAILED and got.attempts == 1
        assert "磁盘暂时只读" in got.error

        # 恢复后重跑：同幂等键转正，external_ref 落账，error 被清空
        second = asyncio.run(dispatch_pending_outbox(_ShimContainer(store, flaky)))
        assert second["status"] == "succeeded" and second["confirmed"] == 1
        got = store.get_outbox_record(record.idempotency_key)
        assert got.status is OutboxStatus.CONFIRMED and got.attempts == 2
        assert got.error == "" and got.external_ref
    finally:
        store.close()


def test_dispatch_skips_unknown_target_and_missing_adapter(tmp_path):
    store = bootstrap_store(tmp_path)
    try:
        _seed_approved_outbox(store)
        # 另一条 json-tickets 记录：S2.5 无路由，保留 pending
        store.save_approval(Approval(
            approval_id="ap-json", action_level=ActionLevel.R3_APPROVED_EXTERNAL,
            target_system="json-tickets", request_summary="工单",
            decision=ApprovalDecision.APPROVED, reviewer="expert-zhang",
            decided_at=_NOW))
        store.add_outbox(OutboxRecord(
            idempotency_key="ticket:abc", action_level=ActionLevel.R3_APPROVED_EXTERNAL,
            target_system="json-tickets", request_body={"title": "x"},
            approval_id="ap-json"))

        # 未装配 case_repo：两条都 skip，不报错不丢账
        report = asyncio.run(dispatch_pending_outbox(_ShimContainer(store)))
        assert report["skipped_unsupported"] == 2
        assert report["status"] == "succeeded"
        assert len(store.pending_outbox_records()) == 2
    finally:
        store.close()


def test_dispatch_policy_denied_mid_run_does_not_fail_sibling(tmp_path):
    # kill switch 打开时两条记录都 blocked，且报告逐条留痕、整体仍 succeeded
    store = bootstrap_store(tmp_path)
    repo = MarkdownCaseRepository(
        tmp_path / "writeback",
        approval_provider=store.get_approval, kill_switch=lambda: True)
    try:
        _seed_approved_outbox(store, key="case-writeback:cases/x.md#TC-N2#k1")
        _seed_approved_outbox(store, key="case-writeback:cases/x.md#TC-N2#k2")
        report = asyncio.run(dispatch_pending_outbox(_ShimContainer(store, repo)))
        assert report["blocked"] == 2 and report["failed"] == 0
        assert {r["outcome"] for r in report["results"]} == {"blocked"}
        assert len(store.pending_outbox_records()) == 2
    finally:
        store.close()
