"""qa_graph 单节点测试（hostless fake 驱动，不启图、不连外部服务）。

反假绿手法：精确调用计数、key 序列断言、两形态检索归一化对照、
降级路径逐条断言 flag 字面量。
"""

from __future__ import annotations

import hashlib

import pytest

from testpilot_core.answering import QaParams, to_evidence_dict
from testpilot_core.chunking import chunk_swift
from testpilot_core.config_schema import Limits
from testpilot_core.enums import AuthorityLevel, QaIntent, QaStatus, VersionState
from testpilot_core.models import AnswerDraft, RepoRef, SourceDocument
from testpilot_orchestrator.qa_graph.nodes import (
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
)

from qa_fakes import (
    FakeModel,
    FakeScm,
    FakeSearch,
    FakeStore,
    make_chunk,
    structured_envelope,
)

HEAD = "7936295a700c8542034573e64f40bced3d5f80e5"
DOC_URN = "urn:testpilot:fs-docs:document:docs/runbook.md"
SYM_DOC = "urn:testpilot:fs-docs:document:Sources/Alarm.swift"
HIST_DOC = "urn:testpilot:fs-docs:document:docs/postmortem.md"
CHUNK1 = "urn:testpilot:fs-docs:chunk:docs/runbook.md#section-one"
SYM1 = "urn:testpilot:fs-docs:symbol:Sources/Alarm.swift#Alarm.ring"
SYM2 = "urn:testpilot:fs-docs:symbol:Sources/Alarm.swift#Alarm.stop"
HIST1 = "urn:testpilot:fs-docs:chunk:docs/postmortem.md#kill-backup"


def base_state(**over) -> dict:
    state = {
        "run_id": "r1", "case_key": "t1",
        "question": "Alarm.ring 方法的实现逻辑是什么？",
        "project": "demo", "branch": "main", "acl_scope": "default",
    }
    state.update(over)
    return state


def make_deps(model, search, **kw) -> QaDeps:
    return QaDeps(model=model, search=search, **kw)


def evidence(urn=CHUNK1, *, kind="text", text="证据原文", doc_urn=DOC_URN,
             authority="", uri="", extra_meta=None) -> dict:
    meta = {"doc_authority": authority, "doc_uri": uri, **(extra_meta or {})}
    chunk = make_chunk(urn, doc_urn=doc_urn, kind=kind, text=text, metadata=meta)
    return to_evidence_dict(chunk)


def draft_data(urn=CHUNK1, anchor="证据原文") -> dict:
    return {
        "summary": "结论：证据原文如此",
        "sections": [],
        "citations": [{"urn": urn, "anchor": anchor}],
    }


def nodes_of(model, search, **kw):
    return make_nodes(make_deps(model, search, **kw))


# ── 1) intent ────────────────────────────────────────────────────────────────

async def test_intent_uses_recorded_key_and_returns_enum():
    model = FakeModel({"qa_intent_t1": structured_envelope({"intent": "code"})})
    nodes = nodes_of(model, FakeSearch([]))

    out = await nodes[N_INTENT](base_state())

    assert out["intent"] == "code"
    assert out["intent_degraded"] is False
    assert out["intent_source"] == "model"
    assert out["model_version"] == "fake-model-1"
    assert out["tokens_total"] == {"in": 100, "out": 30}
    assert model.structured_keys() == ["qa_intent_t1"]


async def test_intent_symbol_form_bypasses_model_with_zero_calls():
    # P4：题面是精确符号形态 → 确定性判 code，整次意图 LLM 调用省掉
    model = FakeModel({})  # 即使没有任何录制 key 也必须成功，证明模型根本没被调用
    nodes = nodes_of(model, FakeSearch([]))

    out = await nodes[N_INTENT](base_state(
        question="AlertListViewModel.snoozeRingingMission(minutes:) 的实现逻辑是什么？"))

    assert out["intent"] == QaIntent.CODE.value
    assert out["intent_degraded"] is False
    assert out["intent_source"] == "regex_bypass"
    assert model.calls == []  # 反假绿：零模型调用，不只是返回值正确
    assert "tokens_total" not in out and "model_version" not in out
    assert "degraded_flags" not in out  # 确定性路由不是降级


