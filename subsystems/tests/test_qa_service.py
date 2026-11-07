"""QaService 测试（S1.4 C5）：真 sqlite 落库 + 反馈守卫 + 超步归一。

与图测试的分工：图测试用 InMemorySaver 只看路由；本文件验证服务层的
qa_runs 一行落库、JSON 列往返、反馈孤儿守卫、UNIQUE 双写拦截、
GraphRecursionError 归一为 status=error 且仍留痕。
"""

from __future__ import annotations

import pytest
from langgraph.errors import GraphRecursionError

from qa_fakes import FakeHybridSearch, FakeModel, make_chunk, structured_envelope
from testpilot_adapters.store import bootstrap_store
from testpilot_core.answering import qa_run_urn
from testpilot_core.enums import (
    AuthorityLevel,
    FeedbackKind,
    FeedbackReason,
    QaIntent,
    QaStatus,
    VersionState,
)
from testpilot_core.errors import NotFoundError
from testpilot_core.models import AskRequest, KnowledgeChunk, SourceDocument
from testpilot_orchestrator.qa_graph.graph import recursion_limit
from testpilot_orchestrator.qa_graph.nodes import QaDeps
from testpilot_orchestrator.qa_graph.service import QaService

DOC1 = "urn:testpilot:fs-docs:document:docs/runbook.md"
DOC2 = "urn:testpilot:fs-docs:document:docs/other.md"
CHUNK1 = "urn:testpilot:fs-docs:chunk:docs/runbook.md#section-one"
CHUNK2 = "urn:testpilot:fs-docs:chunk:docs/other.md#section-one"
SYM1 = "urn:testpilot:fs-docs:symbol:Sources/Alarm.swift#Alarm.ring"
SYM_DOC = "urn:testpilot:fs-docs:document:Sources/Alarm.swift"


def _answer_env(urn: str, anchor: str) -> dict:
    return structured_envelope({
        "summary": f"结论：{anchor}",
        "sections": [],
        "citations": [{"urn": urn, "anchor": anchor}],
    })


def _service(tmp_path, model, search, **kw) -> QaService:
    store = bootstrap_store(tmp_path / "data")
    deps = QaDeps(model=model, search=search, store=store, **kw)
    return QaService(deps, store)


def _request(question: str = "Alarm.ring 方法的实现逻辑是什么？") -> AskRequest:
    return AskRequest(question=question, project="demo", branch="main")


async def test_ask_persists_full_qa_run_and_roundtrips_json(tmp_path):
    chunk = make_chunk(SYM1, doc_urn=SYM_DOC, kind="code_symbol")
    model = FakeModel({
        "qa_intent_t1": structured_envelope({"intent": "code"}),
        "qa_answer_t1": _answer_env(SYM1, "证据原文"),
    })
    service = _service(tmp_path, model, FakeHybridSearch([chunk]))

    resp = await service.ask(_request(), case_key="t1", run_id="run-fixed-1")

    assert resp.status == QaStatus.ANSWERED
    assert resp.intent == QaIntent.CODE
    assert resp.intent_source == "model"
    row = await service.get_run("run-fixed-1")
    assert row is not None and row["status"] == "answered"
    assert row["question"].startswith("Alarm.ring")
    assert row["intent_source"] == "model"
    assert row["thread_id"] == "run-fixed-1"
    assert row["attempts"] == 1 and row["regens"] == 0
    assert row["tokens_in"] == 200 and row["tokens_out"] == 60  # intent+answer 两次信封累加
    assert row["prompt_version"] == "qa-prompt-3"
    assert row["params_version"] == "qa-params-1"
    assert row["data_version"]  # verify 节点写入的 HEAD sha
    assert len(row["verify_results"]) == 1
    assert row["evidence"][0]["urn"] == SYM1
    assert row["citations"][0]["version_state"] == VersionState.CONSISTENT.value
    assert row["answer_payload"]["markdown"]
    assert row["retrieval_event_id"] == 11


async def test_symbol_question_bypasses_intent_model_end_to_end(tmp_path):
    # P4 端到端：符号形态题面只给 answer 录制键——若意图节点误调模型，缺 key 立即报错
    chunk = make_chunk(SYM1, doc_urn=SYM_DOC, kind="code_symbol")
    model = FakeModel({"qa_answer_t1": _answer_env(SYM1, "证据原文")})
    service = _service(tmp_path, model, FakeHybridSearch([chunk]))

    resp = await service.ask(
        _request("Alarm.ring() 方法的实现逻辑是什么？"),
        case_key="t1", run_id="run-bypass-1",
    )

    assert resp.status == QaStatus.ANSWERED
    assert resp.intent_source == "regex_bypass"
    # 只有 answer 一次信封的 token：意图调用整次省掉
    row = await service.get_run("run-bypass-1")
    assert row["intent_source"] == "regex_bypass"
    assert row["tokens_in"] == 100 and row["tokens_out"] == 30
    trace = await service.get_run_trace("run-bypass-1")
    assert trace["intent_source"] == "regex_bypass"


