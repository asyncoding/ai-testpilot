"""图执行预算守卫（S1.4 C4）。

两道预算：
1. 步数预算：recursion_limit 从 Limits.max_agent_steps 唯一映射（默认 12），
   LangGraph 默认值 10007 等于没有守卫——这里钉死映射值与超限异常类型，
   QaService（C5/C6）将捕获同一 GraphRecursionError 落 status=error；
2. token 预算：generation 节点调模型前预检，超限直接 unknown/budget_exceeded，
   一个生成调用都不许发出（对照实验：同夹具放宽预算后必须能 answered）。
"""

from __future__ import annotations

from typing import TypedDict

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, StateGraph

from testpilot_core.config_schema import Limits
from testpilot_core.enums import QaStatus
from testpilot_orchestrator.qa_graph.graph import build_qa_graph, recursion_limit
from testpilot_orchestrator.qa_graph.nodes import QaDeps

from qa_fakes import FakeModel, FakeSearch, make_chunk, structured_envelope

CHUNK1 = "urn:testpilot:fs-docs:chunk:docs/runbook.md#section-one"


def initial(**over) -> dict:
    state = {
        "run_id": "run-budget-1", "case_key": "t1",
        "question": "如何配置备份间隔？",
        "project": "demo", "branch": "main", "acl_scope": "default",
    }
    state.update(over)
    return state


def intent_env(value: str) -> dict:
    return structured_envelope({"intent": value})


def answer_env(urn: str, anchor: str) -> dict:
    return structured_envelope({
        "summary": f"结论：{anchor}",
        "sections": [],
        "citations": [{"urn": urn, "anchor": anchor}],
    })


# ── 步数预算映射 ─────────────────────────────────────────────────────────────

def test_recursion_limit_is_mapped_from_agent_steps_not_framework_default():
    assert recursion_limit() == Limits().max_agent_steps == 12
    assert recursion_limit(Limits(max_agent_steps=7)) == 7


async def test_graph_raises_recursion_error_at_mapped_limit():
    """自循环小图钉死：映射值 12 是活的守卫，超限抛 QaService 将来要捕获的异常。"""

    class LoopState(TypedDict, total=False):
        n: int

    g = StateGraph(LoopState)

    async def bump(state: LoopState) -> dict:
        return {"n": state.get("n", 0) + 1}

    g.add_node(bump)
    g.add_edge(START, "bump")
    g.add_conditional_edges("bump", lambda s: "bump" if s["n"] < 100 else END)
    app = g.compile(checkpointer=InMemorySaver())

    with pytest.raises(GraphRecursionError):
        await app.ainvoke(
            {"n": 0},
            config={"configurable": {"thread_id": "run-loop"},
                    "recursion_limit": recursion_limit()},
        )


async def test_qa_graph_longest_real_path_runs_under_the_limit():
    """对照：真实问答图最坏路径（stale 刷新 + 一次重生成）在 12 步内收敛，不被误杀。"""
    chunk = make_chunk(CHUNK1, text="操作步骤原文")
    forged_urn = "urn:testpilot:fs-docs:document:docs/never-retrieved.md"
    model = FakeModel({
        "qa_intent_t1": intent_env("howto"),
        "qa_answer_t1": [answer_env(forged_urn, "编造")],
        "qa_answer_retry_t1": answer_env(CHUNK1, "操作步骤原文"),
    })
    deps = QaDeps(model=model, search=FakeSearch([chunk], hybrid=True))
    graph = build_qa_graph(deps)

    out = await graph.ainvoke(
        initial(run_id="run-longest"),
        config={"configurable": {"thread_id": "run-longest"},
                "recursion_limit": recursion_limit()},
    )

    assert out["status"] == QaStatus.ANSWERED.value
    assert out["attempts"] == 2


# ── token 预算预检 ───────────────────────────────────────────────────────────

async def test_token_budget_precheck_terminates_without_calling_generation():
    chunk = make_chunk(CHUNK1, text="操作步骤原文")
    # intent envelope 默认 tokens_in=100；预算 50 → 进 generation 前即超限
    model = FakeModel({
        "qa_intent_t1": intent_env("howto"),
        "qa_answer_t1": answer_env(CHUNK1, "操作步骤原文"),
    })
    tight = Limits(max_tokens_per_run=50)
    deps = QaDeps(
        model=model, search=FakeSearch([chunk], hybrid=True), limits=tight,
    )
    graph = build_qa_graph(deps)

    out = await graph.ainvoke(
        initial(run_id="run-token-cap"),
        config={"configurable": {"thread_id": "run-token-cap"},
                "recursion_limit": recursion_limit(tight)},
    )

    assert out["status"] == QaStatus.UNKNOWN.value
    assert out["unknown_reason"] == "budget_exceeded"
    assert model.count("qa_answer_t1") == 0  # 预检拦截，生成调用零发出
    assert out.get("final_answer") is None


async def test_same_fixture_with_roomy_budget_is_answered_control():
    """对照组：仅放宽 token 预算，其余夹具完全一致，必须正常 answered。"""
    chunk = make_chunk(CHUNK1, text="操作步骤原文")
    model = FakeModel({
        "qa_intent_t1": intent_env("howto"),
        "qa_answer_t1": answer_env(CHUNK1, "操作步骤原文"),
    })
    deps = QaDeps(model=model, search=FakeSearch([chunk], hybrid=True))
    graph = build_qa_graph(deps)

    out = await graph.ainvoke(
        initial(run_id="run-token-ok"),
        config={"configurable": {"thread_id": "run-token-ok"},
                "recursion_limit": recursion_limit()},
    )

    assert out["status"] == QaStatus.ANSWERED.value
    assert model.count("qa_answer_t1") == 1