async def test_intent_bypass_accepts_no_arg_and_compact_selector_forms():
    model = FakeModel({})
    nodes = nodes_of(model, FakeSearch([]))

    for question in ("C.keep() 怎么实现？", "MissionCoordinator.schedulePrimary(_:at:) 在哪调用？"):
        out = await nodes[N_INTENT](base_state(question=question))
        assert out["intent"] == QaIntent.CODE.value
        assert out["intent_source"] == "regex_bypass"
    assert model.calls == []


async def test_intent_natural_language_question_never_bypasses():
    # 无符号形态的中文题必须仍走模型：免模型只认真正则，不做关键词猜测
    model = FakeModel({"qa_intent_t1": structured_envelope({"intent": "concept"})})
    nodes = nodes_of(model, FakeSearch([]))

    out = await nodes[N_INTENT](base_state(question="停钟兜底机制是什么概念？"))

    assert out["intent"] == "concept"
    assert out["intent_source"] == "model"
    assert model.structured_keys() == ["qa_intent_t1"]


async def test_intent_missing_key_falls_back_to_keyword_classifier():
    model = FakeModel({})  # 缺 key → AdapterUnavailableError
    nodes = nodes_of(model, FakeSearch([]))

    out = await nodes[N_INTENT](base_state(question="你好，聊聊天"))

    assert out["intent"] == QaIntent.OUT_OF_SCOPE.value
    assert out["intent_degraded"] is True
    assert out["intent_source"] == "fallback"
    assert any(f.startswith("intent_degraded:") for f in out["degraded_flags"])


async def test_intent_invalid_enum_falls_back():
    model = FakeModel({"qa_intent_t1": structured_envelope({"intent": "hacker"})})
    nodes = nodes_of(model, FakeSearch([]))

    out = await nodes[N_INTENT](base_state())

    assert out["intent"] == QaIntent.CODE.value  # 拿不准保守归 code
    assert out["intent_degraded"] is True
    assert out["intent_source"] == "fallback"


async def test_intent_degraded_envelope_falls_back():
    model = FakeModel({
        "qa_intent_t1": structured_envelope(None, degraded=True),
    })
    nodes = nodes_of(model, FakeSearch([]))

    out = await nodes[N_INTENT](base_state())

    assert out["intent_degraded"] is True
    assert out["intent"] == QaIntent.CODE.value
    assert out["intent_source"] == "fallback"


# ── 2) acl ───────────────────────────────────────────────────────────────────

async def test_acl_out_of_scope_terminates_before_retrieval():
    nodes = nodes_of(FakeModel({}), FakeSearch([]))
    state = base_state(intent=QaIntent.OUT_OF_SCOPE.value)

    out = await nodes[N_ACL](state)

    assert out["status"] == QaStatus.UNKNOWN.value
    assert out["unknown_reason"] == "out_of_scope"
    assert out["denied_reason"]


async def test_acl_denied_when_scope_not_permitted():
    params = QaParams(permitted_scopes=("team-a",))
    nodes = nodes_of(FakeModel({}), FakeSearch([]), params=params)
    state = base_state(intent="code", acl_scope="team-b")

    out = await nodes[N_ACL](state)

    assert out["status"] == QaStatus.DENIED.value
    assert "team-b" in out["denied_reason"]


async def test_acl_allows_default_scope_without_restriction():
    nodes = nodes_of(FakeModel({}), FakeSearch([]))
    out = await nodes[N_ACL](base_state(intent="code"))
    assert out == {}


# ── 3) retrieval：两形态归一化 ───────────────────────────────────────────────

