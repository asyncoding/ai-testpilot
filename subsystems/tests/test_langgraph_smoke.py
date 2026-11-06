"""LangGraph 依赖门冒烟（S1.4 C3，docs/07 §11 指定框架的 Python 3.14 实证）。

固化 2026-11-05 在 Python 3.14.6 + langgraph 1.2.20 上的实测契约，后续升级版本
若导入路径/异常类型/序列化行为变化，本文件第一时间变红：
1. 导入面：StateGraph/START/END、InMemorySaver（在 langgraph.checkpoint.memory）、
   AsyncSqliteSaver（langgraph.checkpoint.sqlite.aio）、GraphRecursionError；
2. ainvoke 接受 configurable.thread_id 与 recursion_limit（默认值 10007，必须显式传）；
3. 超限抛 GraphRecursionError（QaService 据此兜底落 status=error 的 qa_runs）；
4. AsyncSqliteSaver 经 from_conn_string 上下文管理，db 文件落盘且连接可正常关闭；
5. 序列化现状：datetime/pydantic 在 1.2.20 可往返但 pydantic 带 unregistered type 警告、
   未来版本将 block——因此问答图 state 坚持全 JSON 原语，不依赖框架宽容度，
   硬自检在 test_qa_answering_core.py 的 json.dumps 往返用例。
"""

import json
import os
from datetime import datetime, timezone
from typing import TypedDict

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel


class CounterState(TypedDict, total=False):
    n: int


def _build_counter(stop: int = 5):
    g = StateGraph(CounterState)

    async def bump(state: CounterState) -> dict:
        return {"n": state.get("n", 0) + 1}

    g.add_node(bump)
    g.add_edge(START, "bump")
    g.add_conditional_edges("bump", lambda s: "bump" if s["n"] < stop else END)
    return g.compile(checkpointer=InMemorySaver())


async def test_import_surface_and_ainvoke_checkpoint():
    app = _build_counter()
    cfg = {"configurable": {"thread_id": "run-1"}, "recursion_limit": 12}
    out = await app.ainvoke({"n": 0}, config=cfg)
    assert out["n"] == 5
    state = await app.aget_state(cfg)  # checkpoint 按 thread_id 可读回
    assert state.values["n"] == 5


async def test_recursion_limit_raises_identifiable_error():
    app = _build_counter(stop=100)
    with pytest.raises(GraphRecursionError):
        await app.ainvoke(
            {"n": 0},
            config={"configurable": {"thread_id": "run-x"}, "recursion_limit": 4},
        )


async def test_async_sqlite_saver_lifecycle_and_persistence(tmp_path):
    db = tmp_path / "checkpoints" / "qa.sqlite"
    db.parent.mkdir(parents=True)
    async with AsyncSqliteSaver.from_conn_string(str(db)) as saver:
        # 同进程内重新构图并绑 sqlite saver（InMemorySaver 版本只验证内存路径）
        g = StateGraph(CounterState)

        async def bump(state):
            return {"n": state.get("n", 0) + 1}

        g.add_node(bump)
        g.add_edge(START, "bump")
        g.add_conditional_edges("bump", lambda s: "bump" if s["n"] < 5 else END)
        compiled = g.compile(checkpointer=saver)
        await compiled.ainvoke(
            {"n": 0}, config={"configurable": {"thread_id": "t1"}, "recursion_limit": 12}
        )
        state = await compiled.aget_state({"configurable": {"thread_id": "t1"}})
        assert state.values["n"] == 5
    assert db.exists()  # async with 退出后连接关闭、文件保留


class _M(BaseModel):
    x: int


class _LooseState(TypedDict, total=False):
    when: object
    model: object


async def test_non_primitive_roundtrip_behavior_is_documented(tmp_path):
    """取证性测试：固定 1.2.20 的宽容行为，提醒不要依赖它。

    问答图 state 的硬约束仍是全原语（to_evidence_dict 白名单 + json.dumps 自检）；
    若未来版本此处开始抛异常，说明框架收紧，我们的原语化纪律零改动即合规。
    """
    db = tmp_path / "loose.sqlite"
    async with AsyncSqliteSaver.from_conn_string(str(db)) as saver:
        g = StateGraph(_LooseState)

        async def putboth(state):
            return {
                "when": datetime(2026, 11, 5, 8, 0, tzinfo=timezone.utc),
                "model": _M(x=42),
            }

        g.add_node(putboth)
        g.add_edge(START, "putboth")
        await g.compile(checkpointer=saver).ainvoke(
            {}, config={"configurable": {"thread_id": "loose"}}
        )

    # 对照：问答图实际流通的证据 dict 必须是严格 JSON 原语（无类实例、无 datetime）
    evidence = {
        "urn": "urn:testpilot:fs-docs:chunk:d#s", "doc_urn": "urn:testpilot:fs-docs:document:d",
        "kind": "code_symbol", "text": "let x = 1", "content_hash": "h",
        "authority_level": "L1_authoritative", "branch": "main", "valid_from": None,
        "stale": "fresh", "score": 0.9, "source_uri": "file:///d.swift",
        "path": "d.swift", "anchor": "s", "doc_content_hash": "dh", "metadata": {},
    }
    assert json.loads(json.dumps(evidence, ensure_ascii=False)) == evidence
    assert os.path.exists(db)
