"""S3.1 schema v15 执行账本/配额账本存储测试（FR-EX-002，tmp 库 hermetic）。

覆盖：v15 新表与老库 ALTER 迁移、run 账本登记/幂等兜底/状态回写双击守卫/
过滤盘点/raw_json 还原、预算账本记账/当日计数/request_id 幂等。
"""

from __future__ import annotations

import sqlite3

import pytest

from testpilot_adapters.store import SqliteQueue, bootstrap_store
from testpilot_core import SCHEMA_VERSION
from testpilot_core.enums import RunStatus
from testpilot_core.execution import BudgetEntry
from testpilot_core.models import (
    CaseResult,
    CaseSelector,
    EnvironmentInfo,
    TestExecutionRequest,
    TestRun,
)
from testpilot_core.enums import CaseStatus


def _table_names(conn) -> set[str]:
    return {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}


def _column_names(conn, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _request(**over) -> TestExecutionRequest:
    kwargs = dict(
        request_id="req-1", idempotency_key="idem-1", project="demo",
        selector=CaseSelector(test_ids=["T/t1"]), commit_sha="deadbeef",
        daily_budget_key="demo:2026-11-10",
    )
    kwargs.update(over)
    return TestExecutionRequest(**kwargs)


def test_v15_fresh_store_has_execution_tables(tmp_path):
    store = bootstrap_store(tmp_path)
    try:
        assert store.get_meta("schema_version") == str(SCHEMA_VERSION) == "15"
        assert {"test_runs", "rerun_budget"} <= _table_names(store.conn)
        assert {"lease_expires_at", "cancel_requested"} <= _column_names(
            store.conn, "jobs")
    finally:
        store.close()


def test_v14_legacy_store_upgrades_jobs_columns_and_data(tmp_path):
    store = bootstrap_store(tmp_path)
    queue = SqliteQueue(store.conn)
    import asyncio

    job_id = asyncio.run(queue.enqueue("test_execution", {"k": 1}, "idem-legacy"))
    # 模拟 v14 老库：jobs 重建为无租约两列的旧形状，版本回拨
    store.conn.execute(
        "CREATE TABLE jobs_old AS SELECT id, job_type, payload_json, idempotency_key,"
        " status, attempts, error, created_at, updated_at FROM jobs"
    )
    store.conn.execute("DROP TABLE jobs")
    store.conn.execute("ALTER TABLE jobs_old RENAME TO jobs")
    store.conn.execute(
        "CREATE INDEX idx_jobs_status_type ON jobs(status, job_type)")
    store.conn.execute("UPDATE meta SET value='14' WHERE key='schema_version'")
    store.conn.commit()
    store.close()

    upgraded = bootstrap_store(tmp_path)
    try:
        assert upgraded.get_meta("schema_version") == "15"
        assert {"lease_expires_at", "cancel_requested"} <= _column_names(
            upgraded.conn, "jobs")
        # 老任务数据无损且仍可被认领
        row = upgraded.conn.execute(
            "SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        assert row["status"] == "queued" and row["cancel_requested"] == 0
    finally:
        upgraded.close()


def test_run_ledger_lifecycle_and_idempotency_backstop(tmp_path):
    store = bootstrap_store(tmp_path)
    try:
        store.insert_test_run_ledger(
            request=_request(), run_id="run-abc123", status=RunStatus.QUEUED.value)
        row = store.get_test_run_row("run-abc123")
        assert row["status"] == "queued" and row["raw_json"] == ""
        assert store.row_to_test_run(row) is None  # 未收尾：只有投影没有完整 TestRun
        assert store.get_test_run_by_idempotency("idem-1")["run_id"] == "run-abc123"

        # UNIQUE(idempotency_key) 是兜底防线：预检失效时 IntegrityError 抛穿
        with pytest.raises(sqlite3.IntegrityError):
            store.insert_test_run_ledger(
                request=_request(), run_id="run-other", status="queued")

        finished = TestRun(
            run_id="run-abc123", idempotency_key="idem-1", project="demo",
            commit_sha="deadbeef", status=RunStatus.PASSED,
            environment=EnvironmentInfo(provider="junit-local", env_id="jdk21"),
            case_results=[CaseResult(
                external_case_id="T/t1", status=CaseStatus.PASSED)],
        )
        store.update_test_run(
            "run-abc123", status=RunStatus.PASSED.value,
            raw_json=finished.model_dump_json(),
            started_at="2026-11-10T09:00:00+00:00",
            finished_at="2026-11-10T09:00:05+00:00")
        row = store.get_test_run_row("run-abc123")
        restored = store.row_to_test_run(row)
        assert restored.status == RunStatus.PASSED
        assert restored.case_results[0].external_case_id == "T/t1"

        with pytest.raises(ValueError):
            store.update_test_run("run-missing", status="failed")
    finally:
        store.close()


def test_run_ledger_list_filters(tmp_path):
    store = bootstrap_store(tmp_path)
    try:
        store.insert_test_run_ledger(
            request=_request(request_id="r1", idempotency_key="k1"),
            run_id="run-1", status="queued")
        store.insert_test_run_ledger(
            request=_request(request_id="r2", idempotency_key="k2", project="other"),
            run_id="run-2", status="failed")
        assert {r["run_id"] for r in store.list_test_runs()} == {"run-1", "run-2"}
        assert {r["run_id"] for r in store.list_test_runs(status="failed")} == {"run-2"}
        assert {r["run_id"] for r in store.list_test_runs(project="other")} == {"run-2"}
    finally:
        store.close()


def test_budget_ledger_count_and_request_idempotency(tmp_path):
    store = bootstrap_store(tmp_path)
    try:
        key, date = "demo:2026-11-10", "2026-11-10"
        assert store.count_budget_today(key, date) == 0
        store.insert_budget_entry(
            BudgetEntry(budget_key=key, date=date, request_id="req-1", run_id="run-1"))
        store.insert_budget_entry(
            BudgetEntry(budget_key=key, date=date, request_id="req-2", run_id="run-2"))
        assert store.count_budget_today(key, date) == 2
        entries = store.list_budget_entries(key, date)
        assert [e.request_id for e in entries] == ["req-1", "req-2"]
        # 跨日计数隔离
        assert store.count_budget_today(key, "2026-11-11") == 0
        with pytest.raises(sqlite3.IntegrityError):
            store.insert_budget_entry(
                BudgetEntry(budget_key=key, date=date,
                            request_id="req-1", run_id="run-dup"))
    finally:
        store.close()