async def test_retrieval_hybrid_result_normalized_with_scores_and_trace():
    chunks = [make_chunk(CHUNK1), make_chunk(
        "urn:testpilot:fs-docs:chunk:docs/runbook.md#section-two", text="第二段证据")]
    search = FakeSearch(chunks, hybrid=True, event_id=11)
    nodes = nodes_of(FakeModel({}), search)

    out = await nodes[N_RETRIEVAL](base_state(intent="howto"))

    assert [e["urn"] for e in out["evidence"]] == [c.urn for c in chunks]
    assert out["evidence"][0]["score"] == pytest.approx(0.9)
    assert out["retrieval_event_id"] == 11
    assert out["retrieval_trace"]["fts_available"] is True
    assert out["retrieval_degraded"] is False
    assert search.search_calls[0]["limit"] == 8


async def test_retrieval_fts_bare_list_has_no_trace_or_score():
    chunks = [make_chunk(CHUNK1)]
    search = FakeSearch(chunks, hybrid=False)
    nodes = nodes_of(FakeModel({}), search)

    out = await nodes[N_RETRIEVAL](base_state(intent="howto"))

    assert out["retrieval_event_id"] is None
    assert out["retrieval_trace"] == {}
    assert out["retrieval_degraded"] is False
    assert out["evidence"][0]["score"] == 0.0


async def test_retrieval_code_intent_passes_kinds_on_capable_adapter():
    search = FakeSearch([make_chunk(CHUNK1)], hybrid=True)
    nodes = nodes_of(FakeModel({}), search)

    await nodes[N_RETRIEVAL](base_state(intent="code"))

    assert search.search_calls[0]["kinds"] == ("code_symbol", "text")


async def test_retrieval_fts_adapter_flags_kinds_unsupported():
    search = FakeSearch([make_chunk(CHUNK1)], hybrid=False)
    nodes = nodes_of(FakeModel({}), search)

    out = await nodes[N_RETRIEVAL](base_state(intent="code"))

    assert "kinds" not in search.search_calls[0]
    assert "kinds_filter_unsupported" in out["degraded_flags"]


async def test_retrieval_non_code_intent_does_not_request_kinds():
    search = FakeSearch([make_chunk(CHUNK1)], hybrid=True)
    nodes = nodes_of(FakeModel({}), search)

    await nodes[N_RETRIEVAL](base_state(intent="howto"))

    # 非 code 意图节点不传 kinds：适配器形参保持默认 None（对照 code 用例的二元组）
    assert search.search_calls[0]["kinds"] is None


async def test_retrieval_code_intent_passes_normalized_symbol_name():
    search = FakeSearch([make_chunk(CHUNK1)], hybrid=True)
    nodes = nodes_of(FakeModel({}), search)

    state = base_state(
        intent="code",
        question="AlertListViewModel.snoozeRingingMission(minutes:) 的实现逻辑是什么？")
    out = await nodes[N_RETRIEVAL](state)

    assert search.search_calls[0]["symbol_name"] == \
        "AlertListViewModel.snoozeRingingMission(minutes:)"
    # 未置顶（fake 检索不做实体路由）：trace 标记如实给 False/空，不造置顶事实
    assert out["retrieval_trace"]["symbol_pinned"] is False
    assert out["retrieval_trace"]["symbol_pin_urn"] == ""


async def test_retrieval_plain_and_non_code_questions_never_pin():
    # code 意图但题面无符号形态 → 不传 symbol_name（常规混合检索零变化）
    search = FakeSearch([make_chunk(CHUNK1)], hybrid=True)
    nodes = nodes_of(FakeModel({}), search)
    await nodes[N_RETRIEVAL](base_state(intent="code", question="闹钟为什么不响？"))
    assert search.search_calls[0]["symbol_name"] is None

    # 题面有符号形态但非 code 意图 → 同样不路由（意图门禁，不替别的意图做确定性置顶）
    search2 = FakeSearch([make_chunk(CHUNK1)], hybrid=True)
    nodes2 = nodes_of(FakeModel({}), search2)
    await nodes2[N_RETRIEVAL](base_state(
        intent="howto",
        question="怎么调用 Alarm.ring() 方法？"))
    assert search2.search_calls[0]["symbol_name"] is None


