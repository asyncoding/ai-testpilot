"""进程级事件 handler 默认挂接表。

CLI 入口（ingest-worker --replay-events）在派发前调一次；测试用
event_dispatch.reset_handlers / register_handler 自行隔离。S4.4 的
PipelineFinished CI 闭环 handler 也在此表追加，本模块保持薄。
"""

from __future__ import annotations

from testpilot_core.event_contract import EVENT_TYPES

from .event_dispatch import register_handler
from .freshness import handle_commit_pushed
from .pipeline_loop import handle_pipeline_finished


def wire_default_handlers() -> None:
    """把业务 handler 挂到事件闭集对应类型；未挂接类型仍走 no-op 骨架。"""
    # 防御：挂接类型必须在 docs/04 §8 闭集内，闭集调整时这里响亮失败
    assert "CommitPushed" in EVENT_TYPES
    register_handler("CommitPushed", handle_commit_pushed)
    assert "PipelineFinished" in EVENT_TYPES
    register_handler("PipelineFinished", handle_pipeline_finished)
