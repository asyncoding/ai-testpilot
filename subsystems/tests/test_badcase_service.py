"""bad case 闭环 worker 服务测试（S2.6c FR-EV-005）。

不拉起完整容器：_ShimContainer 只暴露 get("store")，状态全落 tmp SQLite。
覆盖：负反馈投影/👍 跳过/幂等巡检、qa_runs 四元组快照补齐、孤儿反馈防御、
分诊→转 golden（题集冻结+状态翻转）→回归关联（幽灵 run 拒入）全链路、
NotFound/BadCase 错误映射与统计口径。
"""

from __future__ import annotations

import json

import pytest

from testpilot_adapters.store import bootstrap_store
from testpilot_core.badcase import GOLDEN_LOCKED_VERSION
from testpilot_core.enums import FeedbackKind, FeedbackReason
from testpilot_core.errors import NotFoundError
from testpilot_core.models import EvalRun, EvalVersion, Feedback
from testpilot_workers import badcase_service as svc


class _ShimContainer:
    """worker 只依赖 get(name) 的最小容器。"""

    def __init__(self, store):
        self._adapters = {"store": store}

    def get(self, name):
        return self._adapters.get(name)


def _seed_qa_run(store, run_id: str = "run-1", *,
                 question: str = "闹钟响铃时如何快速停掉？") -> None:
    store.insert_qa_run(record={
        "run_id": run_id, "question": question, "status": "succeeded",
        "data_version": "d7", "model_version": "m3",
        "prompt_version": "p2", "params_version": "x1",
    })


def _seed_feedback(store, *, run_id: str = "run-1", kind: FeedbackKind =
                   FeedbackKind.NOT_USEFUL,
                   reason: FeedbackReason | None = FeedbackReason.STALE_REFERENCE,
                   detail: str = "引用的是旧版停闹钟逻辑") -> int:
    return store.add_feedback(Feedback(
        run_id=run_id, target_urn=f"urn:testpilot:qa:run:{run_id}",
        kind=kind, reason=reason, detail=detail, model_version="m3"))


# ── 摄入：投影、跳过、幂等 ──────────────────────────────────────────────────

def test_ingest_projects_not_useful_and_skips_useful(tmp_path):
    store = bootstrap_store(tmp_path)
    try:
        _seed_qa_run(store)
        _seed_feedback(store, kind=FeedbackKind.USEFUL, reason=None, detail="赞")
        fb_id = _seed_feedback(store)

        report = svc.ingest_feedback(_ShimContainer(store))
        assert report == {
            "status": "succeeded", "scanned": 2, "ingested": 1,
            "skipped_useful": 1, "skipped_duplicate": 0,
            "bad_case_ids": report["bad_case_ids"],  # 内容寻址 ID 另查
        }
        case = store.get_bad_case_by_feedback(str(fb_id))
        assert case is not None
        assert case.source == "qa_feedback"
        assert case.source_ref == f"run-1#feedback-{fb_id}"
        assert case.feedback_id == str(fb_id)
        assert case.status == "open"
        assert case.question == "闹钟响铃时如何快速停掉？"
        assert case.reason == "stale_reference" and "旧版" in case.detail
        # 四元组快照从 qa_runs 关联补齐，不采信反馈行的残缺口径
        assert case.snapshot.model_dump() == {
            "data_version": "d7", "model_version": "m3",
            "prompt_version": "p2", "params_version": "x1"}
        assert case.history[0].actor == "system"
    finally:
        store.close()


def test_ingest_is_idempotent_on_resweep(tmp_path):
    store = bootstrap_store(tmp_path)
    try:
        _seed_qa_run(store)
        fb_id = _seed_feedback(store)
        first = svc.ingest_feedback(_ShimContainer(store))
        second = svc.ingest_feedback(_ShimContainer(store))
        assert first["ingested"] == 1 and second["ingested"] == 0
        assert second["skipped_duplicate"] == 1 and second["scanned"] == 1
        # 台账不增生
        assert len(store.list_bad_cases()) == 1
        assert store.get_bad_case_by_feedback(str(fb_id)) is not None
    finally:
        store.close()


def test_ingest_empty_feedback_returns_zero_report(tmp_path):
    store = bootstrap_store(tmp_path)
    try:
        report = svc.ingest_feedback(_ShimContainer(store))
        assert report["scanned"] == 0 and report["ingested"] == 0
        assert report["bad_case_ids"] == []
    finally:
        store.close()


def test_ingest_orphan_feedback_without_qa_run_is_defensive(tmp_path):
    store = bootstrap_store(tmp_path)
    try:
        # 直接落反馈行、不建 qa_run（正常管线 record_feedback 会拦，这里测防御分支）
        fb_id = store.add_feedback(Feedback(
            run_id="ghost-run", target_urn="urn:testpilot:qa:run:ghost-run",
            kind=FeedbackKind.NOT_USEFUL, reason=FeedbackReason.OTHER,
            model_version="m9"))
        report = svc.ingest_feedback(_ShimContainer(store))
        assert report["ingested"] == 1
        case = store.get_bad_case_by_feedback(str(fb_id))
        assert case.question == ""  # 缺 run 不编造问题
        assert case.snapshot.model_version == "m9"  # 反馈行自带版本仍保留
        assert case.snapshot.data_version == ""
    finally:
        store.close()


# ── 全生命周期：分诊 → golden → 回归闭环 ───────────────────────────────────

