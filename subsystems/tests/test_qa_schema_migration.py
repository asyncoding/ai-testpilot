"""schema v4→v5 迁移与 qa_runs CRUD 测试（S1.4，FR-QA-001～004 持久化）。

迁移范式复刻 test_retrieval_store：bootstrap 建出当前版本库 → 手动 DROP 新表/索引
并回拨 meta 到 4 → 重开 bootstrap，断言幂等补建、旧表无损、meta 升 5。
"""

import sqlite3

import pytest

from testpilot_adapters.store import bootstrap_store
from testpilot_core import SCHEMA_VERSION
from testpilot_core.enums import FeedbackKind, FeedbackReason
from testpilot_core.models import Feedback


def _table_names(conn) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    return {r["name"] for r in rows}


def _index_names(conn) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {r["name"] for r in rows}


def test_v4_store_upgrades_to_v5_with_qa_runs(tmp_path):
    store = bootstrap_store(tmp_path)
    # 老库留档：一行反馈 + 表清单
    store.add_feedback(Feedback(
        run_id="legacy-run", target_urn="urn:testpilot:qa-run:legacy-run",
        kind=FeedbackKind.NOT_USEFUL, reason=FeedbackReason.STALE_REFERENCE,
    ))
    tables_before = _table_names(store.conn)
    feedback_before = store.conn.execute("SELECT COUNT(*) c FROM feedback").fetchone()["c"]

    # 模拟 v4 老库：v5 新增对象全部撤掉，meta 回拨。
    # 注意 DROP TABLE 会连带删除 qa_runs 上的两个索引，只需单独 DROP feedback 索引。
    store.conn.execute("DROP TABLE qa_runs")
    store.conn.execute("DROP INDEX idx_feedback_run_id")
    store.conn.execute("UPDATE meta SET value='4' WHERE key='schema_version'")
    store.conn.commit()
    assert "qa_runs" not in _table_names(store.conn)
    store.close()

    # 重开：幂等补建 + 版本升 5
    store2 = bootstrap_store(tmp_path)
    try:
        assert store2.get_meta("schema_version") == str(SCHEMA_VERSION) == "5"
        assert "qa_runs" in _table_names(store2.conn)
        assert {"idx_qa_runs_ts", "idx_qa_runs_status", "idx_feedback_run_id"} <= _index_names(store2.conn)
        # 旧表无损：表清单只增不减，老反馈行还在
        assert tables_before <= _table_names(store2.conn)
        assert store2.conn.execute("SELECT COUNT(*) c FROM feedback").fetchone()["c"] == feedback_before
        # 补建后的表立即可写
        store2.insert_qa_run(record={"run_id": "run-after-upgrade", "question": "迁移后可写？",
                                     "status": "answered"})
        row = store2.get_qa_run("run-after-upgrade")
        assert row is not None and row["status"] == "answered"
    finally:
        store2.close()


def _full_record(**over):
    record = {
        "run_id": "run-1", "ts": "2026-11-05T08:00:00+00:00",
        "project": "demo", "branch": "alarmkit-spike", "acl_scope": "default",
        "question": "kill-backup 现在多少秒？", "intent": "code", "status": "answered",
        "answer_payload": {"markdown": "30s"},
        "citations": [{"urn": "urn:x", "version_state": "consistent"}],
        "evidence": [{"urn": "urn:x", "text": "证据"}],
        "verify_results": [{"urn": "urn:x", "verified": True}],
        "retrieval_event_id": 7, "unknown_reason": "",
        "degraded": ["intent_degraded"], "attempts": 2, "regens": 1,
        "model_version": "recorded-1", "prompt_version": "qa-prompt-1",
        "params_version": "qa-params-1", "data_version": "7936295",
        "tokens_in": 100, "tokens_out": 40, "latency_ms": 320,
        "thread_id": "run-1",
    }
    record.update(over)
    return record


def test_qa_run_roundtrip_parses_json_columns(tmp_path):
    store = bootstrap_store(tmp_path)
    store.insert_qa_run(record=_full_record())
    row = store.get_qa_run("run-1")
    assert row["answer_payload"] == {"markdown": "30s"}  # JSON 列已解析回对象
    assert row["citations"][0]["version_state"] == "consistent"
    assert row["verify_results"][0]["verified"] is True
    assert row["degraded"] == ["intent_degraded"]
    assert row["retrieval_event_id"] == 7 and row["regens"] == 1
    assert store.get_qa_run("missing") is None


def test_list_qa_runs_filters_status_and_orders_desc(tmp_path):
    store = bootstrap_store(tmp_path)
    store.insert_qa_run(record=_full_record(run_id="r1", status="answered"))
    store.insert_qa_run(record=_full_record(run_id="r2", status="unknown",
                                            unknown_reason="insufficient_evidence"))
    store.insert_qa_run(record=_full_record(run_id="r3", status="answered"))
    unknowns = store.list_qa_runs(status="unknown")
    assert [r["run_id"] for r in unknowns] == ["r2"]
    assert [r["run_id"] for r in store.list_qa_runs()] == ["r3", "r2", "r1"]


def test_duplicate_run_id_rejected_not_upserted(tmp_path):
    store = bootstrap_store(tmp_path)
    store.insert_qa_run(record=_full_record(status="answered"))
    # 双写是接线 bug：UNIQUE 约束直接抛，不允许静默覆盖首行结论
    with pytest.raises(sqlite3.IntegrityError):
        store.insert_qa_run(record=_full_record(status="unknown"))
    assert store.get_qa_run("run-1")["status"] == "answered"


def test_feedback_summary_groups_kind_and_reason(tmp_path):
    store = bootstrap_store(tmp_path)
    store.add_feedback(Feedback(
        run_id="r1", target_urn="urn:testpilot:qa-run:r1",
        kind=FeedbackKind.NOT_USEFUL, reason=FeedbackReason.STALE_REFERENCE))
    store.add_feedback(Feedback(
        run_id="r2", target_urn="urn:testpilot:qa-run:r2",
        kind=FeedbackKind.NOT_USEFUL, reason=FeedbackReason.FACTUAL_ERROR))
    store.add_feedback(Feedback(
        run_id="r3", target_urn="urn:testpilot:qa-run:r3",
        kind=FeedbackKind.USEFUL))
    summary = {(s["kind"], s["reason"]): s["n"] for s in store.feedback_summary()}
    assert summary[("not_useful", "stale_reference")] == 1
    assert summary[("not_useful", "factual_error")] == 1
    assert summary[("useful", "")] == 1  # reason 可空，聚合键归一成空串
