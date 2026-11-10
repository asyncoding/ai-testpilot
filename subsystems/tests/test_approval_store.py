"""S2.5 schema v12 存储测试（FR-TD-004 / FR-WR-001 持久化口径）。

覆盖两条迁移/留痕路径：
- v11→v12 老库升级：oracle_suggestions 经 _COLUMN_MIGRATIONS ALTER 补四人审列，
  preference_samples 经 CREATE TABLE IF NOT EXISTS 补建；老 pending 行无损且
  补列吃默认值（NULL/空串），升级后立即可写；
- 人审落账/偏好样本/outbox 审计读口的口径与幂等守卫。
"""

from datetime import datetime, timezone

import pytest

from testpilot_adapters.store import bootstrap_store
from testpilot_core import SCHEMA_VERSION
from testpilot_core.enums import (
    ActionLevel,
    ApprovalDecision,
    OracleType,
    OutboxStatus,
)
from testpilot_core.models import Approval, OutboxRecord, PreferenceSample
from testpilot_core.oracle_mining import MinedOracle

_DOC_URN = "urn:testpilot:fs-cases:document:cases/synthetic-cases.md"
_CASE_URN_A = "urn:testpilot:fs-cases:test_case:cases/synthetic-cases.md#TC-A1"
_CASE_URN_B = "urn:testpilot:fs-cases:test_case:cases/synthetic-cases.md#TC-B2"
_NOW = datetime(2026, 11, 9, 8, 30, tzinfo=timezone.utc)


def _oracle(expectation: str, *, otype=OracleType.STATE_ASSERTION) -> MinedOracle:
    return MinedOracle(
        type=otype, expectation=expectation,
        source_kind="requirement_ac", provenance="deterministic",
        source_refs=["urn:testpilot:fs-reqs:requirement:req.md#AC-1"],
        confidence=0.7,
    )


