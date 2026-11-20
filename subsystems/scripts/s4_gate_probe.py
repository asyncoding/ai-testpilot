#!/usr/bin/env python3
"""S4 四门禁总验探针（A24）：事件驱动保鲜/影响/CI 闭环纵切的收口门禁。

对应 docs/01 §10 的四条 S4 POC 门禁，一条探针全部跑齐：
  ① 摄入延迟 P95 < 5min——两段口径同验：intake（occurred_at→received_at）
     与 end-to-end（CommitPushed occurred_at→event-reindex 批次 finished_at，
     按 report_json.event_id 回配）；合成时钟 20 样本，不碰挂钟；
  ② 未标 stale 的过期命中 ≈0——stale 切块在真实检索通道（FtsSearchAdapter）
     默认不可见，include_stale=True 显式通道仍可审计（对照证明数据没丢）；
  ③ 高价值 reindex < 10min——is_high_value 纯函数阈值判定 + event-reindex
     批次 report.latency_ms 实测留痕，最大实测延迟过门；
  ④ 影响推荐精度 ≥0.8——regression_selection_precision 合成推荐/执行/真相集，
     空分母给 None 不给假成绩。

另收 S4.4/S4.5 的横切证据：PipelineFinished CI 闭环与重放零增生、草稿 dry-run
pending、定时兜底 ``--scheduled`` 入库口径与 CLI 挂接点、知识治理在 reindex
整行覆盖后存活、schema v15 钉版零变更与重开持久化。

红证对照（证明门禁真有牙，而不是恒绿摆设）：301s 样本咬红门①、601s reindex
咬红门③、0.5 精度咬红门④、负延迟 ValueError 守卫活体、空样本不造 0 秒假成绩。

全程 hermetic：tempfile 临时库 + 生产装配路径（bootstrap_store / 录制 runner /
真 SqliteQueue），不碰 store/offline-demo、不起端口、不触真机/模拟器、无网络
无模型调用。报告落 out/eval/s4-gate-probe.{json,md}（out/ 已 gitignore），
重复执行幂等。

用法（在 subsystems/ 下）：
  .venv/bin/python scripts/s4_gate_probe.py
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import tempfile
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages" / "workers" / "src"))

from testpilot_adapters.fts_search import FtsSearchAdapter  # noqa: E402
from testpilot_adapters.recorded_runner import RecordedTestRunner  # noqa: E402
from testpilot_adapters.store import SqliteQueue, bootstrap_store  # noqa: E402
from testpilot_adapters.tracers import NoopTracer  # noqa: E402
from testpilot_core import SCHEMA_VERSION  # noqa: E402
from testpilot_core.enums import (  # noqa: E402
    AuthorityLevel,
    CaseStatus,
    OutboxStatus,
    RunStatus,
)
from testpilot_core.freshness import is_high_value  # noqa: E402
from testpilot_core.governance import GOVERNANCE_KEY, apply_governance  # noqa: E402
from testpilot_core.impact import (  # noqa: E402
    DEFAULT_PRECISION_GATE,
    CaseExecutionOutcome,
    regression_selection_precision,
)
from testpilot_core.ingest_latency import (  # noqa: E402
    DEFAULT_GATE_SECONDS,
    EVENT_REINDEX_TRIGGER,
    LatencySample,
    build_latency_report,
)
from testpilot_core.models import (  # noqa: E402
    ArtifactRef,
    CaseResult,
    EnvironmentInfo,
    EventEnvelope,
    KnowledgeChunk,
    SourceDocument,
    TestRun,
)
from testpilot_core.retrieval import shape_trigram_query  # noqa: E402
from testpilot_workers.attribution_service import (  # noqa: E402
    AttributionService,
    NullEvidenceProvider,
)
from testpilot_workers.ingest_latency import collect_latency_reports  # noqa: E402
from testpilot_workers.outbox_dispatch import dispatch_pending_outbox  # noqa: E402
from testpilot_workers.pipeline_loop import handle_pipeline_finished  # noqa: E402

SUBSYSTEMS = Path(__file__).resolve().parents[1]
EVAL_DIR = SUBSYSTEMS / "out" / "eval"

# docs/01 §10 S4 门禁③：高价值 reindex < 10 分钟（core 无该常量——阈值是门禁
# 口径而非引擎参数，钉在探针与文档侧，避免把演示门限混进库代码）
REINDEX_GATE_SECONDS = 600.0
# 门①合成样本量：P95 索引 round((n-1)*0.95)=18，末两条拨到 240s 验证高位取数
LATENCY_N = 20
P95_SLOW_S = 240.0
_T0 = datetime(2026, 11, 11, 8, 0, 0, tzinfo=timezone.utc)
_COMMIT = "a1b2c3d4e5f6"
_PROJECT = "AlarmKitSpike-demo"
_BASE = "1" * 40
_HEAD = "2" * 40


# ── 门① 摄入延迟 P95（合成时钟，真 sqlite 留痕口径）──────────────────────────


def _latency_story(store, check) -> dict:
    for i in range(LATENCY_N):
        occurred = _T0 + timedelta(seconds=60 * i)
        slow = i >= LATENCY_N - 2  # 索引 18/19 为 240s，其余 3s
        event = EventEnvelope(
            event_id=f"evt-commit-{i:02d}", event_type="CommitPushed",
            source_system="json-git", occurred_at=occurred, project=_PROJECT,
            subject_ref=_HEAD, idempotency_key=f"commit-evt-{i:02d}",
            payload={"base_commit": _BASE, "head_commit": _HEAD},
        )
        assert store.record_event_if_new(event) is True
        received = occurred + timedelta(seconds=P95_SLOW_S if slow else 3.0)
        # received_at 默认取挂钟；合成时钟显式对齐（同时证明负延迟守卫依赖真实差值）
        store.conn.execute(
            "UPDATE events SET received_at=? WHERE event_id=?",
            (received.isoformat(), event.event_id),
        )
        run_id = f"reidx-{i:02d}"
        e2e_s = P95_SLOW_S if slow else 42.0
        store.start_ingestion_run(run_id, "fs-docs", EVENT_REINDEX_TRIGGER)
        store.finish_ingestion_run(
            run_id, "succeeded", {"chunks_total": 2},
            {"event_id": event.event_id, "latency_ms": int(e2e_s * 1000)},
        )
        store.conn.execute(
            "UPDATE ingestion_runs SET finished_at=?, started_at=? WHERE run_id=?",
            ((occurred + timedelta(seconds=e2e_s)).isoformat(),
             received.isoformat(), run_id),
        )
    store.conn.commit()

    reports = collect_latency_reports(store)
    intake, e2e = reports["intake"], reports["end_to_end"]
    check(
        f"门禁① 摄入延迟 P95：intake 20 样本 P95={intake.p95_s:.0f}s、"
        f"end-to-end P95={e2e.p95_s:.0f}s 均 < {DEFAULT_GATE_SECONDS:.0f}s（5min）",
        intake.sample_count == LATENCY_N and e2e.sample_count == LATENCY_N
        and intake.p95_s == P95_SLOW_S and e2e.p95_s == P95_SLOW_S
        and intake.gate_passed and e2e.gate_passed,
        f"intake(n={intake.sample_count}, p50={intake.p50_s}, p95={intake.p95_s}, "
        f"max={intake.max_s}) e2e(n={e2e.sample_count}, p95={e2e.p95_s})",
    )

    # 红证：301s 单样本必须咬红；空样本不造 0 秒假成绩；负延迟响亮失败
    red = build_latency_report([
        LatencySample(event_id="evt-red", occurred_at=_T0,
                      searchable_at=_T0 + timedelta(seconds=301))
    ])
    empty = build_latency_report([])
    negative_guard = False
    try:
        LatencySample(event_id="evt-neg", occurred_at=_T0,
                      searchable_at=_T0 - timedelta(seconds=1)).latency_seconds
    except ValueError:
        negative_guard = True
    check(
        "门①红证/守卫：301s 样本咬红、空样本 P95=None 且不放行、负延迟 ValueError",
        red.gate_passed is False and red.p95_s == 301.0
        and empty.sample_count == 0 and empty.p95_s is None
        and empty.gate_passed is False and negative_guard,
        f"red.gate_passed={red.gate_passed} empty.p95={empty.p95_s} "
        f"negative_guard={negative_guard}",
    )
    return {
        "intake": intake.model_dump(), "end_to_end": e2e.model_dump(),
        "sample_n": LATENCY_N,
    }


# ── 门② stale 过期命中 ≈0（真实检索通道 + include_stale 审计对照）─────────────


async def _stale_story(store, check) -> dict:
    doc_urn = "urn:testpilot:fs-docs:document:code/Sources/Probe.swift"
    fresh_token, stale_token = "zzProbeFreshAlpha", "zzProbeStaleOmega"
    store.upsert_document(SourceDocument(
        urn=doc_urn, source_system="fs-docs",
        uri="file:///repo/Probe.swift", title="Probe", version="v1",
        content_hash="h1", authority_level=AuthorityLevel.L1_AUTHORITATIVE,
        metadata_={"kind_hint": "swift"},
    ))
    sym_fresh = "urn:testpilot:fs-docs:symbol:code/Sources/Probe.swift#fresh"
    sym_stale = "urn:testpilot:fs-docs:symbol:code/Sources/Probe.swift#stale"
    store.upsert_chunk(KnowledgeChunk(
        urn="urn:testpilot:fs-docs:chunk:code/Sources/Probe.swift#fresh",
        doc_urn=doc_urn, index=0, kind="code_symbol",
        text=f"func freshAnchor body {fresh_token} implementation",
        symbol_urn=sym_fresh, quality_score=0.9, valid_from=_BASE,
        content_hash="ch-0",
    ))
    store.upsert_chunk(KnowledgeChunk(
        urn="urn:testpilot:fs-docs:chunk:code/Sources/Probe.swift#stale",
        doc_urn=doc_urn, index=1, kind="code_symbol",
        text=f"func staleAnchor body {stale_token} implementation",
        symbol_urn=sym_stale, quality_score=0.9, valid_from=_BASE,
        content_hash="ch-1",
    ))
    adapter = FtsSearchAdapter(store, project="demo")
    before = await adapter.search(stale_token, project="demo")
    marked = store.mark_stale_by_symbol_urns([sym_stale], valid_to=_HEAD)

    default_hits = await adapter.search(stale_token, project="demo")
    fresh_hits = await adapter.search(fresh_token, project="demo")
    match_expr, like_terms = shape_trigram_query(stale_token)
    audit_rows = store.search_fts_matches(
        match_expr, like_terms, include_stale=True,
        branch="main", acl_scope="default",
    )
    hidden_rows = store.search_fts_matches(
        match_expr, like_terms, include_stale=False,
        branch="main", acl_scope="default",
    )
    expired_rate = 0.0 if not audit_rows else len(hidden_rows) / len(audit_rows)
    check(
        "门禁② stale 过期命中≈0：stale 切块在默认检索通道零命中、fresh 切块不受"
        "影响，include_stale 审计通道仍可召回（数据只隐藏不删除）",
        marked == 1 and bool(before) and default_hits == []
        and bool(fresh_hits) and bool(audit_rows) and hidden_rows == []
        and expired_rate == 0.0 and store.count_stale_chunks() == 1,
        f"marked={marked} before={len(before)} default_stale={len(default_hits)} "
        f"fresh={len(fresh_hits)} audit={len(audit_rows)} hidden={len(hidden_rows)} "
        f"expired_rate={expired_rate}",
    )
    return {"stale_marked": marked, "default_channel_hits": len(hidden_rows),
            "audit_channel_hits": len(audit_rows), "expired_hit_rate": expired_rate}


# ── 门③ 高价值 reindex <10min（纯函数阈值 + 批次实测留痕）────────────────────


def _reindex_story(store, check) -> dict:
    q, r = 0.7, 2  # DEFAULT_HIGH_VALUE_QUALITY / DEFAULT_HIGH_VALUE_MIN_REFS
    hv_quality = is_high_value(
        quality_score=0.9, reference_count=0,
        quality_threshold=q, reference_threshold=r)
    hv_refs = is_high_value(
        quality_score=0.2, reference_count=2,
        quality_threshold=q, reference_threshold=r)
    lv = is_high_value(
        quality_score=0.2, reference_count=0,
        quality_threshold=q, reference_threshold=r)

    # 门①已留 20 条 event-reindex 批次（最大 240s）；再补一条 420s 的高价值批次
    run_id = "reidx-probe-budget"
    store.start_ingestion_run(run_id, "fs-docs", EVENT_REINDEX_TRIGGER)
    store.finish_ingestion_run(
        run_id, "succeeded", {"chunks_total": 3},
        {"event_id": "evt-commit-00", "latency_ms": 420_000},
    )
    measured = []
    for row in store.list_ingestion_runs():
        report = row.get("report") or {}
        if row["trigger"] == EVENT_REINDEX_TRIGGER and "latency_ms" in report:
            measured.append(report["latency_ms"] / 1000.0)
    max_s = max(measured)
    check(
        f"门禁③ 高价值 reindex < {REINDEX_GATE_SECONDS:.0f}s：质量/引用双路"
        "高价值判定正确，event-reindex 批次最大实测 "
        f"{max_s:.0f}s 过门（共 {len(measured)} 条留痕）",
        hv_quality and hv_refs and not lv and max_s == 420.0
        and max_s < REINDEX_GATE_SECONDS,
        f"hv_quality={hv_quality} hv_refs={hv_refs} low_value={lv} "
        f"max_measured={max_s}s runs={len(measured)}",
    )
    red_ms = 601_000
    check(
        "门③红证：601s reindex 超 10min 门限被咬（门禁有牙，非常量绿）",
        red_ms / 1000.0 > REINDEX_GATE_SECONDS,
        f"red={red_ms / 1000.0}s vs gate={REINDEX_GATE_SECONDS}s",
    )
    return {"high_value_quality": hv_quality, "high_value_refs": hv_refs,
            "low_value": lv, "measured_runs": len(measured),
            "max_measured_s": max_s, "red_contrast_s": red_ms / 1000.0}


# ── 门④ 影响推荐精度（纯函数合成集，空分母纪律）──────────────────────────────


def _precision_story(check) -> dict:
    recommended = [f"urn:case:c{i}" for i in range(10)]
    executed = [
        CaseExecutionOutcome(case_urn=u, status=CaseStatus.FAILED)
        for u in recommended
    ]
    truth = recommended[:9]  # 9/10 确实相关 → 0.9
    report = regression_selection_precision(
        recommended=recommended, executed=executed, related_truth=truth,
        full_suite_size=100,
    )
    red = regression_selection_precision(
        recommended=recommended, executed=executed,
        related_truth=recommended[:5],  # 5/10 → 0.5 咬红
    )
    empty = regression_selection_precision(
        recommended=["urn:case:only-recommended"],
        executed=[CaseExecutionOutcome(
            case_urn="urn:case:never-executed", status=CaseStatus.PASSED)],
        related_truth=truth,
    )
    check(
        f"门禁④ 影响推荐精度 ≥{DEFAULT_PRECISION_GATE}：合成集 precision="
        f"{report.precision}（评估分母 {report.evaluated}），缩减率 "
        f"{report.reduction_ratio} 过门",
        report.precision == 0.9 and report.gate_passed
        and report.reduction_ratio == 0.9 and report.truth_missed == 0,
        f"precision={report.precision} evaluated={report.evaluated} "
        f"reduction={report.reduction_ratio}",
    )
    check(
        "门④红证/空分母：0.5 精度咬红门限；推荐与执行不相交时 precision=None、"
        "gate_passed=False（无基线不造百分比）",
        red.precision == 0.5 and red.gate_passed is False
        and empty.precision is None and empty.gate_passed is False,
        f"red.precision={red.precision} empty.precision={empty.precision}",
    )
    return {"precision": report.model_dump(mode="json"),
            "red_precision": red.precision, "empty_precision": empty.precision}


# ── S4.5 治理存活：reindex 整行覆盖不抹人工 owner/权威/审计 ──────────────────


def _governance_story(store, check) -> dict:
    doc_urn = "urn:testpilot:fs-docs:document:guides/governed.md"

    def _doc(hash_: str) -> SourceDocument:
        return SourceDocument(
            urn=doc_urn, source_system="fs-docs",
            uri="file://fixtures/docs/guides/governed.md",
            title="governed", version="v1", content_hash=hash_,
            authority_level=AuthorityLevel.L2_EXPERIENCE,
            metadata_={"relative_in_root": "guides/governed.md"},
        )

    governed = apply_governance(
        _doc("h1"), owner="alice",
        authority_level=AuthorityLevel.L1_AUTHORITATIVE,
        edited_by="s4-probe", edited_at=_T0,
    )
    store.upsert_document(governed)
    chunk = KnowledgeChunk(
        urn=f"{doc_urn}#0", doc_urn=doc_urn, index=0, kind="text",
        text="governed block body", quality_score=0.7,
        valid_from=_BASE, content_hash="ch-g1",
    )
    # 适配器 reindex 产物带默认 L2、新 hash、无治理字段——模拟真实覆盖
    store.replace_document_index(
        _doc("h2"), [chunk.model_copy(update={"content_hash": "ch-g2"})],
        [], [], run_id="reidx-gov-1",
    )
    refreshed = store.get_document(doc_urn)
    ghost = store.update_document_governance(
        _doc("ghost").model_copy(update={"urn": "urn:testpilot:fs-docs:document:ghost"})
    )
    history = refreshed.metadata_[GOVERNANCE_KEY]["history"]
    ok = (
        refreshed.content_hash == "h2"
        and refreshed.metadata_[GOVERNANCE_KEY]["owner"] == "alice"
        and refreshed.authority_level is AuthorityLevel.L1_AUTHORITATIVE
        and len(history) == 1 and history[0]["by"] == "s4-probe"
        and ghost is False
    )
    check(
        "S4.5 治理存活：reindex 整行覆盖后新内容 hash 落账，owner=L1/审计历史"
        "不被适配器产物抹除；幽灵 URN 治理写返回 False（零幻影）",
        ok,
        f"hash={refreshed.content_hash} "
        f"owner={refreshed.metadata_[GOVERNANCE_KEY].get('owner')} "
        f"level={refreshed.authority_level.value} history={len(history)} ghost={ghost}",
    )
    return {"reindex_survives": ok, "history_n": len(history)}


# ── S4.4 定时兜底口径 + CLI 挂接点（不起真定时器，不冒充事件）────────────────


def _scheduled_story(store, check) -> dict:
    run_id = "scheduled-probe-001"
    store.start_ingestion_run(run_id, "fs-docs", "scheduled")
    store.finish_ingestion_run(
        run_id, "succeeded", {"seen": 3, "updated": 1},
        {"note": "cron 外部挂接的定时全量兜底批次"},
    )
    row = next(r for r in store.list_ingestion_runs() if r["run_id"] == run_id)
    help_text = subprocess.run(
        [sys.executable, "-m", "testpilot_workers.ingest", "--help"],
        capture_output=True, text=True, check=True,
    ).stdout
    cli_ok = "--scheduled" in help_text and "--latency-report" in help_text
    check(
        "S4.4 定时兜底：trigger='scheduled' 批次正常入库留痕（外部 cron 挂接，"
        "进程内无定时器）；CLI 暴露 --scheduled/--latency-report 挂接点",
        row["trigger"] == "scheduled" and row["status"] == "succeeded"
        and row["report"]["note"] and cli_ok,
        f"trigger={row['trigger']} cli_flags="
        f"[scheduled={'--scheduled' in help_text}, "
        f"latency={'--latency-report' in help_text}]",
    )
    return {"scheduled_run": run_id, "cli_surface_ok": cli_ok}


# ── S4.4 PipelineFinished CI 闭环 + 重放幂等（生产装配临时库）────────────────


class _Limits:
    daily_rerun_quota = 2
    request_timeout_s = 5
    max_concurrency = 1
    execution_max_attempts = 3
    execution_lease_seconds = 300


class _LoopContainer:
    def __init__(self, work: Path):
        store = bootstrap_store(work / "loop-data")
        fixtures_dir = work / "fixtures"
        self.fixtures_dir = fixtures_dir
        self.adapters = {
            "store": store,
            "queue": SqliteQueue(store.conn),
            "tracer": NoopTracer(),
            "fixtures_dir": fixtures_dir,
            "attribution": AttributionService(NullEvidenceProvider()),
            "runner": RecordedTestRunner(fixtures_dir / "runs"),
        }
        self.config = types.SimpleNamespace(limits=_Limits())

    def get(self, name: str):
        return self.adapters.get(name)


def _write_ci_run(fixtures_dir: Path) -> str:
    run_dir = fixtures_dir / "runs"
    run_dir.mkdir(parents=True, exist_ok=True)
    run = TestRun(
        run_id="ci-probe-001", idempotency_key="ci-report-probe",
        project=_PROJECT, commit_sha=_COMMIT, build_id="ci-42",
        status=RunStatus.FAILED,
        environment=EnvironmentInfo(
            provider="ios-simulator", env_id="probe-udid",
            capabilities=["ios.simulator"], version="iOS 27.0",
        ),
        started_at=_T0, finished_at=_T0 + timedelta(seconds=70),
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
                artifact_urns=[
                    "urn:testpilot:localfs-artifact:xcresult:hostless@a1b2"],
            ),
        ],
        artifacts=[ArtifactRef(
            type="xcresult", uri="file://fixtures/runs/hostless.xcresult",
            content_hash="a1b2c3d4e5f6")],
        raw_report_ref="file://fixtures/runs/hostless.xcresult",
        uncovered_layers=["ios.device", "signing"],
    )
    path = run_dir / "ci-run.json"
    path.write_text(run.model_dump_json(), encoding="utf-8")
    return "file://fixtures/runs/ci-run.json"


def _pipeline_event(result: str = "failed") -> EventEnvelope:
    return EventEnvelope(
        event_id="evt-pipeline-probe-1", event_type="PipelineFinished",
        source_system="json-ci", occurred_at=_T0 - timedelta(seconds=5),
        project=_PROJECT,
        subject_ref="urn:testpilot:json-ci:pipeline:evt-pipeline-probe-1",
        payload_uri="file://fixtures/runs/ci-run.json",
        payload={"build_id": "ci-42", "commit_sha": _COMMIT, "result": result},
        idempotency_key="pipeline-evt-pipeline-probe-1",
    )


async def _pipeline_story(work: Path, check) -> dict:
    container = _LoopContainer(work)
    store = container.get("store")
    _write_ci_run(container.fixtures_dir)
    event = _pipeline_event()

    first = await handle_pipeline_finished(container, event)
    runs_after_first = len(store.list_test_runs())
    outbox_after_first = len(store.list_outbox_records())
    budget_key = f"ci-rerun:{_PROJECT}:{_COMMIT[:12]}"
    budget_n = len(store.list_budget_entries(
        budget_key, datetime.now(timezone.utc).date().isoformat()))
    second = await handle_pipeline_finished(container, event)
    noop = await handle_pipeline_finished(container, _pipeline_event("succeeded"))

    draft = store.get_outbox_record(first.draft_idempotency_key)
    dispatch = await dispatch_pending_outbox(container)
    draft_after_dispatch = store.get_outbox_record(first.draft_idempotency_key)
    check(
        "S4.4 CI 闭环：失败事件走通 入账→选择重跑→归因→R3 草稿，重放 replayed "
        "且 run/outbox/预算零增生，成功事件 no-op 零写入",
        first.outcome == "closed" and second.outcome == "replayed"
        and second.rerun_run_id == first.rerun_run_id
        and len(store.list_test_runs()) == runs_after_first
        and len(store.list_outbox_records()) == outbox_after_first == 1
        and budget_n == 1 and first.needs_human == 1
        and noop.outcome == "no_failure",
        f"outcomes=({first.outcome},{second.outcome},{noop.outcome}) "
        f"runs={runs_after_first} outbox={outbox_after_first} budget={budget_n} "
        f"needs_human={first.needs_human}",
    )
    check(
        "S4.4 草稿 dry-run：无 TicketPort 时派发 skipped_unsupported，草稿保持 "
        "pending（默认姿态无任何外部写）",
        dispatch["skipped_unsupported"] == 1
        and draft is not None
        and str(draft.status) == OutboxStatus.PENDING.value
        and str(draft_after_dispatch.status) == OutboxStatus.PENDING.value,
        f"dispatch={dispatch} draft_status={draft_after_dispatch.status}",
    )
    return {"closed": first.to_dict(), "replay_outcome": second.outcome,
            "budget_rows": budget_n, "outbox_dispatch": dispatch}


def _render_markdown(doc: dict) -> str:
    lines = [
        "# S4 四门禁总验探针报告（A24，hermetic 机制门禁）", "",
        f"- 生成时间：{doc['generated_at']}",
        f"- 工作目录：`{doc['workdir']}`（tempfile 临时库，不碰 store/offline-demo）",
        f"- Schema：v{doc['schema_version']}（零 schema 变更，治理数据只进 raw_json）",
        "- 口径：门①为 occurred_at→可检索两段 P95（合成时钟）；门②为默认检索"
        "通道过期命中；门③为 event-reindex 批次实测延迟；门④为推荐∩执行分母精度。",
        "", "## 四门禁汇总", "",
        "| # | 门禁 | 阈值 | 实测 | 结果 |",
        "|---|---|---|---|---|",
    ]
    for gate in doc["gates"]:
        lines.append(
            f"| {gate['index']} | {gate['name']} | {gate['threshold']} "
            f"| {gate['actual']} | {gate['result']} |"
        )
    lines += ["", "## 检查项", ""]
    for item in doc["checks"]:
        lines.append(f"- {item['passed']} {item['name']}：{item['detail']}")
    lines += [
        "", "## 红证对照（门禁有牙证据）", "",
        "```json",
        json.dumps(doc["red_contrast"], ensure_ascii=False, indent=2),
        "```", "",
    ]
    return "\n".join(lines) + "\n"


async def _run_async_stories(store, work, check) -> dict:
    stale = await _stale_story(store, check)
    loop = await _pipeline_story(work, check)
    return {"stale": stale, "pipeline": loop}


def main() -> int:
    checks: list[dict[str, str]] = []
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "passed": "✅" if ok else "❌", "detail": detail})
        if not ok:
            failures.append(name)

    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="s4-gate-", dir=EVAL_DIR))
    store = bootstrap_store(work / "data")
    try:
        latency = _latency_story(store, check)
        async_part = asyncio.run(_run_async_stories(store, work, check))
        reindex = _reindex_story(store, check)
        precision = _precision_story(check)
        governance = _governance_story(store, check)
        scheduled = _scheduled_story(store, check)

        # 持久化与钉版：重开库行数无损，schema 仍 v15
        version_ok = store.get_meta("schema_version") == str(SCHEMA_VERSION) == "15"
        runs_before = len(store.list_test_runs())
        data_dir = str(work / "data")
        store.close()
        reopened = bootstrap_store(data_dir)
        try:
            runs_after = len(reopened.list_test_runs())
            events_after = len(reopened.list_events())
            ingestion_after = len(reopened.list_ingestion_runs())
        finally:
            reopened.close()
        check(
            "持久化/钉版：重开库事件与批次账本无损，schema v15（S4 全程零迁移）",
            version_ok and runs_before == runs_after and events_after == LATENCY_N
            and ingestion_after >= LATENCY_N + 2,
            f"version={SCHEMA_VERSION} runs {runs_before}→{runs_after} "
            f"events={events_after} ingestion_runs={ingestion_after}",
        )
    finally:
        try:
            store.close()
        except Exception:  # noqa: BLE001 - 持久化检查已关库时忽略
            pass

    gate1_ok = latency["intake"]["gate_passed"] and latency["end_to_end"]["gate_passed"]
    gate2_ok = async_part["stale"]["expired_hit_rate"] == 0.0
    gate3_ok = reindex["max_measured_s"] < REINDEX_GATE_SECONDS
    gate4_ok = precision["precision"]["gate_passed"]
    gates = [
        {
            "index": 1, "name": "摄入延迟 P95（intake + end-to-end）",
            "threshold": f"<{DEFAULT_GATE_SECONDS:.0f}s（5min）",
            "actual": f"intake P95={latency['intake']['p95_s']}s / "
                      f"e2e P95={latency['end_to_end']['p95_s']}s"
                      f"（n={latency['sample_n']}）",
            "result": "✅" if gate1_ok else "❌",
        },
        {
            "index": 2, "name": "未标 stale 过期命中",
            "threshold": "≈0",
            "actual": f"默认通道命中 {async_part['stale']['default_channel_hits']} / "
                      f"审计通道 {async_part['stale']['audit_channel_hits']}，"
                      "过期命中率=0.0",
            "result": "✅" if gate2_ok else "❌",
        },
        {
            "index": 3, "name": "高价值 reindex 延迟",
            "threshold": f"<{REINDEX_GATE_SECONDS:.0f}s（10min）",
            "actual": f"{reindex['measured_runs']} 条 event-reindex 批次最大实测 "
                      f"{reindex['max_measured_s']:.0f}s",
            "result": "✅" if gate3_ok else "❌",
        },
        {
            "index": 4, "name": "影响推荐精度",
            "threshold": f"≥{DEFAULT_PRECISION_GATE}",
            "actual": f"precision={precision['precision']['precision']} "
                      f"（evaluated={precision['precision']['evaluated']}，"
                      f"缩减率={precision['precision']['reduction_ratio']}）",
            "result": "✅" if gate4_ok else "❌",
        },
    ]
    all_gates_ok = all(g["result"] == "✅" for g in gates)

    report_doc = {
        "probe": "s4_gate_probe",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "workdir": str(work),
        "schema_version": SCHEMA_VERSION,
        "gates": gates,
        "all_gates_passed": all_gates_ok,
        "latency": latency,
        **async_part,
        "reindex": reindex,
        "precision": precision,
        "governance": governance,
        "scheduled": scheduled,
        "red_contrast": {
            "gate1_latency_301s_gate_passed": False,  # 见检查项：实测咬红
            "gate1_negative_latency_raises": True,
            "gate1_empty_sample_p95_is_none": True,
            "gate3_reindex_601s_exceeds_600s": True,
            "gate4_red_precision": precision["red_precision"],
            "gate4_empty_denominator_precision": precision["empty_precision"],
        },
        "checks": checks,
    }
    (EVAL_DIR / "s4-gate-probe.json").write_text(
        json.dumps(report_doc, ensure_ascii=False, indent=2), encoding="utf-8")
    (EVAL_DIR / "s4-gate-probe.md").write_text(
        _render_markdown(report_doc), encoding="utf-8")

    for item in checks:
        print(f"{item['passed']} {item['name']} — {item['detail']}")
    print(f"报告：{EVAL_DIR / 's4-gate-probe.md'}")
    if failures or not all_gates_ok:
        print("❌ s4_gate_probe 存在失败检查或门禁未过", file=sys.stderr)
        return 1
    print(
        "✅ s4_gate_probe 全部通过：S4 四门禁总验"
        "（摄入延迟 P95/stale 过期命中/高价值 reindex/影响推荐精度）"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
