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
    FeedbackKind,
    FeedbackReason,
    QaIntent,
    QaStatus,
    VersionState,
)
from testpilot_core.errors import NotFoundError
from testpilot_core.models import AskRequest
from testpilot_orchestrator.qa_graph.graph import recursion_limit
from testpilot_orchestrator.qa_graph.nodes import QaDeps
from testpilot_orchestrator.qa_graph.service import QaService

DOC1 = "urn:testpilot:fs-docs:document:docs/runbook.md"
CHUNK1 = "urn:testpilot:fs-docs:chunk:docs/runbook.md#section-one"
SYM1 = "urn:testpilot:fs-docs:symbol:Sources/Alarm.swift#Alarm.ring"
SYM_DOC = "urn:testpilot:fs-docs:document:Sources/Alarm.swift"


def _answer_env(urn: str, quote: str) -> dict:
    return structured_envelope({
        "summary": f"结论：{quote}",
        "sections": [],
        "citations": [{"urn": urn, "quote": quote}],
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
    row = await service.get_run("run-fixed-1")
    assert row is not None and row["status"] == "answered"
    assert row["question"].startswith("Alarm.ring")
    assert row["thread_id"] == "run-fixed-1"
    assert row["attempts"] == 1 and row["regens"] == 0
    assert row["tokens_in"] == 200 and row["tokens_out"] == 60  # intent+answer 两次信封累加
    assert row["prompt_version"] == "qa-prompt-1"
    assert row["params_version"] == "qa-params-1"
    assert row["data_version"]  # verify 节点写入的 HEAD sha
    assert len(row["verify_results"]) == 1
    assert row["evidence"][0]["urn"] == SYM1
    assert row["citations"][0]["version_state"] == VersionState.CONSISTENT.value
    assert row["answer_payload"]["markdown"]
    assert row["retrieval_event_id"] == 11


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