async def test_retrieval_fts_adapter_flags_symbol_pin_unsupported():
    search = FakeSearch([make_chunk(CHUNK1)], hybrid=False)
    nodes = nodes_of(FakeModel({}), search)

    out = await nodes[N_RETRIEVAL](base_state(
        intent="code", question="Alarm.ring() 的实现逻辑是什么？"))

    assert "symbol_pin_unsupported" in out["degraded_flags"]
    assert "symbol_name" not in search.search_calls[0]


async def test_retrieval_extracts_l2_history_testimony_verbatim():
    chunk = make_chunk(
        HIST1, doc_urn=HIST_DOC,
        text="早期版本 killBackupInterval = 90s，导致后台被杀",
        metadata={"doc_authority": AuthorityLevel.L2_EXPERIENCE.value,
                  "date": "2026-01-01"},
    )
    search = FakeSearch([chunk], hybrid=True)
    nodes = nodes_of(FakeModel({}), search)

    out = await nodes[N_RETRIEVAL](base_state(intent="incident"))

    assert len(out["history_testimony"]) == 1
    history = out["history_testimony"][0]
    assert history["text"] == "早期版本 killBackupInterval = 90s，导致后台被杀"
    assert history["doc_urn"] == HIST_DOC
    assert history["date"] == "2026-01-01"


# ── 4) sufficiency ───────────────────────────────────────────────────────────

async def test_sufficiency_empty_evidence_terminates_unknown():
    nodes = nodes_of(FakeModel({}), FakeSearch([]))
    out = await nodes[N_SUFFICIENCY](base_state(intent="code", evidence=[]))
    assert out["status"] == QaStatus.UNKNOWN.value
    assert out["unknown_reason"] == "empty_retrieval"


async def test_sufficiency_passes_with_evidence():
    nodes = nodes_of(FakeModel({}), FakeSearch([]))
    out = await nodes[N_SUFFICIENCY](
        base_state(intent="code", evidence=[evidence()]))
    assert out == {}


# ── 5) verify：调用数=代码证据块数 ───────────────────────────────────────────

async def test_verify_calls_live_verify_once_per_code_symbol_only():
    evs = [
        evidence(SYM1, kind="code_symbol", doc_urn=SYM_DOC),
        evidence(SYM2, kind="code_symbol", doc_urn=SYM_DOC),
        evidence(CHUNK1, kind="text"),
    ]
    search = FakeSearch([])
    nodes = nodes_of(FakeModel({}), search)

    out = await nodes[N_VERIFY](base_state(intent="code", evidence=evs))

    assert search.verify_calls == [SYM1, SYM2]  # text 块零调用
    assert len(out["verify_results"]) == 2
    assert out["data_version"] == HEAD
    expected_keys = {
        "urn", "kind", "exists", "verified", "body_changed", "degraded",
        "reason", "head_commit", "indexed_commit", "latency_ms", "branch",
    }
    assert set(out["verify_results"][0]) == expected_keys


async def test_verify_degraded_row_is_projected_faithfully():
    search = FakeSearch(
        [], verify_results={SYM1: {"degraded": True, "exists": None,
                                   "verified": False, "head_commit": ""}})
    nodes = nodes_of(FakeModel({}), search)

    out = await nodes[N_VERIFY](
        base_state(intent="code", evidence=[
            evidence(SYM1, kind="code_symbol", doc_urn=SYM_DOC)]))

    row = out["verify_results"][0]
    assert row["degraded"] is True
    assert row["exists"] is None  # None 不得被节点洗成 False
    assert out["data_version"] == ""