async def test_unknown_run_is_persisted_with_reason(tmp_path):
    model = FakeModel({"qa_intent_t1": structured_envelope({"intent": "code"})})
    service = _service(tmp_path, model, FakeHybridSearch([]))

    resp = await service.ask(_request(), case_key="t1", run_id="run-empty-1")

    assert resp.status == QaStatus.UNKNOWN
    assert resp.unknown_reason == "empty_retrieval"
    row = await service.get_run("run-empty-1")
    assert row["status"] == "unknown"
    assert row["unknown_reason"] == "empty_retrieval"
    assert row["answer_payload"] == {} and row["citations"] == []


async def test_duplicate_run_id_raises_integrity_error(tmp_path):
    chunk = make_chunk(CHUNK1, doc_urn=DOC1, text="证据原文")
    model = FakeModel({
        "qa_intent_t1": structured_envelope({"intent": "howto"}),
        "qa_answer_t1": _answer_env(CHUNK1, "证据原文"),
    })
    service = _service(tmp_path, model, FakeHybridSearch([chunk]))

    await service.ask(_request("如何配置备份？"), case_key="t1", run_id="run-dup")
    with pytest.raises(Exception) as exc:  # sqlite3.IntegrityError
        await service.ask(_request("如何配置备份？"), case_key="t1", run_id="run-dup")
    assert "UNIQUE" in str(exc.value) or "Integrity" in type(exc.value).__name__


async def test_feedback_requires_existing_run_and_writes_convention_urn(tmp_path):
    chunk = make_chunk(CHUNK1, doc_urn=DOC1, text="证据原文")
    model = FakeModel({
        "qa_intent_t1": structured_envelope({"intent": "howto"}),
        "qa_answer_t1": _answer_env(CHUNK1, "证据原文"),
    })
    service = _service(tmp_path, model, FakeHybridSearch([chunk]))
    await service.ask(_request("如何配置备份？"), case_key="t1", run_id="run-fb-1")

    with pytest.raises(NotFoundError):
        await service.record_feedback("run-ghost", FeedbackKind.NOT_USEFUL)

    row_id = await service.record_feedback(
        "run-fb-1", FeedbackKind.NOT_USEFUL,
        reason=FeedbackReason.STALE_REFERENCE, detail="引用过期",
        model_version="recorded-local",
    )
    assert row_id > 0
    row = service._store.conn.execute(
        "SELECT * FROM feedback WHERE id=?", (row_id,)
    ).fetchone()
    assert row["target_urn"] == qa_run_urn("run-fb-1")
    assert row["kind"] == "not_useful" and row["reason"] == "stale_reference"
    assert row["detail"] == "引用过期" and row["model_version"] == "recorded-local"
    # 被拒的孤儿反馈没有留下任何行
    ghost_rows = service._store.conn.execute(
        "SELECT COUNT(*) AS n FROM feedback WHERE run_id=?", ("run-ghost",)
    ).fetchone()
    assert ghost_rows["n"] == 0


async def test_feedback_summary_groups_by_kind_and_reason(tmp_path):
    chunk = make_chunk(CHUNK1, doc_urn=DOC1, text="证据原文")
    model = FakeModel({
        "qa_intent_t1": structured_envelope({"intent": "howto"}),
        "qa_answer_t1": _answer_env(CHUNK1, "证据原文"),
    })
    service = _service(tmp_path, model, FakeHybridSearch([chunk]))
    for rid in ("run-s1", "run-s2", "run-s3"):
        await service.ask(_request("如何配置备份？"), case_key="t1", run_id=rid)
    await service.record_feedback("run-s1", FeedbackKind.NOT_USEFUL,
                                  reason=FeedbackReason.STALE_REFERENCE)
    await service.record_feedback("run-s2", FeedbackKind.NOT_USEFUL,
                                  reason=FeedbackReason.STALE_REFERENCE)
    await service.record_feedback("run-s3", FeedbackKind.USEFUL)

    summary = await service.feedback_summary()
    flat = {(r["kind"], r["reason"]): r["n"] for r in summary}
    assert flat[("not_useful", "stale_reference")] == 2
    assert flat[("useful", "")] == 1


