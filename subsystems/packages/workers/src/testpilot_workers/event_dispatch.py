"""事件消费骨架（docs/08 FR-CON-005 / FR-FRS-001 AC2）。

replay JSON 事件目录 → 逐信封解析 + 契约校验 → 幂等落账（events 表 event_id
去重）→ 按 event_type 路由 handler → 状态流转 received→processed/dead。
解析失败、契约不符、handler 抛错一律进 dead_letters，单条坏事件不阻断整批
（FR-CON-005 AC2：缺字段进死信并报错，不静默忽略）。

S4.1 只装骨架：路由表与 no-op handler 就位（no-op 仍完成落账+状态流转+留痕）。
CommitPushed 的 stale/reindex 在 S4.2 经 :func:`register_handler` 接入，
PipelineFinished 的 CI 闭环在 S4.4 接入，本模块不改。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from pydantic import ValidationError

from testpilot_core.event_contract import (
    DEAD_REASON_HANDLER_ERROR,
    DEAD_REASON_PARSE_ERROR,
    EventContractError,
    validate_event,
)
from testpilot_core.models import EventEnvelope

EventHandler = Callable[[object, EventEnvelope], Awaitable[None]]
_DEAD_SOURCE = "event-dispatch"


async def _noop_handler(container, event: EventEnvelope) -> None:
    """默认 handler：事件已落账可检索，业务消费由后续切片挂接。"""


# 路由表：进程级注册，测试与后续切片用 register_handler 挂接/替换。
_HANDLERS: dict[str, EventHandler] = {}


def register_handler(event_type: str, handler: EventHandler) -> None:
    """挂接事件类型的业务 handler（S4.2/S4.4 用），重复注册显式报错防静默覆盖。"""
    existing = _HANDLERS.get(event_type)
    if existing is not None and existing is not handler:
        raise ValueError(f"事件类型 {event_type} 已注册 handler: {existing!r}")
    _HANDLERS[event_type] = handler


def reset_handlers() -> None:
    """测试隔离用：清空路由表回到全 no-op 骨架态。"""
    _HANDLERS.clear()


@dataclass
class EventDispatchReport:
    scanned: int = 0
    new: int = 0
    replayed: int = 0
    processed: int = 0
    dead: int = 0
    by_type: Counter = field(default_factory=Counter)
    dead_reasons: Counter = field(default_factory=Counter)

    def to_dict(self) -> dict:
        return {
            "scanned": self.scanned,
            "new": self.new,
            "replayed": self.replayed,
            "processed": self.processed,
            "dead": self.dead,
            "by_type": dict(self.by_type),
            "dead_reasons": dict(self.dead_reasons),
        }


def _dead(store, reason: str, payload: dict, error: str, report: EventDispatchReport) -> None:
    store.add_dead_letter(f"{_DEAD_SOURCE}:{reason}", payload, error)
    report.dead += 1
    report.dead_reasons[reason] += 1


async def dispatch_events(container, *, source: str = "", event_source=None) -> EventDispatchReport:
    """消费一个 replay 事件源；event_source 缺省取容器装配的 ``ci_events``。

    幂等语义：events 表 event_id 主键去重，重复投递不重跑 handler、不增生行，
    计入 replayed（FR-FRS-001 AC2）。坏事件只进死信与自身状态，不影响同批其他事件。
    """
    event_source = event_source or container.get("ci_events")
    if event_source is None:
        raise ValueError("容器未装配 ci_events 且未显式传入 event_source，无事件可消费")
    store = container.get("store")
    tracer = container.get("tracer")
    report = EventDispatchReport()

    with tracer.span("events.dispatch", metadata={"source": source}):
        raw_events = await event_source.replay_raw(source)
        for path, data in raw_events:
            report.scanned += 1
            # 1) 解析关：JSON 是 dict 但信封结构不合法（缺字段/坏时间戳）
            try:
                event = EventEnvelope.model_validate(data)
            except ValidationError as exc:
                _dead(store, DEAD_REASON_PARSE_ERROR,
                      {"file": str(path), "payload": data}, str(exc).splitlines()[0], report)
                continue

            # 2) 契约关：类型闭集 + 必填非空。先落账（拿到 event_id 行）再标 dead，
            #    重复 event_id 仍走幂等短路，不重复记死信。
            try:
                validate_event(event)
            except EventContractError as exc:
                is_new = store.record_event_if_new(event)
                if not is_new:
                    report.replayed += 1
                    continue
                store.mark_event_status(event.event_id, "dead")
                _dead(store, exc.reason,
                      {"file": str(path), "event_id": event.event_id,
                       "event_type": event.event_type}, exc.detail, report)
                continue

            # 3) 幂等关：重复投递直接短路，handler 绝不重跑
            is_new = store.record_event_if_new(event)
            if not is_new:
                report.replayed += 1
                report.by_type[event.event_type] += 1
                continue
            report.new += 1
            report.by_type[event.event_type] += 1

            # 4) 路由消费：handler 异常转 dead 而非炸整批
            handler = _HANDLERS.get(event.event_type, _noop_handler)
            try:
                await handler(container, event)
            except Exception as exc:  # noqa: BLE001 - 单事件失败隔离是显式设计
                store.mark_event_status(event.event_id, "dead")
                _dead(store, DEAD_REASON_HANDLER_ERROR,
                      {"event_id": event.event_id, "event_type": event.event_type},
                      f"{type(exc).__name__}: {exc}", report)
                continue
            store.mark_event_status(event.event_id, "processed")
            report.processed += 1

    return report