# ── 6) head_refresh ──────────────────────────────────────────────────────────

SWIFT_V2 = """\
struct Alarm {
    func ring() {
        print("ring v2 body")
    }
}
"""


async def test_head_refresh_rechunks_head_blob_and_replaces_evidence():
    rechunked = chunk_swift(SWIFT_V2)
    target = next(c for c in rechunked if c.kind == "code_symbol")
    urn = f"urn:testpilot:fs-docs:symbol:Sources/Alarm.swift#{target.anchor}"
    evs = [to_evidence_dict(make_chunk(
        urn, doc_urn=SYM_DOC, kind="code_symbol", text="旧索引文本"))]
    scm = FakeScm({("Sources/Alarm.swift", HEAD): SWIFT_V2})
    repo = RepoRef(project="demo", repo_path="/tmp/fake-repo")
    nodes = nodes_of(
        FakeModel({}), FakeSearch([]), scm=scm, repo=repo)

    out = await nodes[N_HEAD_REFRESH](base_state(
        evidence=evs, data_version=HEAD,
        verify_results=[{"urn": urn, "body_changed": True,
                         "head_commit": HEAD, "degraded": False}]))

    assert out["head_refreshed_urns"] == [urn]
    new_ev = next(e for e in out["evidence"] if e["urn"] == urn)
    assert new_ev["text"] == target.text
    assert new_ev["content_hash"] == hashlib.sha256(
        target.text.encode("utf-8")).hexdigest()
    assert new_ev["metadata"]["head_refreshed"] is True
    assert scm.reads == [("Sources/Alarm.swift", HEAD)]


async def test_head_refresh_no_stale_chunk_is_noop():
    scm = FakeScm({("Sources/Alarm.swift", HEAD): SWIFT_V2})
    nodes = nodes_of(FakeModel({}), FakeSearch([]), scm=scm,
                     repo=RepoRef(project="demo", repo_path="/tmp/fake-repo"))

    out = await nodes[N_HEAD_REFRESH](base_state(
        evidence=[evidence(SYM1, kind="code_symbol", doc_urn=SYM_DOC)],
        verify_results=[{"urn": SYM1, "body_changed": False}]))

    assert out == {"head_refreshed_urns": []}
    assert scm.reads == []


async def test_head_refresh_without_scm_flags_unavailable():
    nodes = nodes_of(FakeModel({}), FakeSearch([]))  # scm/repo 均缺

    out = await nodes[N_HEAD_REFRESH](base_state(
        evidence=[evidence(SYM1, kind="code_symbol", doc_urn=SYM_DOC)],
        verify_results=[{"urn": SYM1, "body_changed": True}]))

    assert out["head_refreshed_urns"] == []
    assert "head_refresh_unavailable" in out["degraded_flags"]


async def test_head_refresh_missing_blob_flags_failure_and_keeps_indexed_text():
    scm = FakeScm({})  # 任何读取都抛 PathMissingAtCommitError
    nodes = nodes_of(
        FakeModel({}), FakeSearch([]), scm=scm,
        repo=RepoRef(project="demo", repo_path="/tmp/fake-repo"))
    evs = [evidence(SYM1, kind="code_symbol", doc_urn=SYM_DOC,
                    text="索引原文不动")]

    out = await nodes[N_HEAD_REFRESH](base_state(
        evidence=evs, data_version=HEAD,
        verify_results=[{"urn": SYM1, "body_changed": True,
                         "head_commit": HEAD}]))

    assert out["head_refreshed_urns"] == []
    assert f"head_refresh_failed:{SYM1}" in out["degraded_flags"]
    kept = next(e for e in out["evidence"] if e["urn"] == SYM1)
    assert kept["text"] == "索引原文不动"


# ── 7) generation ────────────────────────────────────────────────────────────