class _ExplodingGraph:
    """模拟图超步：只暴露服务实际使用的 ainvoke 面。"""

    async def ainvoke(self, state, *, config):  # noqa: ANN001
        raise GraphRecursionError("recursion limit=12 reached")


async def test_recursion_error_is_normalized_and_persisted(tmp_path):
    store = bootstrap_store(tmp_path / "data")
    chunk = make_chunk(CHUNK1, doc_urn=DOC1, text="证据原文")
    deps = QaDeps(model=FakeModel({}), search=FakeHybridSearch([chunk]), store=store)
    service = QaService(deps, store, graph=_ExplodingGraph())

    resp = await service.ask(_request(), run_id="run-recursion-1")

    assert resp.status == QaStatus.ERROR
    assert resp.unknown_reason == "recursion_limit_exceeded"
    assert any("recursion_limit_exceeded" in f for f in resp.degraded)
    row = await service.get_run("run-recursion-1")
    assert row["status"] == "error"
    assert row["question"]  # 请求侧字段仍完整留痕
    assert row["degraded"][0].startswith("recursion_limit_exceeded:")
    assert row["thread_id"] == "run-recursion-1"
    await service.aclose()  # C5 无资源收口，调通即可（C6 接 sqlite saver）


async def test_service_uses_mapped_step_limit(tmp_path):
    store = bootstrap_store(tmp_path / "data")
    deps = QaDeps(model=FakeModel({}), search=FakeHybridSearch([]), store=store)
    service = QaService(deps, store)
    assert service._step_limit == recursion_limit() == 12


# ── S1.5：原文代理白名单与 trace 时间线 ──────────────────────────────────────

def _doc(urn: str, *, authority=AuthorityLevel.L2_EXPERIENCE) -> SourceDocument:
    return SourceDocument(
        urn=urn, source_system="fs-docs",
        uri=f"file:///repo/{urn.split(':', 4)[-1]}",
        title=urn.rsplit(":", 1)[-1], version="h1", content_hash=f"doc-hash-{urn[-4:]}",
        authority_level=authority, acl_scope="default",
    )


def _howto_service(store, chunks):
    # 复用调用方预灌过文档的同一 store 连接，不另起 bootstrap
    model = FakeModel({
        "qa_intent_t1": structured_envelope({"intent": "howto"}),
        "qa_answer_t1": _answer_env(CHUNK1, "证据原文"),
    })
    deps = QaDeps(model=model, search=FakeHybridSearch(chunks), store=store)
    return QaService(deps, store)


async def test_get_run_source_chunk_and_document_envelopes(tmp_path):
    store = bootstrap_store(tmp_path / "data")
    store.upsert_document(_doc(DOC1))
    store.upsert_chunk(KnowledgeChunk(
        urn=CHUNK1, doc_urn=DOC1, index=0, kind="text",
        text="证据原文", branch="main", content_hash="ch-hash-1",
    ))
    # 对照文档：同库真实存在但不在本 run 白名单内，必须同样 404
    store.upsert_document(_doc(DOC2))
    store.upsert_chunk(KnowledgeChunk(
        urn=CHUNK2, doc_urn=DOC2, index=0, kind="text",
        text="另一篇的原文", branch="main", content_hash="ch-hash-2",
    ))
    chunk = make_chunk(CHUNK1, doc_urn=DOC1, text="证据原文",
                       content_hash="ch-hash-1")
    service = _howto_service(store, [chunk])
    await service.ask(_request("如何配置备份？"), case_key="t1", run_id="run-src-1")

    # chunk 级：字段直接来自 run 证据，整句等值防子串假绿
    src = await service.get_run_source("run-src-1", CHUNK1)
    assert src["urn"] == CHUNK1 and src["kind"] == "text"
    assert src["text"] == "证据原文" and src["content_hash"] == "ch-hash-1"
    assert src["doc_urn"] == DOC1 and src["branch"] == "main"

    # 文档级：白名单含 doc_urn，经 store 聚合当前索引切块
    doc_src = await service.get_run_source("run-src-1", DOC1)
    assert doc_src["kind"] == "document" and doc_src["doc_urn"] == DOC1
    assert doc_src["text"] == "证据原文"
    assert doc_src["content_hash"] == f"doc-hash-{DOC1[-4:]}"
    assert doc_src["authority_level"] == AuthorityLevel.L2_EXPERIENCE.value

    # 白名单外的他库文档（证明不是「所有 urn 都 404」的假绿）
    with pytest.raises(NotFoundError, match="白名单"):
        await service.get_run_source("run-src-1", DOC2)
    # 路径穿越形态：urn 不接收文件路径，天然不可达，同样 404
    with pytest.raises(NotFoundError):
        await service.get_run_source("run-src-1", "../../etc/passwd")
    with pytest.raises(NotFoundError):
        await service.get_run_source("run-ghost", CHUNK1)


