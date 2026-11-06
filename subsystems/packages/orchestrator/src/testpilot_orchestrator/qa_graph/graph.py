"""LangGraph 薄装配（全仓唯一 import langgraph 的业务文件）。

节点函数与框架无关（nodes.py），本文件只做注册与条件边接线。若 LangGraph 在
某环境不可用，备选手写状态机=一张「节点名→路由函数」循环，节点零改动。

预算：LangGraph 默认 recursion_limit=10007（wheel 源码取证），等于没有守卫；
这里从 Limits.max_agent_steps（默认 12）显式映射，最坏路径
intent→acl→retrieval→sufficiency→verify→head_refresh→generation→citation
→generation→citation→assembly 共 11 supersteps，12 留 1 步余量。
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, StateGraph

from testpilot_core.config_schema import Limits
from testpilot_core.enums import QaStatus

from .nodes import (
    END_MARK,
    N_ACL,
    N_ASSEMBLY,
    N_CITATION_CHECK,
    N_GENERATION,
    N_HEAD_REFRESH,
    N_INTENT,
    N_RETRIEVAL,
    N_SUFFICIENCY,
    N_VERIFY,
    QaDeps,
    make_nodes,
    route_after_acl,
    route_after_citation,
    route_after_generation,
    route_after_sufficiency,
    route_after_verify,
)
from .state import QaState


def build_qa_graph(deps: QaDeps, *, checkpointer: Any | None = None):
    """编译问答图。测试注 InMemorySaver；生产由 QaService 注 AsyncSqliteSaver。

    步数预算不在这里：节点侧 token 预算走 deps.limits，图侧 recursion_limit
    由调用方 ainvoke(config=...) 时经 recursion_limit() 映射传入，两处同一事实源。
    """
    nodes = make_nodes(deps)
    graph = StateGraph(QaState)
    for name, fn in nodes.items():
        graph.add_node(name, fn)

    graph.add_edge(START, N_INTENT)
    graph.add_edge(N_INTENT, N_ACL)
    graph.add_conditional_edges(
        N_ACL, route_after_acl,
        {N_RETRIEVAL: N_RETRIEVAL, END_MARK: END},
    )
    graph.add_edge(N_RETRIEVAL, N_SUFFICIENCY)
    graph.add_conditional_edges(
        N_SUFFICIENCY, route_after_sufficiency,
        {N_VERIFY: N_VERIFY, N_GENERATION: N_GENERATION, END_MARK: END},
    )
    graph.add_conditional_edges(
        N_VERIFY, route_after_verify,
        {N_HEAD_REFRESH: N_HEAD_REFRESH, N_GENERATION: N_GENERATION},
    )
    graph.add_edge(N_HEAD_REFRESH, N_GENERATION)
    graph.add_conditional_edges(
        N_GENERATION, route_after_generation,
        {N_CITATION_CHECK: N_CITATION_CHECK, END_MARK: END},
    )
    graph.add_conditional_edges(
        N_CITATION_CHECK, route_after_citation,
        {N_GENERATION: N_GENERATION, N_ASSEMBLY: N_ASSEMBLY, END_MARK: END},
    )
    graph.add_edge(N_ASSEMBLY, END)

    return graph.compile(checkpointer=checkpointer or InMemorySaver())


def sqlite_saver_ctx(db_path: Path) -> AbstractAsyncContextManager:
    """AsyncSqliteSaver 连接上下文（langgraph-checkpoint-sqlite 的全仓唯一入口）。

    必须在 running loop 内 enter（驱动取连接时即触 loop），因此同步 build_container
    不碰它，由 QaService.astart() 在异步生命周期里接管；db 落
    data_dir/checkpoints/qa.sqlite，*.sqlite* 已 gitignore，连接关闭后文件保留。
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return AsyncSqliteSaver.from_conn_string(str(db_path))


def recursion_limit(limits: Limits | None = None) -> int:
    """图执行预算的唯一映射点（service/探针/测试共用，杜绝魔数）。"""
    return (limits or Limits()).max_agent_steps


async def run_qa_graph(compiled, state: dict, *, limit: int) -> dict:
    """图执行的唯一收口：thread_id=run_id、显式步数预算、超步异常归一化。

    QaService 不直接 import langgraph（框架隔离纪律：业务代码只有本文件 import）。
    GraphRecursionError 不抛穿——归一为 status=error 的最小 state，由服务层照常
    落 qa_runs（可观测、可对账），请求侧拿到的仍是结构化 AskResponse 而非 500。
    """
    cfg = {
        "configurable": {"thread_id": state["run_id"]},
        "recursion_limit": limit,
    }
    try:
        return await compiled.ainvoke(state, config=cfg)
    except GraphRecursionError:
        return {
            "run_id": state["run_id"],
            "status": QaStatus.ERROR.value,
            "unknown_reason": "recursion_limit_exceeded",
            "degraded_flags": [f"recursion_limit_exceeded:{limit}"],
        }
