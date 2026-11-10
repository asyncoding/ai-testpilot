"""S2.6c schema v14 bad_cases 存储测试（FR-EV-005 闭环台账持久化口径）。

覆盖：
- v13→v14 老库升级：bad_cases 经 CREATE TABLE IF NOT EXISTS 补建，
  既有表（feedback 等）数据无损，升级后立即可写；
- round-trip（四元组快照/审计历史/时间戳）、反馈幂等查询、过滤盘点、
  UNIQUE 双保险与 update 双击守卫。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from testpilot_adapters.store import bootstrap_store
from testpilot_core import SCHEMA_VERSION
from testpilot_core.badcase import (
    BadCaseSnapshot,
    new_bad_case,
    triage,
)
from testpilot_core.enums import FeedbackKind
from testpilot_core.models import Feedback

_T0 = datetime(2026, 11, 10, 9, 0, tzinfo=timezone.utc)
_T1 = datetime(2026, 11, 10, 11, 0, tzinfo=timezone.utc)


def _table_names(conn) -> set[str]:
    return {
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }


def _case(store, *, source_ref: str = "INC-1#c1", source: str = "manual_review",
          feedback_id: str = "") -> BadCase:
    case = new_bad_case(
        source=source, source_ref=source_ref, title="响铃场景缺失",
        question="如何停闹钟？", detail="复现步骤略", reason="factual_error",
        feedback_id=feedback_id,
        snapshot=BadCaseSnapshot(data_version="d1", model_version="m1",
                                 prompt_version="p1", params_version="x1"),
        at=_T0)
    store.insert_bad_case(case)
    return case


# ── v13→v14 老库迁移 ───────────────────────────────────────────────────────

def test_v13_store_upgrades_with_bad_cases_table(tmp_path):
    store = bootstrap_store(tmp_path)
    try:
        assert "bad_cases" in _table_names(store.conn)
        # 老库既有的反馈数据必须无损
        store.add_feedback(Feedback(
            run_id="run-old", target_urn="urn:testpilot:qa:run:run-old",
            kind=FeedbackKind.NOT_USEFUL, reason="off_topic"))
        # 模拟 v13 老库：拆表并把版本号打回 13
        store.conn.execute("DROP TABLE bad_cases")
        store.conn.execute(
            "UPDATE meta SET value='13' WHERE key='schema_version'")
        store.conn.commit()
        assert "bad_cases" not in _table_names(store.conn)
    finally:
        store.close()

    store2 = bootstrap_store(tmp_path)
    try:
        assert store2.get_meta("schema_version") == str(SCHEMA_VERSION) == "15"
        assert "bad_cases" in _table_names(store2.conn)
        # 升级后立即可写，部分唯一索引就位
        case = _case(store2, source_ref="INC-9#c1")
        assert store2.get_bad_case(case.bad_case_id).status == "open"
        # 老数据无损
        summary = {r["kind"]: r["n"] for r in store2.feedback_summary()}
        assert summary["not_useful"] == 1
    finally:
        store2.close()


# ── round-trip 与查询 ──────────────────────────────────────────────────────

def test_insert_get_round_trip_preserves_snapshot_history_and_timestamps(tmp_path):
    store = bootstrap_store(tmp_path)
    try:
        case = _case(store)
        loaded = store.get_bad_case(case.bad_case_id)
        assert loaded == case
        assert loaded.snapshot.model_dump() == {
            "data_version": "d1", "model_version": "m1",
            "prompt_version": "p1", "params_version": "x1"}
        assert loaded.history[0].to_status == "open"
        assert loaded.closed_at is None and loaded.created_at == _T0
        assert store.get_bad_case("bc-nonexistent") is None
    finally:
        store.close()


def test_get_by_feedback_id_and_list_filters(tmp_path):
    store = bootstrap_store(tmp_path)
    try:
        fb_case = _case(store, source="qa_feedback",
                        source_ref="run-7#feedback-7", feedback_id="7")
        manual = _case(store, source_ref="INC-2#c1")
        assert store.get_bad_case_by_feedback("7").bad_case_id == fb_case.bad_case_id
        assert store.get_bad_case_by_feedback("") is None
        assert store.get_bad_case_by_feedback("999") is None

        all_cases = store.list_bad_cases()
        assert {c.bad_case_id for c in all_cases} == {
            fb_case.bad_case_id, manual.bad_case_id}
        assert [c.bad_case_id for c in all_cases] == sorted(
            c.bad_case_id for c in all_cases)  # created_at 同刻按 ID 排序
        only_fb = store.list_bad_cases(source="qa_feedback")
        assert [c.bad_case_id for c in only_fb] == [fb_case.bad_case_id]

        rejected = triage(fb_case, decision="reject", actor="li", at=_T1)
        store.update_bad_case(rejected)
        rejected = store.list_bad_cases(status="rejected")
        assert [c.bad_case_id for c in rejected] == [fb_case.bad_case_id]
        assert store.get_bad_case(fb_case.bad_case_id).is_closed
    finally:
        store.close()


def test_unique_constraints_and_empty_feedback_coexistence(tmp_path):
    store = bootstrap_store(tmp_path)
    try:
        case = _case(store, source="qa_feedback",
                     source_ref="run-1#feedback-1", feedback_id="1")
        # 同 (source, source_ref) 重复插入：IntegrityError 抛穿（预检失效的接线 bug）
        with pytest.raises(sqlite3.IntegrityError):
            store.insert_bad_case(case)
        # 同 feedback_id 也撞部分唯一索引
        dup_fb = new_bad_case(
            source="qa_feedback", source_ref="run-2#feedback-2",
            title="另一条", feedback_id="1", at=_T0)
        with pytest.raises(sqlite3.IntegrityError):
            store.insert_bad_case(dup_fb)
        # 非反馈源多行空 feedback_id 共存（部分索引不含空串）
        store.insert_bad_case(new_bad_case(
            source="manual_review", source_ref="INC-3#c1", title="t1", at=_T0))
        store.insert_bad_case(new_bad_case(
            source="eval_failure", source_ref="eval-8#case-2", title="t2", at=_T0))
        assert len(store.list_bad_cases()) == 3
    finally:
        store.close()


def test_update_unknown_id_raises_and_transition_persists(tmp_path):
    store = bootstrap_store(tmp_path)
    try:
        case = _case(store)
        ghost = case.model_copy(update={"bad_case_id": "bc-ghost000000"})
        with pytest.raises(ValueError, match="不存在"):
            store.update_bad_case(ghost)

        accepted = triage(case, decision="accept", actor="li",
                          reason="stale_reference", note="复现", at=_T1)
        store.update_bad_case(accepted)
        loaded = store.get_bad_case(case.bad_case_id)
        assert loaded.status == "accepted" and loaded.triaged_by == "li"
        assert loaded.triaged_at == _T1 and loaded.updated_at == _T1
        assert [e.to_status for e in loaded.history][-2:] == [
            "triaged", "accepted"]
    finally:
        store.close()
