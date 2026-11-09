"""schema 迁移与 qa_runs CRUD 测试（S1.4 起，FR-QA-001～004 持久化）。

三条迁移路径都覆盖：
- v4→v7 全量重建：DROP 新表/索引并回拨 meta → 重开 bootstrap，断言幂等补建、
  旧表无损、meta 升到当前版本（复刻 test_retrieval_store 范式）；
- v5→v6 单列 ALTER：qa_runs 已存在时 CREATE IF NOT EXISTS 不会加列，靠
  _COLUMN_MIGRATIONS 补 citation_errors，老行吃 DEFAULT '[]' 不丢数据；
- v6→v7 单列 ALTER：同理补 intent_source，老行吃 DEFAULT ''（legacy 不造数）。
"""

import sqlite3

import pytest

from testpilot_adapters.store import bootstrap_store
from testpilot_core import SCHEMA_VERSION
from testpilot_core.enums import FeedbackKind, FeedbackReason
from testpilot_core.models import Feedback


def _column_names(conn, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


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


def test_v4_store_upgrades_to_current_with_qa_runs(tmp_path):
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

    # 重开：幂等补建 + 版本升到当前版本，新表直接带 v6 列
    store2 = bootstrap_store(tmp_path)
    try:
        assert store2.get_meta("schema_version") == str(SCHEMA_VERSION) == "8"
        assert "qa_runs" in _table_names(store2.conn)
        assert {"idx_qa_runs_ts", "idx_qa_runs_status", "idx_feedback_run_id"} <= _index_names(store2.conn)
        assert "citation_errors" in _column_names(store2.conn, "qa_runs")
        assert "intent_source" in _column_names(store2.conn, "qa_runs")
        # 旧表无损：表清单只增不减，老反馈行还在
        assert tables_before <= _table_names(store2.conn)
        assert store2.conn.execute("SELECT COUNT(*) c FROM feedback").fetchone()["c"] == feedback_before
        # 补建后的表立即可写
        store2.insert_qa_run(record={"run_id": "run-after-upgrade", "question": "迁移后可写？",
                                     "status": "answered"})
        row = store2.get_qa_run("run-after-upgrade")
        assert row is not None and row["status"] == "answered"
        assert row["citation_errors"] == []  # 缺省值经 JSON 解析回空列表
    finally:
        store2.close()


# v5 形态的 qa_runs（schema.sql 当前定义去掉 v6 的 citation_errors 列）
_V5_QA_RUNS_DDL = """
CREATE TABLE qa_runs_v5 (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id             TEXT NOT NULL UNIQUE,
    ts                 TEXT NOT NULL,
    project            TEXT NOT NULL DEFAULT '',
    branch             TEXT NOT NULL DEFAULT 'main',
    acl_scope          TEXT NOT NULL DEFAULT 'default',
    question           TEXT NOT NULL,
    intent             TEXT NOT NULL DEFAULT '',
    status             TEXT NOT NULL,
    answer_payload     TEXT NOT NULL DEFAULT '{}',
    citations          TEXT NOT NULL DEFAULT '[]',
    evidence           TEXT NOT NULL DEFAULT '[]',
    verify_results     TEXT NOT NULL DEFAULT '[]',
    retrieval_event_id INTEGER,
    unknown_reason     TEXT NOT NULL DEFAULT '',
    degraded           TEXT NOT NULL DEFAULT '[]',
    attempts           INTEGER NOT NULL DEFAULT 0,
    regens             INTEGER NOT NULL DEFAULT 0,
    model_version      TEXT NOT NULL DEFAULT '',
    prompt_version     TEXT NOT NULL DEFAULT '',
    params_version     TEXT NOT NULL DEFAULT '',
    data_version       TEXT NOT NULL DEFAULT '',
    tokens_in          INTEGER NOT NULL DEFAULT 0,
    tokens_out         INTEGER NOT NULL DEFAULT 0,
    latency_ms         INTEGER NOT NULL DEFAULT 0,
    thread_id          TEXT NOT NULL DEFAULT '',
    created_at         TEXT NOT NULL
)
"""
_V5_COLUMNS = [
    "run_id", "ts", "project", "branch", "acl_scope", "question", "intent", "status",
    "answer_payload", "citations", "evidence", "verify_results", "retrieval_event_id",
    "unknown_reason", "degraded", "attempts", "regens", "model_version", "prompt_version",
    "params_version", "data_version", "tokens_in", "tokens_out", "latency_ms",
    "thread_id", "created_at",
]


def test_v5_qa_runs_alter_adds_citation_errors_with_default(tmp_path):
    """真实老库场景：qa_runs 表已存在且有数据，bootstrap 只能走 ALTER 补列。"""
    store = bootstrap_store(tmp_path)
    store.insert_qa_run(record=_full_record())
    store.close()

    db_path = tmp_path / "store" / "testpilot.sqlite"
    legacy = sqlite3.connect(db_path)
    legacy.row_factory = sqlite3.Row
    try:
        # 回退成 v5 表形状（整表重建是 SQLite 去列的通用做法，不依赖 3.35 DROP COLUMN）
        legacy.execute(_V5_QA_RUNS_DDL)
        cols = ", ".join(_V5_COLUMNS)
        legacy.execute(f"INSERT INTO qa_runs_v5 ({cols}) SELECT {cols} FROM qa_runs")
        legacy.execute("DROP TABLE qa_runs")
        legacy.execute("ALTER TABLE qa_runs_v5 RENAME TO qa_runs")
        legacy.execute("CREATE INDEX idx_qa_runs_ts ON qa_runs(ts)")
        legacy.execute("CREATE INDEX idx_qa_runs_status ON qa_runs(status)")
        legacy.execute("UPDATE meta SET value='5' WHERE key='schema_version'")
        legacy.commit()
        assert "citation_errors" not in _column_names(legacy, "qa_runs")
    finally:
        legacy.close()

    store2 = bootstrap_store(tmp_path)
    try:
        assert store2.get_meta("schema_version") == str(SCHEMA_VERSION) == "8"
        assert "citation_errors" in _column_names(store2.conn, "qa_runs")
        # v7 的 intent_source 随同一轮迁移链补上，老行吃 DEFAULT ''（legacy 不造数）
        assert "intent_source" in _column_names(store2.conn, "qa_runs")
        # 老行无损且新列吃 DEFAULT，经 _qa_row_to_dict 解析为 []
        old = store2.get_qa_run("run-1")
        assert old is not None
        assert old["question"] == "kill-backup 现在多少秒？"
        assert old["citation_errors"] == []
        assert old["intent_source"] == ""
        # 补列后新写入的具体错误原样 JSON 往返
        store2.insert_qa_run(record=_full_record(
            run_id="run-v6",
            citation_errors=["quote_not_verbatim: urn:testpilot:chunk:x",
                             "urn_not_in_evidence: urn:testpilot:chunk:y"],
        ))
        new = store2.get_qa_run("run-v6")
        assert new["citation_errors"] == [
            "quote_not_verbatim: urn:testpilot:chunk:x",
            "urn_not_in_evidence: urn:testpilot:chunk:y",
        ]
    finally:
        store2.close()


# v6 形态的 qa_runs（v5 DDL + citation_errors 列，缺 v7 的 intent_source）
_V6_QA_RUNS_DDL = """
CREATE TABLE qa_runs_v6 (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id             TEXT NOT NULL UNIQUE,
    ts                 TEXT NOT NULL,
    project            TEXT NOT NULL DEFAULT '',
    branch             TEXT NOT NULL DEFAULT 'main',
    acl_scope          TEXT NOT NULL DEFAULT 'default',
    question           TEXT NOT NULL,
    intent             TEXT NOT NULL DEFAULT '',
    status             TEXT NOT NULL,
    answer_payload     TEXT NOT NULL DEFAULT '{}',
    citations          TEXT NOT NULL DEFAULT '[]',
    evidence           TEXT NOT NULL DEFAULT '[]',
    verify_results     TEXT NOT NULL DEFAULT '[]',
    retrieval_event_id INTEGER,
    unknown_reason     TEXT NOT NULL DEFAULT '',
    citation_errors    TEXT NOT NULL DEFAULT '[]',
    degraded           TEXT NOT NULL DEFAULT '[]',
    attempts           INTEGER NOT NULL DEFAULT 0,
    regens             INTEGER NOT NULL DEFAULT 0,
    model_version      TEXT NOT NULL DEFAULT '',
    prompt_version     TEXT NOT NULL DEFAULT '',
    params_version     TEXT NOT NULL DEFAULT '',
    data_version       TEXT NOT NULL DEFAULT '',
    tokens_in          INTEGER NOT NULL DEFAULT 0,
    tokens_out         INTEGER NOT NULL DEFAULT 0,
    latency_ms         INTEGER NOT NULL DEFAULT 0,
    thread_id          TEXT NOT NULL DEFAULT '',
    created_at         TEXT NOT NULL
)
"""
_V6_COLUMNS = [
    "run_id", "ts", "project", "branch", "acl_scope", "question", "intent", "status",
    "answer_payload", "citations", "evidence", "verify_results", "retrieval_event_id",
    "unknown_reason", "citation_errors", "degraded", "attempts", "regens",
    "model_version", "prompt_version", "params_version", "data_version", "tokens_in",
    "tokens_out", "latency_ms", "thread_id", "created_at",
]


def test_v6_qa_runs_alter_adds_intent_source_with_default(tmp_path):
    """v7 真实老库场景：v6 表已存在且有数据，bootstrap 只走 ALTER 补 intent_source。"""
    store = bootstrap_store(tmp_path)
    store.insert_qa_run(record=_full_record())
    store.close()

    db_path = tmp_path / "store" / "testpilot.sqlite"
    legacy = sqlite3.connect(db_path)
    legacy.row_factory = sqlite3.Row
    try:
        legacy.execute(_V6_QA_RUNS_DDL)
        cols = ", ".join(_V6_COLUMNS)
        legacy.execute(f"INSERT INTO qa_runs_v6 ({cols}) SELECT {cols} FROM qa_runs")
        legacy.execute("DROP TABLE qa_runs")
        legacy.execute("ALTER TABLE qa_runs_v6 RENAME TO qa_runs")
        legacy.execute("CREATE INDEX idx_qa_runs_ts ON qa_runs(ts)")
        legacy.execute("CREATE INDEX idx_qa_runs_status ON qa_runs(status)")
        legacy.execute("UPDATE meta SET value='6' WHERE key='schema_version'")
        legacy.commit()
        assert "intent_source" not in _column_names(legacy, "qa_runs")
        assert "citation_errors" in _column_names(legacy, "qa_runs")
    finally:
        legacy.close()

    store2 = bootstrap_store(tmp_path)
    try:
        assert store2.get_meta("schema_version") == str(SCHEMA_VERSION) == "8"
        assert "intent_source" in _column_names(store2.conn, "qa_runs")
        # 老行无损：intent_source 吃 DEFAULT ''，既有 v6 列原样可读
        old = store2.get_qa_run("run-1")
        assert old is not None
        assert old["intent_source"] == ""
        assert old["citation_errors"] == ["quote_not_verbatim: urn:x"]
        # 新写入的三值来源原样往返
        store2.insert_qa_run(record=_full_record(
            run_id="run-v7", intent_source="regex_bypass"))
        new = store2.get_qa_run("run-v7")
        assert new["intent_source"] == "regex_bypass"
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
        "citation_errors": ["quote_not_verbatim: urn:x"],
        "intent_source": "model",
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
    assert row["citation_errors"] == ["quote_not_verbatim: urn:x"]
    assert row["intent_source"] == "model"
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