async def test_generation_first_attempt_uses_answer_key_and_persists_draft():
    model = FakeModel({"qa_answer_t1": structured_envelope(draft_data())})
    nodes = nodes_of(model, FakeSearch([]))

    out = await nodes[N_GENERATION](base_state(evidence=[evidence()]))

    assert model.structured_keys() == ["qa_answer_t1"]
    assert out["attempts"] == 1
    assert out["draft"]["citations"][0]["urn"] == CHUNK1
    assert out["prompt_version"] == "qa-prompt-3"
    assert out["params_version"] == "qa-params-1"
    assert out["tokens_total"]["in"] == 100


async def test_generation_retry_uses_retry_key_and_injects_errors():
    model = FakeModel({
        "qa_answer_retry_t1": structured_envelope(draft_data()),
    })
    nodes = nodes_of(model, FakeSearch([]))
    state = base_state(
        evidence=[evidence()], attempts=1,
        citation_errors=["urn_not_in_evidence: urn:x"])

    out = await nodes[N_GENERATION](state)

    assert model.structured_keys() == ["qa_answer_retry_t1"]
    assert out["attempts"] == 2
    joined = "\n".join(m["content"] for m in model.messages[0])
    assert "urn_not_in_evidence: urn:x" in joined


async def test_generation_symbol_pin_slims_prompt_but_keeps_pinned_full_text():
    # P4：retrieval_trace 带置顶 URN 时，置顶块全文进 prompt，其余块只投影签名
    long_body = "func snoozeRingingMission(minutes: Int) {\n" + \
        "\n".join(f"    line {i}" for i in range(30)) + "\n}\n"
    other_body = "func unrelated() {\n" + "    // 无关实现细节 " * 10 + "}\n"
    pinned = evidence(SYM1, kind="code_symbol", doc_urn=SYM_DOC, text=long_body,
                      extra_meta={"signature": "func snoozeRingingMission(minutes: Int)"})
    other = evidence(SYM2, kind="code_symbol", doc_urn=SYM_DOC, text=other_body,
                     extra_meta={"signature": "func unrelated()"})
    model = FakeModel({"qa_answer_t1": structured_envelope(draft_data(urn=SYM1))})
    nodes = nodes_of(model, FakeSearch([]))
    state = base_state(
        evidence=[pinned, other],
        retrieval_trace={"symbol_pinned": True, "symbol_pin_urn": SYM1,
                         "symbol_pin_name": "AlertListViewModel.snoozeRingingMission(minutes:)"},
    )

    out = await nodes[N_GENERATION](state)

    assert out["attempts"] == 1
    joined = "\n".join(m["content"] for m in model.messages[0])
    assert long_body in joined  # 置顶块全文可见，anchor 素材不丢
    assert "func unrelated()" in joined and other_body not in joined  # 非置顶只剩签名
    assert "非置顶证据" in joined


async def test_generation_without_pin_keeps_full_text_prompt():
    # 护栏：无置顶（FTS 裸 list / 未命中 / 重名）时 prompt 与旧版全文形态一致
    other_body = "func unrelated() {\n    detail line\n}\n"
    other = evidence(SYM2, kind="code_symbol", doc_urn=SYM_DOC, text=other_body,
                     extra_meta={"signature": "func unrelated()"})
    model = FakeModel({"qa_answer_t1": structured_envelope(draft_data(urn=SYM2))})
    nodes = nodes_of(model, FakeSearch([]))

    await nodes[N_GENERATION](base_state(evidence=[other]))  # 无 retrieval_trace 键

    joined = "\n".join(m["content"] for m in model.messages[0])
    assert other_body in joined
    assert "非置顶证据" not in joined


async def test_generation_missing_key_degrades_unknown():
    model = FakeModel({})
    nodes = nodes_of(model, FakeSearch([]))

    out = await nodes[N_GENERATION](base_state(evidence=[evidence()]))

    assert out["status"] == QaStatus.UNKNOWN.value
    assert out["unknown_reason"] == "model_unavailable"
    assert any(f.startswith("model_unavailable:") for f in out["degraded_flags"])