async def test_get_run_trace_assembles_timeline_without_inventing_links(tmp_path):
    store = bootstrap_store(tmp_path / "data")
    chunk = make_chunk(CHUNK1, doc_urn=DOC1, text="证据原文")
    service = _howto_service(store, [chunk])
    await service.ask(_request("如何配置备份？"), case_key="t1", run_id="run-tr-1")

    trace = await service.get_run_trace("run-tr-1")
    assert trace["run_id"] == "run-tr-1"
    assert trace["status"] == "answered" and trace["intent"] == "howto"
    assert trace["intent_source"] == "model"
    assert trace["attempts"] == 1 and trace["regens"] == 0
    assert trace["tokens"] == {"in": 200, "out": 60}
    assert trace["evidence"][0]["urn"] == CHUNK1
    # RRF 小分值不再单独面向 UI：证据投影改显排名+命中通道（分数保留供调试）
    assert set(trace["evidence"][0]) == {
        "urn", "doc_urn", "kind", "rank", "channels", "score", "authority_level"}
    assert trace["evidence"][0]["rank"] == 1
    # fake 不写 retrieval_events 表（event_id=11 只挂在 qa_runs 上），查不到事件→通道如实为空
    assert trace["evidence"][0]["channels"] == []
    assert trace["citation_errors"] == []
    assert trace["versions"]["model"] == "fake-model-1"
    assert trace["retrieval_event_id"] == 11  # FakeHybridSearch 默认 event_id
    # 服务层不读 profile 配置：Langfuse 深链由 router 注入，这里绝不能自造字段
    assert "langfuse" not in trace
    with pytest.raises(NotFoundError):
        await service.get_run_trace("run-ghost")


async def test_trace_evidence_projects_rank_and_channels_from_retrieval_event(tmp_path):
    """trace 通道反查：retrieval_events 里 fts/vector 名单投影成每条证据的 channels，
    rank 按 run 证据顺序 1-based；事件缺失（FTS 裸路径）时 channels 为空而非报错。"""
    store = bootstrap_store(tmp_path / "data")
    chunk1 = make_chunk(CHUNK1, doc_urn=DOC1, text="证据原文一")
    chunk2 = make_chunk(CHUNK2, doc_urn=DOC2, text="证据原文二")
    # 真实写一行 retrieval_events：CHUNK1 双路命中、CHUNK2 仅 FTS；
    # raw_json 带 P3 符号置顶决策（模拟适配器 symbol_name 命中后的留痕）
    event_id = store.record_retrieval_event(
        query="备份", query_sha="sha-1", project="demo", branch="main",
        acl_scope="default", kinds=["code_symbol", "text"],
        model_version="fake-model-1", fts_available=True, vector_available=True,
        degraded=False, fts_urns=[CHUNK1, CHUNK2], vector_urns=[CHUNK1],
        fused_urns=[CHUNK1, CHUNK2], fts_latency_ms=10, vector_latency_ms=20,
        extra={"symbol_pinned": True, "symbol_pin_urn": CHUNK1,
               "symbol_pin_name": "Alarm.ring()"})
    model = FakeModel({
        "qa_intent_t1": structured_envelope({"intent": "howto"}),
        "qa_answer_t1": _answer_env(CHUNK1, "证据原文一"),
    })
    service = _service(tmp_path, model,
                       FakeHybridSearch([chunk1, chunk2], event_id=event_id))
    await service.ask(_request("如何配置备份？"), case_key="t1", run_id="run-ch-1")

    trace = await service.get_run_trace("run-ch-1")
    by_urn = {e["urn"]: e for e in trace["evidence"]}
    assert by_urn[CHUNK1]["rank"] == 1 and by_urn[CHUNK1]["channels"] == ["fts", "vector"]
    assert by_urn[CHUNK2]["rank"] == 2 and by_urn[CHUNK2]["channels"] == ["fts"]
    # get_retrieval_event 主键反查契约（trace 之外的排查入口）
    event = store.get_retrieval_event(event_id)
    assert event is not None and event["fts_urns"] == [CHUNK1, CHUNK2]
    assert store.get_retrieval_event(999_999) is None
    # P3：置顶决策从 raw_json 投影到 trace（无 qa_runs schema 变更）
    assert trace["symbol_pinned"] is True
    assert trace["symbol_pin_urn"] == CHUNK1
    assert trace["symbol_pin_name"] == "Alarm.ring()"


