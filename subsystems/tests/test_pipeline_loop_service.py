"""PipelineFinished CI 触发闭环 hermetic 测试（FR-EX-008，S4.4）。

tempfile 真 sqlite + 真 SqliteQueue + RecordedTestRunner（tmp 夹具），不起端口、
不碰真机/真仓。覆盖：失败闭环全链路、重放幂等、成功 no-op、证据矛盾/缺件
fail-closed、重跑配额、派发死信隔离、草稿 dry-run（outbox 不被外部派发）、
摄入延迟 P95 收集。
"""

from __future__ import annotations

import json
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from testpilot_adapters.json_events import JsonReplayEventSource
from testpilot_adapters.recorded_runner import RecordedTestRunner
from testpilot_adapters.store import SqliteQueue, bootstrap_store
from testpilot_adapters.tracers import NoopTracer
from testpilot_core.approval import TICKET_TARGET_SYSTEM
from testpilot_core.enums import CaseStatus, RunStatus
from testpilot_core.errors import AdapterUnavailableError
from testpilot_core.execution import run_id_for
from testpilot_core.ingest_latency import EVENT_REINDEX_TRIGGER
from testpilot_core.models import (
    ArtifactRef,
    CaseResult,
    EnvironmentInfo,
    EventEnvelope,
    TestRun,
)
from testpilot_workers.attribution_service import (
    AttributionService,
    NullEvidenceProvider,
)
from testpilot_workers.event_dispatch import (
    dispatch_events,
    register_handler,
    reset_handlers,
)
from testpilot_workers.execution_service import RerunQuotaExceededError
from testpilot_workers.ingest_latency import collect_latency_reports
from testpilot_workers.outbox_dispatch import dispatch_pending_outbox
from testpilot_workers.pipeline_loop import (
    _rerun_idempotency_key,
    handle_pipeline_finished,
)

_COMMIT = "a1b2c3d4e5f6"
_PROJECT = "AlarmKitSpike-demo"
_T0 = datetime(2026, 11, 11, 8, 0, 0, tzinfo=timezone.utc)


class _Limits:
    daily_rerun_quota = 2
    request_timeout_s = 5
    max_concurrency = 1
    execution_max_attempts = 3
    execution_lease_seconds = 300


class _Container:
    def __init__(self, tmp_path: Path, *, runner=True, quota: int = 2):
        store = bootstrap_store(tmp_path / "data")
        fixtures_dir = tmp_path / "fixtures"
        self.fixtures_dir = fixtures_dir
        adapters = {
            "store": store,
            "queue": SqliteQueue(store.conn),
            "tracer": NoopTracer(),
            "fixtures_dir": fixtures_dir,
            "attribution": AttributionService(NullEvidenceProvider()),
        }
        if runner:
            adapters["runner"] = RecordedTestRunner(fixtures_dir / "runs")
        self.adapters = adapters
        limits = _Limits()
        limits.daily_rerun_quota = quota
        self.config = types.SimpleNamespace(limits=limits)

    def get(self, name: str):
        return self.adapters.get(name)


def _write_ci_run(fixtures_dir: Path, *, name: str = "ci-run",
                  commit: str = _COMMIT,
                  status: RunStatus = RunStatus.FAILED) -> str:
    """自造 CI TestRun 报告（1 passed + 1 failed，含制品与未覆盖层）。"""
    run_dir = fixtures_dir / "runs"
    run_dir.mkdir(parents=True, exist_ok=True)
    run = TestRun(
        run_id=f"ci-{name}-001",
        idempotency_key=f"ci-report-{name}",
        project=_PROJECT,
        commit_sha=commit,
        build_id="ci-42",
        status=status,
        environment=EnvironmentInfo(
            provider="ios-simulator", env_id="probe-udid",
            capabilities=["ios.simulator"], version="iOS 27.0",
        ),
        started_at=_T0,
        finished_at=_T0 + timedelta(seconds=70),
        duration_ms=70000,
        case_results=[
            CaseResult(
                external_case_id="AlarmTests/testSchedulePrimary",
                node_id="AlarmTests/testSchedulePrimary",
                status=CaseStatus.PASSED, duration_ms=42,
            ),
            CaseResult(
                external_case_id="AlarmTests/testKillBackupInterval",
                node_id="AlarmTests/testKillBackupInterval",
                status=CaseStatus.FAILED, duration_ms=58,
                failure_type="XCTAssertionError",
                message="XCTAssertEqual failed",
                stack_top="AlarmMissionTests.swift:88",
                artifact_urns=["urn:testpilot:localfs-artifact:xcresult:hostless@a1b2"],
            ),
        ],
        artifacts=[
            ArtifactRef(type="xcresult",
                        uri="file://fixtures/runs/hostless.xcresult",
                        content_hash="a1b2c3d4e5f6"),
        ],
        raw_report_ref="file://fixtures/runs/hostless.xcresult",
        uncovered_layers=["ios.device", "signing"],
    )
    path = run_dir / f"{name}.json"
    path.write_text(run.model_dump_json(), encoding="utf-8")
    return f"file://fixtures/runs/{name}.json"