async def test_generation_degraded_envelope_degrades_unknown():
    model = FakeModel({
        "qa_answer_t1": structured_envelope(None, degraded=True),
    })
    nodes = nodes_of(model, FakeSearch([]))

    out = await nodes[N_GENERATION](base_state(evidence=[evidence()]))

    assert out["status"] == QaStatus.UNKNOWN.value
    assert out["unknown_reason"] == "model_unavailable"
    assert "model_degraded_envelope" in out["degraded_flags"]


async def test_generation_invalid_draft_schema_degrades_unknown():
    model = FakeModel({
        "qa_answer_t1": structured_envelope({"summary": 123}),  # 缺 citations 且类型错
    })
    nodes = nodes_of(model, FakeSearch([]))

    out = await nodes[N_GENERATION](base_state(evidence=[evidence()]))

    assert out["status"] == QaStatus.UNKNOWN.value
    assert any(f.startswith("draft_schema_invalid:")
               for f in out["degraded_flags"])


async def test_generation_token_budget_precheck_skips_model_call():
    model = FakeModel({
        "qa_answer_t1": structured_envelope(draft_data()),
    })
    limits = Limits(max_tokens_per_run=50)
    nodes = nodes_of(model, FakeSearch([]), limits=limits)

    out = await nodes[N_GENERATION](base_state(
        evidence=[evidence()], tokens_total={"in": 60, "out": 0}))

    assert out["status"] == QaStatus.UNKNOWN.value
    assert out["unknown_reason"] == "budget_exceeded"
    assert model.count() == 0  # 预检拦截，一个模型调用都不许发出


# ── 8) citation_check ────────────────────────────────────────────────────────

async def test_citation_check_passes_valid_draft():
    nodes = nodes_of(FakeModel({}), FakeSearch([]))
    state = base_state(
        evidence=[evidence()],
        draft=AnswerDraft.model_validate(draft_data()).model_dump(),
        attempts=1)

    out = await nodes[N_CITATION_CHECK](state)

    assert out["citation_errors"] == []
    assert "status" not in out


async def test_citation_check_first_failure_routes_back_to_regeneration():
    forged = draft_data(urn="urn:testpilot:fs-docs:document:docs/nope.md")
    nodes = nodes_of(FakeModel({}), FakeSearch([]))
    state = base_state(
        evidence=[evidence()],
        draft=AnswerDraft.model_validate(forged).model_dump(), attempts=1)

    out = await nodes[N_CITATION_CHECK](state)

    assert out["citation_errors"]
    assert "status" not in out  # 还有一次重生成机会，终态不许提前写


async def test_citation_check_exhausted_budget_writes_terminal_unknown():
    forged = draft_data(urn="urn:testpilot:fs-docs:document:docs/nope.md")
    nodes = nodes_of(FakeModel({}), FakeSearch([]))
    state = base_state(
        evidence=[evidence()],
        draft=AnswerDraft.model_validate(forged).model_dump(), attempts=2)

    out = await nodes[N_CITATION_CHECK](state)

    assert out["status"] == QaStatus.UNKNOWN.value
    assert out["unknown_reason"] == "citation_failed_exhausted"


async def test_citation_check_zero_citations_terminates_at_exhaustion():
    no_cite = {"summary": "无引用结论", "sections": [], "citations": []}
    nodes = nodes_of(FakeModel({}), FakeSearch([]))
    state = base_state(
        evidence=[evidence()],
        draft=AnswerDraft.model_validate(no_cite).model_dump(), attempts=2)

    out = await nodes[N_CITATION_CHECK](state)

    assert any("no_citations" in e for e in out["citation_errors"])
    assert out["status"] == QaStatus.UNKNOWN.value


# ── 9) assembly ──────────────────────────────────────────────────────────────

