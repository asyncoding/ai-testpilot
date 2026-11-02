"""CI/事件 JSON 重放适配器（EventSourcePort）。

生产替代：Kafka/webhook（docs/04 §4.3 #14）。S0/S4 用可重放 JSON 事件演示
事件驱动摄入；重复 event_id 由存储层幂等去重。
"""

from __future__ import annotations

import json
from pathlib import Path

from testpilot_core.models import EventEnvelope


class JsonReplayEventSource:
    def __init__(self, events_dir: str | Path) -> None:
        self.events_dir = Path(events_dir)

    async def replay(self, source: str) -> list[EventEnvelope]:
        if not self.events_dir.exists():
            return []
        events: list[EventEnvelope] = []
        for path in sorted(self.events_dir.glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            event = EventEnvelope.model_validate(data)
            if not source or event.source_system == source:
                events.append(event)
        return events
