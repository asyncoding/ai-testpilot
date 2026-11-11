"""摄入延迟纯逻辑 hostless 测试（FR-FRS-001 AC1 / S4.4 门禁①）。

合成时钟：时间全部手工构造，无挂钟、无 IO。覆盖分位数口径、空样本纪律、
负延迟守卫、event-reindex 配对去重与悬空 event_id 跳过。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from testpilot_core.ingest_latency import (
    DEFAULT_GATE_SECONDS,
    EVENT_REINDEX_TRIGGER,
    EventTiming,
    IngestionRunTiming,
    LatencySample,
    build_latency_report,
    intake_samples,
    pair_reindex_latency,
    percentile,
)

_T0 = datetime(2026, 11, 11, 8, 0, 0, tzinfo=timezone.utc)


def _sample(event_id: str, seconds: float) -> LatencySample:
    return LatencySample(
        event_id=event_id, occurred_at=_T0, searchable_at=_T0 + timedelta(seconds=seconds)
    )


def _event(event_id: str, *, occurred_offset: float = 0.0, received_offset: float) -> EventTiming:
    return EventTiming(
        event_id=event_id,
        occurred_at=_T0 + timedelta(seconds=occurred_offset),
        received_at=_T0 + timedelta(seconds=received_offset),
    )


# ── 分位数基元 ────────────────────────────────────────────────────────────────


def test_percentile_known_indices():
    values = [10.0, 20.0, 30.0, 40.0, 50.0]
    assert percentile(values, 0.0) == 10.0
    assert percentile(values, 0.5) == 30.0
    assert percentile(values, 0.95) == 50.0  # round((5-1)*0.95)=4
    assert percentile([7.0], 0.95) == 7.0


def test_percentile_rejects_empty_and_out_of_range():
    with pytest.raises(ValueError):
        percentile([], 0.95)
    with pytest.raises(ValueError):
        percentile([1.0], 1.5)


# ── 报告聚合与门禁 ─────────────────────────────────────────────────────────────


def test_empty_samples_report_none_and_gate_not_passed():
    report = build_latency_report([])
    assert report.sample_count == 0
    assert report.p50_s is None and report.p95_s is None and report.max_s is None
    assert report.gate_passed is False  # 无基线不造数，不允许展示假通过
    assert report.gate_threshold_s == DEFAULT_GATE_SECONDS


def test_report_passes_under_five_minute_gate():
    samples = [_sample(f"e{i}", seconds=30 + i * 10) for i in range(10)]
    report = build_latency_report(samples)
    assert report.sample_count == 10
    assert report.p95_s == 120.0
    assert report.max_s == 120.0
    assert report.gate_passed is True


def test_report_goes_red_when_p95_crosses_threshold():
    # 9 条 60s + 1 条 299s：P95 落在 299s（有序末位），门限 300s 红/绿边界
    red = build_latency_report(
        [*(_sample(f"fast-{i}", 60) for i in range(9)), _sample("slow", 301.0)],
        gate_threshold_s=300.0,
    )
    assert red.p95_s == 301.0 and red.gate_passed is False
    green = build_latency_report(
        [*(_sample(f"fast-{i}", 60) for i in range(9)), _sample("slow", 299.0)],
        gate_threshold_s=300.0,
    )
    assert green.p95_s == 299.0 and green.gate_passed is True


def test_negative_latency_raises_loudly():
    bad = LatencySample(
        event_id="clock-skew",
        occurred_at=_T0 + timedelta(seconds=10),
        searchable_at=_T0,
    )
    with pytest.raises(ValueError, match="负延迟"):
        bad.latency_seconds
    with pytest.raises(ValueError):
        build_latency_report([bad])


# ── 摄入段样本 ─────────────────────────────────────────────────────────────────


def test_intake_samples_use_received_at_as_searchable():
    events = [_event("e1", received_offset=5), _event("e2", received_offset=25)]
    samples = intake_samples(events)
    assert [s.event_id for s in samples] == ["e1", "e2"]
    assert [s.latency_seconds for s in samples] == [5.0, 25.0]


# ── 端到端配对（event-reindex）─────────────────────────────────────────────────


def _run(run_id: str, *, event_id: str, finished_offset: float,
         trigger: str = EVENT_REINDEX_TRIGGER) -> IngestionRunTiming:
    return IngestionRunTiming(
        run_id=run_id, trigger=trigger,
        finished_at=_T0 + timedelta(seconds=finished_offset),
        event_id=event_id,
    )


def test_pair_reindex_latency_matches_events_and_skips_unknown():
    events = [_event("e1", occurred_offset=0, received_offset=2),
              _event("e2", occurred_offset=0, received_offset=3)]
    runs = [
        _run("r1", event_id="e1", finished_offset=42),
        _run("r2", event_id="ghost", finished_offset=99),  # 无对应事件：跳过不臆造
        IngestionRunTiming(run_id="r3", trigger="incremental",
                           finished_at=_T0 + timedelta(seconds=50),
                           event_id="e2"),  # 非 event-reindex：不参与
    ]
    samples = pair_reindex_latency(events, runs)
    assert len(samples) == 1
    assert samples[0].event_id == "e1"
    assert samples[0].latency_seconds == 42.0


def test_pair_reindex_dedupes_multiple_batches_per_event():
    events = [_event("e1", received_offset=1)]
    runs = [
        _run("r1", event_id="e1", finished_offset=10),
        _run("r2", event_id="e1", finished_offset=20),
    ]
    samples = pair_reindex_latency(events, runs)
    assert len(samples) == 1
    # 稳定取 run_id 序首个，保证可重复
    assert samples[0].searchable_at == _T0 + timedelta(seconds=10)


def test_pair_reindex_ignores_runs_without_event_link():
    events = [_event("e1", received_offset=1)]
    runs = [_run("r1", event_id="", finished_offset=10)]
    assert pair_reindex_latency(events, runs) == []


def test_end_to_end_report_drives_gate_with_synthetic_clock():
    events = [_event(f"e{i}", received_offset=1) for i in range(20)]
    # P95 索引 round((20-1)*0.95)=18：索引 18/19 为 240，P95 才落在 240
    runs = [
        _run(f"r{i:02d}", event_id=f"e{i}",
             finished_offset=60 if i < 18 else 240)
        for i in range(20)
    ]
    report = build_latency_report(pair_reindex_latency(events, runs))
    assert report.sample_count == 20
    assert report.gate_passed is True and report.p95_s == 240.0