def _column_names(conn, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _table_names(conn) -> set[str]:
    return {
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }


# ── v11→v12 老库迁移 ────────────────────────────────────────────────────────

def test_v11_store_upgrades_with_review_columns_and_preference_table(tmp_path):
    store = bootstrap_store(tmp_path)
    closed = False
    try:
        oracle = _oracle("落库一条 stopped 记录")
        store.upsert_oracle_suggestion(
            _DOC_URN, _CASE_URN_A, "TC-A1", oracle, content_hash="h1")
        assert store.count_oracle_suggestions() == 1

        # 回滚成 v11 老库形态：拆掉 preference_samples，并把 oracle_suggestions
        # 退回无四人审列的 16 列结构（CTAS 只保留列名与数据，迁移路径只依赖这两者）
        store.conn.execute("DROP TABLE preference_samples")
        store.conn.execute(
            "CREATE TABLE oracle_suggestions_v11 AS "
            "SELECT urn, doc_urn, case_urn, external_id, oracle_type, expectation, "
            "source_kind, provenance, source_refs, confidence, status, exploratory, "
            "content_hash, branch, raw_json, indexed_at FROM oracle_suggestions"
        )
        store.conn.execute("DROP TABLE oracle_suggestions")
        store.conn.execute(
            "ALTER TABLE oracle_suggestions_v11 RENAME TO oracle_suggestions")
        store.conn.execute(
            "UPDATE meta SET value='11' WHERE key='schema_version'")
        store.conn.commit()
        assert "preference_samples" not in _table_names(store.conn)
        assert "reviewed_by" not in _column_names(store.conn, "oracle_suggestions")
        store.close()
        closed = True

        # 重开：schema.sql 补建新表/索引，_COLUMN_MIGRATIONS 补四列，版本升 v12
        store2 = bootstrap_store(tmp_path)
        try:
            assert store2.get_meta("schema_version") == str(SCHEMA_VERSION) == "14"
            assert "preference_samples" in _table_names(store2.conn)
            cols = _column_names(store2.conn, "oracle_suggestions")
            assert {"approval_id", "reviewed_by", "reviewed_at",
                    "decision_reason"} <= cols

            # 老 pending 行无损：仍在待审队列，人审四列吃默认值
            queue = store2.list_review_queue_rows()
            assert len(queue) == 1
            row = queue[0]
            assert row["status"] == "pending"
            assert row["oracle"] == oracle
            assert row["approval_id"] is None
            assert row["reviewed_by"] is None
            assert row["reviewed_at"] is None
            assert row["decision_reason"] == ""

            # ALTER 补出来的列立即可写（迁移不是只过 PRAGMA）
            store2.mark_suggestion_reviewed(
                row["urn"], status="approved", approval_id="ap-mig",
                reviewer="expert-zhang", reason="迁移后评审",
                reviewed_at=_NOW.isoformat())
            again = store2.conn.execute(
                "SELECT status, reviewed_by, decision_reason "
                "FROM oracle_suggestions WHERE urn=?", (row["urn"],),
            ).fetchone()
            assert again["status"] == "approved"
            assert again["reviewed_by"] == "expert-zhang"
            assert again["decision_reason"] == "迁移后评审"
        finally:
            store2.close()
    finally:
        if not closed:  # 迁移准备阶段断言失败时保证连接释放
            store.close()


# ── 待审队列读口 ────────────────────────────────────────────────────────────

def test_review_queue_filters_pending_and_orders(tmp_path):
    store = bootstrap_store(tmp_path)
    try:
        o1 = _oracle("第一条预期")
        o2 = _oracle("第二条预期", otype=OracleType.LOG_ASSERTION)
        o3 = _oracle("另案预期")
        store.upsert_oracle_suggestion(
            _DOC_URN, _CASE_URN_A, "TC-A1", o1, content_hash="h1")
        store.upsert_oracle_suggestion(
            _DOC_URN, _CASE_URN_A, "TC-A1", o2, content_hash="h2")
        store.upsert_oracle_suggestion(
            _DOC_URN, _CASE_URN_B, "TC-B2", o3, content_hash="h3")

        queue = store.list_review_queue_rows()
        assert len(queue) == 3
        # 排序契约：case_urn 优先、urn 次序；且只含 pending
        ordered = sorted(
            ((r["case_urn"], r["urn"]) for r in queue))
        assert [(r["case_urn"], r["urn"]) for r in queue] == ordered
        assert all(r["status"] == "pending" for r in queue)

        # 单用例读口只回该案
        case_a = store.list_pending_suggestions_for_case(_CASE_URN_A)
        assert len(case_a) == 2
        assert all(r["case_urn"] == _CASE_URN_A for r in case_a)
        assert store.list_pending_suggestions_for_case("urn:ghost") == []

        # 评审一条后：队列读口与单用例读口都剔除它，已评审行不回潮
        reviewed_urn = case_a[0]["urn"]
        store.mark_suggestion_reviewed(
            reviewed_urn, status="approved", approval_id="ap-1",
            reviewer="expert-zhang", reason="采纳",
            reviewed_at=_NOW.isoformat())
        assert len(store.list_review_queue_rows()) == 2
        remaining = store.list_pending_suggestions_for_case(_CASE_URN_A)
        assert len(remaining) == 1
        assert remaining[0]["urn"] != reviewed_urn
    finally:
        store.close()


# ── 人审落账守卫 ────────────────────────────────────────────────────────────

def test_mark_suggestion_reviewed_double_review_raises(tmp_path):
    store = bootstrap_store(tmp_path)
    try:
        store.upsert_oracle_suggestion(
            _DOC_URN, _CASE_URN_A, "TC-A1", _oracle("待审"), content_hash="h")
        urn = store.list_review_queue_rows()[0]["urn"]

        store.mark_suggestion_reviewed(
            urn, status="approved", approval_id="ap-1",
            reviewer="expert-zhang", reason="第一次评审",
            reviewed_at=_NOW.isoformat())

        # 并发双击/重复投递：WHERE status='pending' 挡下，rowcount=0 响亮失败
        with pytest.raises(ValueError, match="草稿不存在或已评审"):
            store.mark_suggestion_reviewed(
                urn, status="rejected", approval_id="ap-2",
                reviewer="expert-li", reason="第二次抢评",
                reviewed_at=_NOW.isoformat())

        # 首次落账不被覆盖
        row = store.conn.execute(
            "SELECT status, approval_id, reviewed_by, decision_reason "
            "FROM oracle_suggestions WHERE urn=?", (urn,),
        ).fetchone()
        assert row["status"] == "approved"
        assert row["approval_id"] == "ap-1"
        assert row["reviewed_by"] == "expert-zhang"
        assert row["decision_reason"] == "第一次评审"
    finally:
        store.close()


# ── 偏好样本 ────────────────────────────────────────────────────────────────

def test_preference_sample_idempotent_and_lists(tmp_path):
    store = bootstrap_store(tmp_path)
    try:
        sample = PreferenceSample(
            sample_id="urn:testpilot:hitl:preference_sample:TC-A1#abc123abcd",
            case_urn=_CASE_URN_A,
            suggestion_urn="urn:testpilot:fs-cases:oracle_suggestion:TC-A1#s1",
            reviewer="expert-zhang",
            reason="期望描述需含账本字段",
            original={"type": "log_assertion", "expectation": "日志出现一行"},
            revised={"type": "state_assertion", "expectation": "落库 stopped 账本"},
            diff={
                "type": {"from": "log_assertion", "to": "state_assertion"},
                "expectation": {"from": "日志出现一行", "to": "落库 stopped 账本"},
            },
            created_at=_NOW,
        )
        store.save_preference_sample(sample)
        store.save_preference_sample(sample)  # sample_id 冲突 DO NOTHING，不翻倍

        listed = store.list_preference_samples()
        assert len(listed) == 1
        got = listed[0]
        assert got.sample_id == sample.sample_id
        assert got.case_urn == _CASE_URN_A
        assert got.original == sample.original
        assert got.revised == sample.revised
        assert got.diff["type"]["to"] == "state_assertion"
        assert got.created_at == _NOW  # datetime 无损 round-trip
    finally:
        store.close()


# ── outbox 审计读口（FR-WR-001 AC1 面板数据源）──────────────────────────────

def test_outbox_records_list_and_get(tmp_path):
    store = bootstrap_store(tmp_path)
    try:
        approval = Approval(
            approval_id="ap-1",
            action_level=ActionLevel.R2_REVIEWED_WRITE,
            target_system="markdown-case-repo",
            request_summary="用例 TC-A1 人审approved",
            decision=ApprovalDecision.APPROVED,
            reviewer="expert-zhang",
        )
        store.save_approval(approval)
        first = OutboxRecord(
            idempotency_key="case-writeback:cases/a.md#TC-A1#aaa111",
            action_level=ActionLevel.R2_REVIEWED_WRITE,
            target_system="markdown-case-repo",
            request_body={"case_external_id": "TC-A1", "markdown": "# m1"},
            approval_id="ap-1",
            rollback_note="删除写根目录文件并重置草稿",
        )
        second = OutboxRecord(
            idempotency_key="case-writeback:cases/b.md#TC-B2#bbb222",
            action_level=ActionLevel.R2_REVIEWED_WRITE,
            target_system="markdown-case-repo",
            request_body={"case_external_id": "TC-B2", "markdown": "# m2"},
            approval_id="ap-1",
        )
        store.add_outbox(first)
        store.add_outbox(second)
        store.mark_outbox(
            first.idempotency_key, OutboxStatus.CONFIRMED,
            external_ref="writeback/cases/a.md#TC-A1")

        records = store.list_outbox_records()
        assert [r.idempotency_key for r in records] == [
            first.idempotency_key, second.idempotency_key]  # ORDER BY id
        got_first = records[0]
        assert got_first.status is OutboxStatus.CONFIRMED
        assert got_first.external_ref == "writeback/cases/a.md#TC-A1"
        assert got_first.request_body["markdown"] == "# m1"
        assert got_first.rollback_note == "删除写根目录文件并重置草稿"
        assert got_first.attempts == 1

        fetched = store.get_outbox_record(second.idempotency_key)
        assert fetched is not None and fetched.status is OutboxStatus.PENDING
        assert store.get_outbox_record("urn:missing") is None
    finally:
        store.close()
