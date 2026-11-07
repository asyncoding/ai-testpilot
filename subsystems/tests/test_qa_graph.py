"""编译后的问答全图路由测试（InMemorySaver，fake 驱动，hostless）。

覆盖每条条件边与 FR-QA-001/002 的关键不变量：
- 持续伪造引用：生成节点精确调用 2 次后降级 unknown，第三次永不发生；
- 首败次成：重生成一次后 answered；
- 闲聊在 ACL 收口：检索/验证/生成调用数全部为 0；
- 非 code 意图：live_verify 调用数为 0；
- 五类负例统一不变量：不存在「无 citation 的 answered」；
- stale 索引走 verify→head_refresh→generation 边，最终 verified_at。
"""

from __future__ import annotations

import hashlib

import pytest

from testpilot_core.answering import QaParams
from testpilot_core.chunking import chunk_swift
from testpilot_core.enums import QaStatus, VersionState
from testpilot_core.models import RepoRef
from testpilot_orchestrator.qa_graph.graph import build_qa_graph, recursion_limit
from testpilot_orchestrator.qa_graph.nodes import QaDeps

from qa_fakes import (
    FakeModel,
    FakeScm,
    FakeSearch,
    make_chunk,
    structured_envelope,
)

HEAD = "7936295a700c8542034573e64f40bced3d5f80e5"
DOC_URN = "urn:testpilot:fs-docs:document:docs/runbook.md"
SYM_DOC = "urn:testpilot:fs-docs:document:Sources/Alarm.swift"
CHUNK1 = "urn:testpilot:fs-docs:chunk:docs/runbook.md#section-one"
SYM1 = "urn:testpilot:fs-docs:symbol:Sources/Alarm.swift#Alarm.ring"
FORGED_URN = "urn:testpilot:fs-docs:document:docs/never-retrieved.md"