def _event(event_id: str = "evt-pipeline-1", *, result: str = "failed",
           commit: str = _COMMIT, payload_uri: str | None = None) -> EventEnvelope:
    return EventEnvelope(
        event_id=event_id,
        event_type="PipelineFinished",
        source_system="json-ci",
        occurred_at=_T0 - timedelta(seconds=5),
        project=_PROJECT,
        subject_ref=f"urn:testpilot:json-ci:pipeline:{event_id}",
        payload_uri=payload_uri or "file://fixtures/runs/ci-run.json",
        payload={"build_id": "ci-42", "commit_sha": commit, "result": result},
        idempotency_key=f"pipeline-{event_id}",
    )


@pytest.mark.asyncio
async def test_failed_pipeline_closes_the_full_loop(tmp_path):
    container = _Container(tmp_path)
    uri = _write_ci_run(container.fixtures_dir)
    event = _event(payload_uri=uri)
    store = container.get("store")

    report = await handle_pipeline_finished(container, event)

    assert report.outcome == "closed"
    assert report.failure_case_ids == ["AlarmTests/testKillBackupInterval"]
    assert report.ci_run_id == "ci-ci-run-001"
    assert report.rerun_status == RunStatus.FAILED.value

    # 1) CI 外部 run 已入账本，重跑 run 内容寻址自 event_id
    ci_row = store.get_test_run_row(report.ci_run_id)
    assert ci_row is not None and ci_row["status"] == RunStatus.FAILED.value
    rerun_key = _rerun_idempotency_key(event)
    rerun_row = store.get_test_run_by_idempotency(rerun_key)
    assert rerun_row is not None
    assert rerun_row["run_id"] == run_id_for(rerun_key) == report.rerun_run_id

    # 2) 选择重跑请求只带失败用例，且占每日重跑配额
    request = json.loads(rerun_row["request_json"])
    assert request["selector"]["test_ids"] == ["AlarmTests/testKillBackupInterval"]
    assert request["daily_budget_key"] == f"ci-rerun:{_PROJECT}:{_COMMIT[:12]}"
    assert request["triggered_by"] == "ci"
    budget = store.list_budget_entries(request["daily_budget_key"],
                                       datetime.now(timezone.utc).date().isoformat())
    assert len(budget) == 1

    # 3) 归因随重跑 TestRun 落 raw_json；Null 证据 → needs_human（不硬归因）
    rerun = store.row_to_test_run(rerun_row)
    assert len(rerun.attributions) == 1
    assert report.needs_human == 1

    # 4) R3 待审草稿：approval pending + outbox pending，请求体四要素齐全
    outbox = store.get_outbox_record(report.draft_idempotency_key)
    assert outbox is not None
    assert outbox.target_system == TICKET_TARGET_SYSTEM
    assert str(outbox.action_level) == "R3"
    approval = store.get_approval(outbox.approval_id)
    assert approval is not None and str(approval.decision) == "pending"
    body = outbox.request_body
    assert body["rerun_run_id"] == report.rerun_run_id
    assert body["source_run_id"] == report.ci_run_id
    assert body["failed_case_ids"] == ["AlarmTests/testKillBackupInterval"]
    assert body["evidence_uris"] and "uncovered_layers" in body
    assert body["uncovered_layers"] == ["ios.device", "signing"]


@pytest.mark.asyncio
async def test_replay_is_idempotent_zero_duplication(tmp_path):
    container = _Container(tmp_path)
    uri = _write_ci_run(container.fixtures_dir)
    event = _event(payload_uri=uri)
    store = container.get("store")

    first = await handle_pipeline_finished(container, event)
    runs_after_first = len(store.list_test_runs())
    outbox_after_first = len(store.list_outbox_records())
    second = await handle_pipeline_finished(container, event)

    assert second.outcome == "replayed"
    assert second.rerun_run_id == first.rerun_run_id
    assert second.draft_idempotency_key == first.draft_idempotency_key
    assert len(store.list_test_runs()) == runs_after_first
    assert len(store.list_outbox_records()) == outbox_after_first
    budget_key = f"ci-rerun:{_PROJECT}:{_COMMIT[:12]}"
    budget = store.list_budget_entries(
        budget_key, datetime.now(timezone.utc).date().isoformat())
    assert len(budget) == 1  # 重放不重复扣配额


@pytest.mark.asyncio
async def test_succeeded_pipeline_is_noop_without_writes(tmp_path):
    container = _Container(tmp_path)
    report = await handle_pipeline_finished(
        container, _event(result="succeeded"))
    assert report.outcome == "no_failure"
    assert container.get("store").list_test_runs() == []
    assert container.get("store").list_outbox_records() == []


@pytest.mark.asyncio
async def test_unknown_result_fails_loudly(tmp_path):
    container = _Container(tmp_path)
    with pytest.raises(ValueError, match="result 非法"):
        await handle_pipeline_finished(container, _event(result="flaky"))


