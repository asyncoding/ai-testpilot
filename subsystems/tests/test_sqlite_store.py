"""SQLite/FTS5/outbox/审批/事件/评测存储测试（docs/08 S0 数据底座）。"""

from datetime import datetime, timezone

from testpilot_adapters.store import bootstrap_store
from testpilot_core.enums import (
    ActionLevel,
    ApprovalDecision,
    AuthorityLevel,
    OutboxStatus,
    StaleStatus,
)
from testpilot_core.models import (
    Approval,
    EventEnvelope,
    EvalVersion,
    Feedback,
    FeedbackKind,
    KnowledgeChunk,
    OutboxRecord,
    SourceDocument,
)


def _doc(i: int = 1) -> SourceDocument:
    return SourceDocument(
        urn=f"urn:testpilot:fs-docs:document:doc{i}@h{i}",
        source_system="fs-docs",
        uri=f"file:///tmp/doc{i}.md",
        title=f"doc{i}",
        version=f"h{i}",
        content_hash=f"hash{i}",
        authority_level=AuthorityLevel.L2_EXPERIENCE,
    )


def _chunk(doc_urn: str, text: str) -> KnowledgeChunk:
    return KnowledgeChunk(
        urn=f"{doc_urn}#chunk0",
        doc_urn=doc_urn,
        index=0,
        kind="text",
        text=text,
        quality_score=0.8,
        content_hash="c1",
    )


def test_bootstrap_idempotent_and_schema_version(tmp_path):
    store = bootstrap_store(tmp_path)
    store2 = bootstrap_store(tmp_path)  # 二次启动不报错
    try:
        # 版本号以 core 常量为准（S1.1 已升到 2）
        from testpilot_core import SCHEMA_VERSION
        assert store.get_meta("schema_version") == str(SCHEMA_VERSION)
    finally:
        store.close()
        store2.close()


def test_chunk_fts_index_and_search(tmp_path):
    store = bootstrap_store(tmp_path)
    try:
        store.upsert_document(_doc())
        # 含 ASCII token，FTS5 unicode61 可直接命中（中文整句检索在 S1 换分词器）
        store.upsert_chunk(_chunk(_doc().urn, "kill-backup schedulePrimary 30s 兜底机制"))
        hits = store.search_fts("schedulePrimary")
        assert len(hits) == 1 and hits[0]["stale"] == StaleStatus.FRESH
        assert store.search_fts("nonexistent_token") == []
    finally:
        store.close()


def test_event_dedup_and_dead_letter(tmp_path):
    store = bootstrap_store(tmp_path)
    try:
        event = EventEnvelope(
            event_id="e1",
            event_type="CommitPushed",
            source_system="git",
            occurred_at=datetime(2026, 11, 1, tzinfo=timezone.utc),
            project="demo",
            subject_ref="urn:x",
            idempotency_key="e1",
        )
        assert store.record_event_if_new(event) is True
        assert store.record_event_if_new(event) is False  # 重复投递
        store.add_dead_letter("ci", {"bad": True}, "缺字段")
        rows = store.conn.execute("SELECT COUNT(*) AS n FROM dead_letters").fetchone()
        assert rows["n"] == 1
    finally:
        store.close()


def test_approval_outbox_roundtrip(tmp_path):
    store = bootstrap_store(tmp_path)
    try:
        ap = Approval(
            approval_id="ap-1",
            action_level=ActionLevel.R3_APPROVED_EXTERNAL,
            target_system="json-tickets",
            request_summary="create issue",
            decision=ApprovalDecision.APPROVED,
            reviewer="tester",
        )
        store.save_approval(ap)
        loaded = store.get_approval("ap-1")
        assert loaded and loaded.decision == ApprovalDecision.APPROVED

        rec = OutboxRecord(
            idempotency_key="ob-1",
            action_level=ActionLevel.R3_APPROVED_EXTERNAL,
            target_system="json-tickets",
            request_body={"title": "t"},
            approval_id="ap-1",
        )
        store.add_outbox(rec)
        store.add_outbox(rec)  # 幂等键不重复落库
        pending = store.list_pending_outbox()
        assert len(pending) == 1
        store.mark_outbox("ob-1", OutboxStatus.CONFIRMED, external_ref="urn:x:y")
        assert store.list_pending_outbox() == []
    finally:
        store.close()


def test_feedback_and_eval_run(tmp_path):
    store = bootstrap_store(tmp_path)
    try:
        store.add_feedback(
            Feedback(run_id="r1", target_urn="urn:a", kind=FeedbackKind.NOT_USEFUL, model_version="recorded")
        )
        from testpilot_core.models import EvalRun

        store.add_eval_run(
            EvalRun(
                eval_run_id="eval-1",
                version=EvalVersion(
                    data_version="d1", model_version="m1", prompt_version="p1", params_version="x1"
                ),
                metrics={"citation_accuracy": 1.0},
            )
        )
        assert store.conn.execute("SELECT COUNT(*) AS n FROM feedback").fetchone()["n"] == 1
        row = store.conn.execute("SELECT metrics FROM eval_runs WHERE eval_run_id='eval-1'").fetchone()
        assert "citation_accuracy" in row["metrics"]
    finally:
        store.close()