async def test_assembly_consistent_code_answer_answered_with_tri_state():
    nodes = nodes_of(FakeModel({}), FakeSearch([]))
    state = base_state(
        intent="code",
        evidence=[evidence(SYM1, kind="code_symbol", doc_urn=SYM_DOC,
                           uri="file:///repo/Sources/Alarm.swift")],
        verify_results=[{"urn": SYM1, "verified": True, "body_changed": False,
                         "degraded": False, "exists": True}],
        draft=AnswerDraft.model_validate(draft_data(
            urn=SYM1, anchor="证据原文")).model_dump())

    out = await nodes[N_ASSEMBLY](state)

    assert out["status"] == QaStatus.ANSWERED.value
    cite = out["final_answer"]["citations"][0]
    assert cite["version_state"] == VersionState.CONSISTENT.value
    assert cite["uri"].endswith("Sources/Alarm.swift")
    assert cite["content_hash"]


async def test_assembly_backfills_source_uri_from_store():
    doc = SourceDocument(urn=DOC_URN, source_system="fs-docs",
                         uri="file:///repo/docs/runbook.md",
                         content_hash="doc-hash-1")
    nodes = nodes_of(FakeModel({}), FakeSearch([]), store=FakeStore({DOC_URN: doc}))
    state = base_state(
        intent="howto", evidence=[evidence()], verify_results=[],
        draft=AnswerDraft.model_validate(draft_data()).model_dump())

    out = await nodes[N_ASSEMBLY](state)

    assert out["final_answer"]["citations"][0]["uri"] == \
        "file:///repo/docs/runbook.md"


async def test_assembly_refreshed_citation_marked_verified_at_head():
    nodes = nodes_of(FakeModel({}), FakeSearch([]))
    state = base_state(
        intent="code",
        evidence=[evidence(SYM1, kind="code_symbol", doc_urn=SYM_DOC)],
        verify_results=[{"urn": SYM1, "verified": False, "body_changed": True,
                         "degraded": False, "exists": True,
                         "head_commit": HEAD}],
        head_refreshed_urns=[SYM1],
        draft=AnswerDraft.model_validate(draft_data(
            urn=SYM1, anchor="证据原文")).model_dump())

    out = await nodes[N_ASSEMBLY](state)

    cite = out["final_answer"]["citations"][0]
    assert cite["version_state"] == VersionState.VERIFIED_AT.value
    assert cite["version_ref"] == HEAD[:7]


async def test_assembly_incident_renders_verbatim_history_and_current_sections():
    history_text = "早期版本 killBackupInterval = 90s，导致后台被杀"
    history_ev = evidence(
        HIST1, doc_urn=HIST_DOC, text=history_text,
        authority=AuthorityLevel.L2_EXPERIENCE.value,
        extra_meta={"date": "2026-01-01"})
    current_ev = evidence(SYM1, kind="code_symbol", doc_urn=SYM_DOC)
    # 模型只能引用当前文档，历史文档 URN 不得进 draft（assemble_incident 会拒交叉）
    draft = AnswerDraft.model_validate(draft_data(urn=SYM1, anchor="证据原文"))
    nodes = nodes_of(FakeModel({}), FakeSearch([]))
    state = base_state(
        intent="incident", evidence=[history_ev, current_ev],
        history_testimony=[{"text": history_text, "doc_urn": HIST_DOC,
                            "source_uri": "", "date": "2026-01-01",
                            "version": ""}],
        verify_results=[], draft=draft.model_dump())

    out = await nodes[N_ASSEMBLY](state)

    md = out["final_answer"]["markdown"]
    assert "## 历史证词（逐字引用，未经模型改写）" in md
    assert "## 当前结论" in md
    assert history_text in md  # 整句逐字出现
    assert out["final_answer"]["history"][0]["text"] == history_text
    assert out["status"] == QaStatus.ANSWERED.value