def initial(**over) -> dict:
    state = {
        "run_id": "run-graph-1", "case_key": "t1",
        "question": "Alarm.ring 方法的实现逻辑是什么？",
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


async def drive(deps, state: dict | None = None) -> dict:
    graph = build_qa_graph(deps)
    cfg = {
        "configurable": {"thread_id": (state or initial())["run_id"]},
        "recursion_limit": recursion_limit(),
    }
    return await graph.ainvoke(state or initial(), config=cfg)


def code_deps(model, chunks, **kw):
    return QaDeps(model=model, search=FakeSearch(chunks, hybrid=True), **kw)


# ── 正向主路径 ───────────────────────────────────────────────────────────────

async def test_graph_code_happy_path_answered_and_verified_consistent():
    chunk = make_chunk(SYM1, doc_urn=SYM_DOC, kind="code_symbol")
    model = FakeModel({
        "qa_intent_t1": intent_env("code"),
        "qa_answer_t1": answer_env(SYM1, "证据原文"),
    })
    search = FakeSearch([chunk], hybrid=True)
    deps = QaDeps(model=model, search=search)

    out = await drive(deps)

    assert out["status"] == QaStatus.ANSWERED.value
    assert model.structured_keys() == ["qa_intent_t1", "qa_answer_t1"]
    assert search.verify_calls == [SYM1]  # 代码块 100% 校验
    assert search.search_calls[0]["kinds"] == ("code_symbol", "text")
    cite = out["final_answer"]["citations"][0]
    assert cite["version_state"] == VersionState.CONSISTENT.value
    assert out["attempts"] == 1


async def test_graph_persistent_forgery_exhausts_exactly_two_generations():
    chunk = make_chunk(SYM1, doc_urn=SYM_DOC, kind="code_symbol")
    forged = answer_env(FORGED_URN, "编造的引用原文")
    model = FakeModel({
        "qa_intent_t1": intent_env("code"),
        "qa_answer_t1": forged,           # 同一 envelope 每次调用都返回伪造稿
        "qa_answer_retry_t1": forged,
    })
    deps = QaDeps(model=model, search=FakeSearch([chunk], hybrid=True))

    out = await drive(deps, initial(run_id="run-forged"))

    assert out["status"] == QaStatus.UNKNOWN.value
    assert out["unknown_reason"] == "citation_failed_exhausted"
    # total=False 的 state：assembly 从未执行，键不存在即证明未产出答案
    assert out.get("final_answer") is None
    assert model.count("qa_answer_t1") == 1
    assert model.count("qa_answer_retry_t1") == 1
    assert model.count() == 3  # intent + 2 次生成，第三次生成永不发生
    assert out["attempts"] == 2
    assert all("urn_not_in_evidence" in e for e in out["citation_errors"])


async def test_graph_first_forgery_then_retry_passes_is_answered():
    chunk = make_chunk(SYM1, doc_urn=SYM_DOC, kind="code_symbol")
    model = FakeModel({
        "qa_intent_t1": intent_env("code"),
        "qa_answer_t1": [answer_env(FORGED_URN, "编造")],  # list：仅首次弹出
        "qa_answer_retry_t1": answer_env(SYM1, "证据原文"),
    })
    deps = QaDeps(model=model, search=FakeSearch([chunk], hybrid=True))

    out = await drive(deps, initial(run_id="run-retry-ok"))

    assert out["status"] == QaStatus.ANSWERED.value
    assert out["attempts"] == 2
    assert model.structured_keys() == [
        "qa_intent_t1", "qa_answer_t1", "qa_answer_retry_t1"]
    assert out["final_answer"]["citations"][0]["urn"] == SYM1


# ── 收口/绕边断言 ────────────────────────────────────────────────────────────

async def test_graph_chitchat_stops_at_acl_with_zero_downstream_calls():
    chunk = make_chunk(CHUNK1)
    model = FakeModel({"qa_intent_t1": intent_env("out_of_scope")})
    search = FakeSearch([chunk], hybrid=True)
    deps = QaDeps(model=model, search=search)

    out = await drive(deps, initial(run_id="run-chitchat", question="你好"))

    assert out["status"] == QaStatus.UNKNOWN.value
    assert out["unknown_reason"] == "out_of_scope"
    assert search.search_calls == []
    assert search.verify_calls == []
    assert model.count("qa_answer_t1") == 0
    assert model.count("qa_answer_retry_t1") == 0


async def test_graph_howto_skips_verify_node_entirely():
    chunk = make_chunk(CHUNK1, text="操作步骤原文")
    model = FakeModel({
        "qa_intent_t1": intent_env("howto"),
        "qa_answer_t1": answer_env(CHUNK1, "操作步骤原文"),
    })
    search = FakeSearch([chunk], hybrid=True)
    deps = QaDeps(model=model, search=search)

    out = await drive(deps, initial(run_id="run-howto"))

    assert out["status"] == QaStatus.ANSWERED.value
    assert search.verify_calls == []  # 非 code 意图零 live-verify
    assert out["final_answer"]["citations"][0]["version_state"] == \
        VersionState.CONSISTENT.value


async def test_graph_empty_retrieval_unknown_without_any_generation():
    model = FakeModel({"qa_intent_t1": intent_env("code")})
    search = FakeSearch([], hybrid=True)
    deps = QaDeps(model=model, search=search)

    out = await drive(deps, initial(run_id="run-empty"))

    assert out["status"] == QaStatus.UNKNOWN.value
    assert out["unknown_reason"] == "empty_retrieval"
    assert model.count("qa_answer_t1") == 0
    assert search.verify_calls == []


async def test_graph_model_unavailable_degrades_to_unknown():
    chunk = make_chunk(CHUNK1)
    model = FakeModel({"qa_intent_t1": intent_env("howto")})  # 无 answer key
    deps = QaDeps(model=model, search=FakeSearch([chunk], hybrid=True))

    out = await drive(deps, initial(run_id="run-no-model"))

    assert out["status"] == QaStatus.UNKNOWN.value
    assert out["unknown_reason"] == "model_unavailable"
    assert any(f.startswith("model_unavailable:")
               for f in out["degraded_flags"])


# ── 五类负例的统一不变量：answered 必有 citation ─────────────────────────────

async def _negative_final_state(name: str) -> dict:
    chunk = make_chunk(SYM1, doc_urn=SYM_DOC, kind="code_symbol")
    text_chunk = make_chunk(CHUNK1)
    if name == "chitchat":
        deps = code_deps(FakeModel({"qa_intent_t1": intent_env("out_of_scope")}),
                         [chunk])
        return await drive(deps, initial(run_id=f"neg-{name}", question="你好"))
    if name == "deny":
        deps = code_deps(
            FakeModel({"qa_intent_t1": intent_env("code")}), [chunk],
            params=QaParams(permitted_scopes=("team-a",)))
        return await drive(deps, initial(run_id=f"neg-{name}",
                                         acl_scope="team-b"))
    if name == "empty":
        deps = code_deps(
            FakeModel({"qa_intent_t1": intent_env("code")}), [])
        return await drive(deps, initial(run_id=f"neg-{name}"))
    if name == "forged":
        forged = answer_env(FORGED_URN, "编造")
        deps = code_deps(FakeModel({
            "qa_intent_t1": intent_env("code"),
            "qa_answer_t1": forged, "qa_answer_retry_t1": forged,
        }), [chunk])
        return await drive(deps, initial(run_id=f"neg-{name}"))
    if name == "model_unavailable":
        deps = code_deps(
            FakeModel({"qa_intent_t1": intent_env("howto")}), [text_chunk])
        return await drive(deps, initial(run_id=f"neg-{name}"))
    raise AssertionError(f"未知负例: {name}")


@pytest.mark.parametrize("name", [
    "chitchat", "deny", "empty", "forged", "model_unavailable"])
async def test_graph_negative_cases_never_answer_without_citations(name):
    out = await _negative_final_state(name)
    citations = (out.get("final_answer") or {}).get("citations") or []
    assert not (out.get("status") == QaStatus.ANSWERED.value and not citations)


# ── verify→head_refresh→generation 整边（stale 索引刷新）────────────────────

SWIFT_V2 = """\
struct Alarm {
    func ring() {
        print("ring v2 body")
    }
}
"""


async def test_graph_stale_index_refreshed_at_head_and_marked_verified_at():
    rechunked = chunk_swift(SWIFT_V2)
    target = next(c for c in rechunked if c.kind == "code_symbol")
    urn = f"urn:testpilot:fs-docs:symbol:Sources/Alarm.swift#{target.anchor}"
    chunk = make_chunk(urn, doc_urn=SYM_DOC, kind="code_symbol",
                       text="旧索引文本", content_hash="stale-hash")
    scm = FakeScm({("Sources/Alarm.swift", HEAD): SWIFT_V2})
    search = FakeSearch(
        [chunk], hybrid=True,
        verify_results={urn: {"body_changed": True, "verified": False,
                              "exists": True, "degraded": False,
                              "head_commit": HEAD}})
    model = FakeModel({
        "qa_intent_t1": intent_env("code"),
        "qa_answer_t1": answer_env(urn, target.text),
    })
    deps = QaDeps(model=model, search=search, scm=scm,
                  repo=RepoRef(project="demo", repo_path="/tmp/fake-repo"))

    out = await drive(deps, initial(run_id="run-stale"))

    assert out["status"] == QaStatus.ANSWERED.value
    assert out["head_refreshed_urns"] == [urn]
    refreshed = next(e for e in out["evidence"] if e["urn"] == urn)
    assert refreshed["content_hash"] == hashlib.sha256(
        target.text.encode("utf-8")).hexdigest()
    cite = out["final_answer"]["citations"][0]
    assert cite["version_state"] == VersionState.VERIFIED_AT.value
    assert cite["version_ref"] == HEAD[:7]
    assert scm.reads == [("Sources/Alarm.swift", HEAD)]