@pytest.mark.asyncio
async def test_missing_report_file_raises(tmp_path):
    container = _Container(tmp_path)
    event = _event(payload_uri="file://fixtures/runs/ghost.json")
    with pytest.raises(AdapterUnavailableError, match="不存在"):
        await handle_pipeline_finished(container, event)


@pytest.mark.asyncio
async def test_commit_mismatch_between_event_and_report_raises(tmp_path):
    container = _Container(tmp_path)
    uri = _write_ci_run(container.fixtures_dir, commit="0" * 12)
    with pytest.raises(ValueError, match="矛盾"):
        await handle_pipeline_finished(container, _event(payload_uri=uri))


@pytest.mark.asyncio
async def test_runner_not_assembled_fails_closed(tmp_path):
    container = _Container(tmp_path, runner=False)
    uri = _write_ci_run(container.fixtures_dir)
    with pytest.raises(AdapterUnavailableError, match="runner"):
        await handle_pipeline_finished(container, _event(payload_uri=uri))


@pytest.mark.asyncio
async def test_rerun_quota_exhausted_rejects_without_phantom(tmp_path):
    container = _Container(tmp_path, quota=0)
    uri = _write_ci_run(container.fixtures_dir)
    store = container.get("store")
    with pytest.raises(RerunQuotaExceededError):
        await handle_pipeline_finished(container, _event(payload_uri=uri))
    # 配额拒入：CI run 已入账但无重跑幻影行、无草稿
    assert len(store.list_test_runs()) == 1
    assert store.list_outbox_records() == []


@pytest.mark.asyncio
async def test_dispatch_routes_pipeline_event_and_isolates_bad_sibling(tmp_path):
    container = _Container(tmp_path)
    uri = _write_ci_run(container.fixtures_dir)
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    good = _event(payload_uri=uri).model_dump(mode="json")
    (events_dir / "good.json").write_text(
        json.dumps(good, ensure_ascii=False), encoding="utf-8")
    # 坏件：缺 event_id 的信封，解析关即进死信，不阻断好事件
    (events_dir / "bad.json").write_text(
        json.dumps({"event_type": "PipelineFinished"}, ensure_ascii=False),
        encoding="utf-8")

    reset_handlers()
    register_handler("PipelineFinished", handle_pipeline_finished)
    report = await dispatch_events(
        container, event_source=JsonReplayEventSource(events_dir))

    assert report.scanned == 2 and report.processed == 1 and report.dead == 1
    store = container.get("store")
    assert store.list_events()[0]["status"] == "processed"
    assert len(store.list_dead_letters()) == 1
    reset_handlers()


@pytest.mark.asyncio
async def test_ticket_draft_stays_pending_under_outbox_dispatch(tmp_path):
    container = _Container(tmp_path)
    uri = _write_ci_run(container.fixtures_dir)
    report = await handle_pipeline_finished(
        container, _event(payload_uri=uri))

    # 未接 TicketPort 写路由：派发器显式 skipped_unsupported，草稿保留 pending，
    # 证明 dry-run 下没有任何外部写
    dispatch = await dispatch_pending_outbox(container)
    assert dispatch["skipped_unsupported"] == 1
    outbox = container.get("store").get_outbox_record(
        report.draft_idempotency_key)
    assert str(outbox.status) == "pending"


def test_latency_collector_builds_intake_and_end_to_end_reports(tmp_path):
    container = _Container(tmp_path)
    store = container.get("store")
    occurred = _T0
    received = _T0 + timedelta(seconds=3)
    event = EventEnvelope(
        event_id="evt-commit-1", event_type="CommitPushed",
        source_system="json-git", occurred_at=occurred,
        project=_PROJECT, subject_ref="deadbeef",
        payload={"base_commit": "1" * 40, "head_commit": "2" * 40},
        idempotency_key="commit-evt-1",
    )
    assert store.record_event_if_new(event) is True
    # received_at 默认取挂钟；合成时钟测试显式对齐为 occurred+3s
    store.conn.execute(
        "UPDATE events SET received_at=? WHERE event_id=?",
        (received.isoformat(), event.event_id),
    )
    store.conn.commit()
    # event-reindex 批次 finished_at 回配 event_id（S4.2 freshness 留痕同形）
    run_id = "reidx-test01"
    store.start_ingestion_run(run_id, "fs-docs", EVENT_REINDEX_TRIGGER)
    store.finish_ingestion_run(
        run_id, "succeeded", {"chunks_total": 2},
        {"event_id": event.event_id, "latency_ms": 42000},
    )
    # 手工把 finished_at 拨到 occurred+42s（start/finish 默认取挂钟，测试显式对齐）
    store.conn.execute(
        "UPDATE ingestion_runs SET finished_at=?, started_at=? WHERE run_id=?",
        ((occurred + timedelta(seconds=42)).isoformat(),
         received.isoformat(), run_id),
    )
    store.conn.commit()

    reports = collect_latency_reports(store)
    assert reports["intake"].sample_count == 1
    assert reports["intake"].p95_s == 3.0
    assert reports["end_to_end"].sample_count == 1
    assert reports["end_to_end"].p95_s == 42.0
    assert reports["end_to_end"].gate_passed is True
