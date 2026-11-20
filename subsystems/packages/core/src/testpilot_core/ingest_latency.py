"""摄入延迟度量纯逻辑（docs/08 FR-FRS-001 AC1 / docs/01 §10 门禁①，S4.4）。

零 IO、零三方依赖（import-lint 守门）：吃 ``(occurred_at, searchable_at)``
样本对算 P50/P95/max 与门禁结论，不接触挂钟——时间全部由入参带入，合成时钟
即可确定性测试。

两段口径（docs/06：摄入延迟＝事件发生到可检索的时间）：

- **intake（摄入段）**：事件 occurred_at → 事件行 received_at（事件落库即可经
  list_events 检索），样本由 events 表直接可得；
- **end-to-end（端到端段）**：CommitPushed occurred_at → event-reindex 批次
  finished_at（变更切块重新可检索），样本由 events 与 ingestion_runs 按
  report_json.event_id 配对得到，是 FR-FRS-001 AC1「push 后 P95<5min 对应
  变更可检索」的头条口径。

无基线不造数：空样本的分位数全部为 None、gate_passed=False（与
impact.regression_selection_precision 空分母同纪律），绝不展示 0%/0s 假成绩。
"""

from __future__ import annotations

from datetime import datetime
from typing import Sequence

from pydantic import BaseModel, Field

# docs/01 §10 S4 门禁①：摄入延迟 P95 < 5 分钟
DEFAULT_GATE_SECONDS = 300.0
# event-reindex 批次的 trigger 字面值（workers/freshness.py 落账口径）
EVENT_REINDEX_TRIGGER = "event-reindex"


class EventTiming(BaseModel):
    """events 表一行的时间投影（workers 层负责 ISO 字符串→datetime）。"""

    event_id: str
    occurred_at: datetime
    received_at: datetime


class IngestionRunTiming(BaseModel):
    """ingestion_runs 表一行的时间投影；event_id 取自 report_json，可空。"""

    run_id: str
    trigger: str
    finished_at: datetime
    event_id: str = ""


class LatencySample(BaseModel):
    """一条「事件发生 → 可检索」延迟样本；event_id 保证样本可回溯。"""

    event_id: str
    occurred_at: datetime
    searchable_at: datetime

    @property
    def latency_seconds(self) -> float:
        delta = (self.searchable_at - self.occurred_at).total_seconds()
        if delta < 0:
            # 时钟回拨/源时钟歪斜是数据问题：响亮失败，禁止 abs() 掩盖成假低延迟
            raise ValueError(
                f"样本 {self.event_id} 出现负延迟 "
                f"({self.occurred_at.isoformat()} → {self.searchable_at.isoformat()})，"
                "先修时钟/数据再算 P95"
            )
        return delta


class IngestionLatencyReport(BaseModel):
    sample_count: int
    p50_s: float | None = None
    p95_s: float | None = None
    max_s: float | None = None
    gate_threshold_s: float = DEFAULT_GATE_SECONDS
    gate_passed: bool = False


def percentile(values: Sequence[float], pct: float) -> float:
    """线性索引百分位（与 evaluation._percentile 同口径，确定性无随机）。

    索引 ``round((n-1)*pct)``：单元素恒为该元素，p=0/p=1 取最小/最大。
    调用方保证非空；空序列在本模块统一走 ``build_latency_report`` 的 None 分支。
    """
    if not values:
        raise ValueError("空序列无法计算百分位")
    if not 0.0 <= pct <= 1.0:
        raise ValueError(f"百分位取值必须在 [0,1]：{pct}")
    ordered = sorted(values)
    return ordered[round((len(ordered) - 1) * pct)]


def build_latency_report(
    samples: Sequence[LatencySample],
    *,
    gate_threshold_s: float = DEFAULT_GATE_SECONDS,
) -> IngestionLatencyReport:
    """聚合延迟样本并按门限判定；空样本→全 None + gate_passed=False。"""
    if not samples:
        return IngestionLatencyReport(
            sample_count=0, gate_threshold_s=gate_threshold_s
        )
    values = [s.latency_seconds for s in samples]
    p95 = percentile(values, 0.95)
    return IngestionLatencyReport(
        sample_count=len(values),
        p50_s=percentile(values, 0.50),
        p95_s=p95,
        max_s=max(values),
        gate_threshold_s=gate_threshold_s,
        gate_passed=p95 < gate_threshold_s,
    )


def intake_samples(events: Sequence[EventTiming]) -> list[LatencySample]:
    """摄入段样本：事件 occurred_at → received_at（落库即可检索）。"""
    return [
        LatencySample(
            event_id=e.event_id,
            occurred_at=e.occurred_at,
            searchable_at=e.received_at,
        )
        for e in events
    ]


def pair_reindex_latency(
    events: Sequence[EventTiming], runs: Sequence[IngestionRunTiming]
) -> list[LatencySample]:
    """端到端样本：event-reindex 批次按 report_json.event_id 回配源事件。

    只认 trigger='event-reindex' 且 finished_at/event_id 齐备的批次；
    event_id 在 events 中找不到对应行时跳过（数据未齐，不臆造 occurred_at）。
    一个事件只产生一条样本（同 event_id 多批次取首个，稳定按 run_id 排序）。
    """
    by_event = {e.event_id: e for e in events}
    samples: list[LatencySample] = []
    seen: set[str] = set()
    for run in sorted(runs, key=lambda r: r.run_id):
        if run.trigger != EVENT_REINDEX_TRIGGER or not run.event_id:
            continue
        event = by_event.get(run.event_id)
        if event is None or run.event_id in seen:
            continue
        seen.add(run.event_id)
        samples.append(
            LatencySample(
                event_id=run.event_id,
                occurred_at=event.occurred_at,
                searchable_at=run.finished_at,
            )
        )
    return samples
