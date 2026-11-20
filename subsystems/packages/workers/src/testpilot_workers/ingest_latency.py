"""摄入延迟 P95 收集服务（docs/08 FR-FRS-001 AC1，S4.4）。

workers 层只做读库与 ISO 时间解析，样本配对/分位数/门禁判定全部在
:mod:`testpilot_core.ingest_latency`（零 IO，合成时钟可测）。

两段报告：

- ``intake``：events 表 occurred_at→received_at，事件入库即可检索的摄入段；
- ``end_to_end``：event-reindex 批次按 report_json.event_id 回配源事件，
  push→变更重新可检索的端到端段（docs/01 §10 门禁①的头条口径）。

任一段无样本都如实给 None + gate_passed=False，不造 0 秒假成绩。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from testpilot_core.ingest_latency import (
    DEFAULT_GATE_SECONDS,
    EventTiming,
    IngestionRunTiming,
    IngestionLatencyReport,
    build_latency_report,
    intake_samples,
    pair_reindex_latency,
)


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def collect_latency_reports(
    store, *, gate_threshold_s: float = DEFAULT_GATE_SECONDS
) -> dict[str, IngestionLatencyReport]:
    """读 events/ingestion_runs 组装摄入段与端到端段两份 P95 报告。"""
    events = [
        EventTiming(
            event_id=row["event_id"],
            occurred_at=_parse(row["occurred_at"]),
            received_at=_parse(row["received_at"]),
        )
        for row in store.list_events()
    ]
    runs: list[IngestionRunTiming] = []
    for row in store.list_ingestion_runs():
        finished_at = row.get("finished_at")
        if not finished_at:
            continue  # 还在跑的批次不进延迟样本
        report = row.get("report") or {}
        runs.append(
            IngestionRunTiming(
                run_id=row["run_id"],
                trigger=row["trigger"],
                finished_at=_parse(finished_at),
                event_id=report.get("event_id", ""),
            )
        )

    return {
        "intake": build_latency_report(
            intake_samples(events), gate_threshold_s=gate_threshold_s
        ),
        "end_to_end": build_latency_report(
            pair_reindex_latency(events, runs), gate_threshold_s=gate_threshold_s
        ),
    }


def reports_to_dict(reports: dict[str, IngestionLatencyReport]) -> dict[str, Any]:
    return {name: report.model_dump() for name, report in reports.items()}