def test_full_lifecycle_triage_promote_resolve(tmp_path):
    store = bootstrap_store(tmp_path)
    try:
        container = _ShimContainer(store)
        _seed_qa_run(store)
        fb_id = _seed_feedback(store)
        svc.ingest_feedback(container)
        case = store.get_bad_case_by_feedback(str(fb_id))

        accepted = svc.triage_case(
            container, case.bad_case_id, decision="accept", actor="qa-li",
            reason="factual_error", note="本地复现，停闹钟路径答错")
        assert accepted.status == "accepted" and accepted.triaged_by == "qa-li"
        assert [e.to_status for e in accepted.history] == [
            "open", "triaged", "accepted"]

        promoted = svc.promote_case(
            container, case.bad_case_id, actor="qa-li", note="纳入回归集")
        bc = promoted["bad_case"]
        assert bc.status == "promoted"
        assert bc.golden_case_id == f"bad-case:{case.bad_case_id}"
        # golden 题集冻结：kind/locked_version/溯源四要素
        eval_row = store.conn.execute(
            "SELECT * FROM eval_cases WHERE case_id=?",
            (bc.golden_case_id,)).fetchone()
        assert eval_row["kind"] == "bad_case"
        assert eval_row["locked_version"] == GOLDEN_LOCKED_VERSION
        gt = json.loads(eval_row["ground_truth"])
        assert gt["provenance"] == {
            "bad_case_id": case.bad_case_id, "source": "qa_feedback",
            "source_ref": f"run-1#feedback-{fb_id}",
            "feedback_id": str(fb_id)}
        assert promoted["eval_case"].case_id == bc.golden_case_id

        # 回归 run 必须真实存在，幽灵 ID 拒入
        with pytest.raises(NotFoundError, match="幽灵"):
            svc.associate_regression_run(
                container, case.bad_case_id, eval_run_id="eval-ghost",
                outcome="pass", actor="ci-bot")

        store.add_eval_run(EvalRun(
            eval_run_id="eval-42",
            version=EvalVersion(data_version="d8", model_version="m4",
                                prompt_version="p3", params_version="x1"),
            metrics={"bad_case_regression": 1.0}))
        failed = svc.associate_regression_run(
            container, case.bad_case_id, eval_run_id="eval-42",
            outcome="fail", actor="ci-bot")
        assert failed.status == "regression_failed"
        assert failed.closed_at is None  # 复测失败不闭环
        assert failed.regression_eval_run_id == "eval-42"

        store.add_eval_run(EvalRun(
            eval_run_id="eval-43",
            version=EvalVersion(data_version="d9", model_version="m4",
                                prompt_version="p3", params_version="x1")))
        fixed = svc.associate_regression_run(
            container, case.bad_case_id, eval_run_id="eval-43",
            outcome="pass", actor="ci-bot")
        assert fixed.status == "fixed" and fixed.is_closed
        assert fixed.closed_at is not None
    finally:
        store.close()


def test_triage_reject_closes_and_promote_is_rejected(tmp_path):
    store = bootstrap_store(tmp_path)
    try:
        container = _ShimContainer(store)
        _seed_qa_run(store)
        _seed_feedback(store, reason=FeedbackReason.OFF_TOPIC)
        svc.ingest_feedback(container)
        case = store.list_bad_cases()[0]

        rejected = svc.triage_case(
            container, case.bad_case_id, decision="reject", actor="qa-wang",
            note="与本工程无关的泛问")
        assert rejected.status == "rejected" and rejected.is_closed
        # 终态坏例不能转 golden（core fail-closed，ValueError 系）
        with pytest.raises(ValueError):
            svc.promote_case(container, case.bad_case_id, actor="qa-wang")
        # reject 路径不留 golden 幻影
        assert store.conn.execute(
            "SELECT COUNT(*) AS n FROM eval_cases WHERE kind='bad_case'"
        ).fetchone()["n"] == 0
    finally:
        store.close()


def test_unknown_bad_case_id_maps_to_not_found(tmp_path):
    store = bootstrap_store(tmp_path)
    try:
        container = _ShimContainer(store)
        with pytest.raises(NotFoundError):
            svc.triage_case(container, "bc-missing0000",
                            decision="accept", actor="x")
        with pytest.raises(NotFoundError):
            svc.promote_case(container, "bc-missing0000", actor="x")
    finally:
        store.close()


# ── 盘点与统计 ──────────────────────────────────────────────────────────────

def test_list_filters_and_stats(tmp_path):
    store = bootstrap_store(tmp_path)
    try:
        container = _ShimContainer(store)
        _seed_qa_run(store, "run-1")
        _seed_qa_run(store, "run-2", question="第二个问题")
        _seed_feedback(store, run_id="run-1")
        store.add_feedback(Feedback(
            run_id="run-2", target_urn="urn:testpilot:qa:run:run-2",
            kind=FeedbackKind.NOT_USEFUL, reason=FeedbackReason.FACTUAL_ERROR))
        svc.ingest_feedback(container)
        assert len(svc.list_bad_cases(container)) == 2
        assert len(svc.list_bad_cases(container, status="open")) == 2
        assert svc.list_bad_cases(container, status="fixed") == []

        first = store.list_bad_cases()[0]
        svc.triage_case(container, first.bad_case_id,
                        decision="reject", actor="qa-li")
        stats = svc.bad_case_stats(container)
        assert stats["total"] == 2
        assert stats["by_status"]["open"] == 1
        assert stats["by_status"]["rejected"] == 1
        assert stats["closed_count"] == 1 and stats["open_count"] == 1
        assert stats["closure_rate"] == 0.5
    finally:
        store.close()