async def test_citation_errors_roundtrip_from_graph_to_qa_run(tmp_path):
    """端到端：citation_check 的具体错误进 graph state → qa_runs.citation_errors →
    AskResponse 与 trace 三处口径一致（事故 run 只有 unknown_reason、看不出错因的修复）。"""
    chunk = make_chunk(CHUNK1, doc_urn=DOC1, text="证据原文")
    # 模型两次都给白名单外 URN：attempts 耗尽 → citation_failed_exhausted
    forged = structured_envelope({
        "summary": "结论", "sections": [],
        "citations": [{"urn": "urn:testpilot:fs-docs:chunk:docs/ghost.md#x",
                       "anchor": "证据原文"}]})
    model = FakeModel({
        "qa_intent_t1": structured_envelope({"intent": "howto"}),
        # 首稿与重生成键不同（节点 _model_key 约定），两次都给同一伪造 URN
        "qa_answer_t1": forged,
        "qa_answer_retry_t1": forged,
    })
    service = _service(tmp_path, model, FakeHybridSearch([chunk]))

    resp = await service.ask(_request("如何配置备份？"), case_key="t1", run_id="run-err-1")
    assert resp.status == QaStatus.UNKNOWN
    assert resp.unknown_reason == "citation_failed_exhausted"
    assert resp.citation_errors and all(
        "urn_not_in_evidence" in e for e in resp.citation_errors)

    row = await service.get_run("run-err-1")
    trace = await service.get_run_trace("run-err-1")
    assert row["citation_errors"] == resp.citation_errors == trace["citation_errors"]


async def test_rewritten_anchor_on_valid_urn_exhausts_with_anchor_not_found(tmp_path):
    """线上事故同构回归（run 3e64fb59）：URN 在白名单内，但模型把证据原文改写成
    证据里不存在的句子。P2 前靠 quote 逐字比对拦截；P2 后 anchor 渲染 fail-closed，
    必须落 anchor_not_found 而非静默出带污染引用的答案。"""
    chunk = make_chunk(CHUNK1, doc_urn=DOC1, text="证据原文")
    rewritten = structured_envelope({
        "summary": "结论", "sections": [],
        "citations": [{"urn": CHUNK1,
                       "anchor": "模型改写后证据里根本不存在的句子"}]})
    model = FakeModel({
        "qa_intent_t1": structured_envelope({"intent": "howto"}),
        "qa_answer_t1": rewritten,
        "qa_answer_retry_t1": rewritten,
    })
    service = _service(tmp_path, model, FakeHybridSearch([chunk]))

    resp = await service.ask(_request("如何配置备份？"), case_key="t1",
                             run_id="run-anchor-miss")
    assert resp.status == QaStatus.UNKNOWN
    assert resp.unknown_reason == "citation_failed_exhausted"
    assert resp.citation_errors and all(
        "anchor_not_found" in e for e in resp.citation_errors)

    row = await service.get_run("run-anchor-miss")
    trace = await service.get_run_trace("run-anchor-miss")
    assert row["citation_errors"] == resp.citation_errors == trace["citation_errors"]


async def test_list_runs_filters_by_status_and_retains_limit(tmp_path):
    store = bootstrap_store(tmp_path / "data")
    answered = _howto_service(
        store, [make_chunk(CHUNK1, doc_urn=DOC1, text="证据原文")])
    await answered.ask(_request("如何配置备份？"), case_key="t1", run_id="run-ok")

    # 空检索服务复用同一 store：跑一条 unknown 做状态过滤对照
    empty_model = FakeModel({"qa_intent_t1": structured_envelope({"intent": "code"})})
    empty_svc = QaService(
        QaDeps(model=empty_model, search=FakeHybridSearch([]), store=store), store)
    await empty_svc.ask(_request(), case_key="t1", run_id="run-bad")

    all_runs = await answered.list_runs()
    assert {r["run_id"] for r in all_runs} == {"run-ok", "run-bad"}
    unknowns = await answered.list_runs(status="unknown")
    assert [r["run_id"] for r in unknowns] == ["run-bad"]
    limited = await answered.list_runs(limit=1)
    assert len(limited) == 1
