"""录制模型 qa_* 夹具契约（S1.4 C5）。

两层保障：
1. 夹具文件层：所有 qa_intent_* 的枚举合法、所有非降级 qa_answer_* 能过
   AnswerDraft schema（夹具手写 JSON，漂移在这里第一时间变红）；
2. 图执行层：真实 RecordedModelGateway + fake 检索跑通不可答/闲聊/持续伪造/
   首败次成四条路径，证明录制键命名与节点 _model_key 约定严格对齐。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from qa_fakes import FakeHybridSearch, make_chunk
from testpilot_adapters.recorded_model import RecordedModelGateway
from testpilot_core.answering import to_evidence_dict
from testpilot_core.enums import QaIntent, QaStatus
from testpilot_core.errors import AdapterUnavailableError
from testpilot_core.models import AnswerDraft
from testpilot_orchestrator.qa_graph.graph import build_qa_graph, recursion_limit
from testpilot_orchestrator.qa_graph.nodes import QaDeps

FIXTURES = Path(__file__).parent / "fixtures" / "model" / "responses.json"
CHUNK1 = "urn:testpilot:fs-docs:chunk:docs/runbook.md#section-one"
DOC1 = "urn:testpilot:fs-docs:document:docs/runbook.md"
VALID_INTENTS = {i.value for i in QaIntent}


def _raw() -> dict:
    return json.loads(FIXTURES.read_text(encoding="utf-8"))


def test_fixture_intent_keys_use_legal_enum_values():
    raw = _raw()
    intent_keys = [k for k in raw if k.startswith("qa_intent_")]
    assert intent_keys  # 夹具存在性守卫：空集合会让下面的断言假绿
    for key in intent_keys:
        assert raw[key]["structured"]["intent"] in VALID_INTENTS, key


def test_fixture_answer_keys_match_answer_draft_contract():
    raw = _raw()
    answer_keys = [
        k for k in raw
        if k.startswith("qa_answer_") and not raw[k].get("degraded")
    ]
    assert answer_keys
    for key in answer_keys:
        draft = AnswerDraft.model_validate(raw[key]["structured"])
        assert draft.citations, f"{key} 违反无引用结论率=0"
        assert all(c.urn.startswith("urn:testpilot:") for c in draft.citations)


def test_legacy_keys_remain_untouched():
    # S0 契约测试依赖的三个旧键，新夹具不许破坏
    raw = _raw()
    assert set(("default", "qa_kill_backup", "qa_unanswerable")) <= set(raw)


async def test_missing_recorded_key_raises_adapter_unavailable():
    gw = RecordedModelGateway(FIXTURES)
    with pytest.raises(AdapterUnavailableError):
        await gw.structured([], {"type": "object"}, key="qa_answer_ghost")


async def _drive(case_key: str, question: str) -> dict:
    chunk = make_chunk(CHUNK1, doc_urn=DOC1, text="证据原文")
    deps = QaDeps(
        model=RecordedModelGateway(FIXTURES),
        search=FakeHybridSearch([chunk]),
    )
    graph = build_qa_graph(deps)
    return await graph.ainvoke(
        {"run_id": f"run-{case_key}", "case_key": case_key,
         "question": question, "project": "demo", "branch": "main",
         "acl_scope": "default"},
        config={"configurable": {"thread_id": f"run-{case_key}"},
                "recursion_limit": recursion_limit()},
    )


async def test_recorded_unanswerable_degrades_to_unknown():
    out = await _drive("unanswerable", "如何配置一个夹具里不存在的能力？")
    assert out["status"] == QaStatus.UNKNOWN.value
    assert out["unknown_reason"] == "model_unavailable"
    assert "model_degraded_envelope" in out["degraded_flags"]


async def test_recorded_chitchat_stops_at_acl_without_answer_key():
    out = await _drive("chitchat", "你好，讲个笑话")
    assert out["status"] == QaStatus.UNKNOWN.value
    assert out["unknown_reason"] == "out_of_scope"
    assert out.get("final_answer") is None
    # 夹具里故意没有 qa_answer_chitchat：若图误调生成，缺 key 会直接抛异常使本测失败


async def test_recorded_persistent_forgery_exhausts_regeneration_budget():
    out = await _drive("forged", "如何配置备份间隔？")
    assert out["status"] == QaStatus.UNKNOWN.value
    assert out["unknown_reason"] == "citation_failed_exhausted"
    assert out["attempts"] == 2
    assert all("urn_not_in_evidence" in e for e in out["citation_errors"])


async def test_recorded_first_fail_then_retry_pass_is_answered():
    out = await _drive("retry_ok", "如何配置备份间隔？")
    assert out["status"] == QaStatus.ANSWERED.value
    assert out["attempts"] == 2
    cite = out["final_answer"]["citations"][0]
    assert cite["urn"] == CHUNK1
    # 逐字 quote 来自夹具且与证据文本严格一致
    assert cite["quote"] == "证据原文"
    evidence = [to_evidence_dict(make_chunk(CHUNK1, doc_urn=DOC1, text="证据原文"))]
    assert cite["quote"] in evidence[0]["text"]
