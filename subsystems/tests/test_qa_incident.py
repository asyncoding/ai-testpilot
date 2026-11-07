"""FR-QA-003 事故问答图级测试：历史证词逐字并列、模型不碰原文、两节 URN 不交叉。

与既有覆盖的分工：
- test_qa_answering_core.py：assemble_incident/extract_history_testimony 纯函数；
- test_qa_nodes.py：单节点装配（手工构造 state/draft）；
- 本文件：编译后的完整图 + 真实 RecordedModelGateway，验证录制夹具驱动下
  检索→L2 抽取→生成→引用校验→双段装配的整条链路，以及模型越界引用历史
  文档时图在装配层硬停（ValueError 抛穿，绝不静默出带污染的答案）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from qa_fakes import FakeHybridSearch, FakeModel, make_chunk, structured_envelope
from testpilot_adapters.recorded_model import RecordedModelGateway
from testpilot_core.enums import AuthorityLevel, QaIntent, QaStatus
from testpilot_orchestrator.qa_graph.graph import build_qa_graph, recursion_limit
from testpilot_orchestrator.qa_graph.nodes import QaDeps

FIXTURES = Path(__file__).parent / "fixtures" / "model" / "responses.json"

HIST_DOC = "urn:testpilot:fs-docs:document:docs/postmortem-2026-01.md"
HIST_CHUNK = "urn:testpilot:fs-docs:chunk:docs/postmortem-2026-01.md#kill-backup-90s"
# 与录制夹具 qa_answer_kill_backup 的 document 级引用 URN 严格一致；
# 外部形态对齐真实 fs-docs 适配器产出（RootSpec.alias=docs，见 fs_docs._external_id）
CUR_DOC = "urn:testpilot:fs-docs:document:docs/kill-backup.md"
CUR_CHUNK = "urn:testpilot:fs-docs:chunk:docs/kill-backup.md#section-one"
HISTORY_TEXT = "早期版本 killBackupInterval = 90 秒，应用进入后台后被系统挂起，闹钟未按预期触发。"
CURRENT_QUOTE = "项目通过 kill-backup 机制在 30 秒后兜底。"


def _chunks():
    history = make_chunk(
        HIST_CHUNK, doc_urn=HIST_DOC, text=HISTORY_TEXT,
        metadata={
            "doc_authority": AuthorityLevel.L2_EXPERIENCE.value,
            "doc_uri": "file:///repo/docs/postmortem-2026-01.md",
            "date": "2026-01-15", "version": "v1.2",
        },
    )
    current = make_chunk(
        CUR_CHUNK, doc_urn=CUR_DOC,
        text=f"# kill-backup 兜底机制\n\n{CURRENT_QUOTE} 超时任务由看门狗回收。",
        metadata={
            "doc_authority": AuthorityLevel.L1_AUTHORITATIVE.value,
            "doc_uri": "file:///repo/tests/fixtures/docs/kill-backup.md",
        },
    )
    return [history, current]


async def _invoke(deps: QaDeps, *, case_key: str, run_id: str, question: str) -> dict:
    graph = build_qa_graph(deps)
    return await graph.ainvoke(
        {"run_id": run_id, "case_key": case_key, "question": question,
         "project": "demo", "branch": "main", "acl_scope": "default"},
        config={"configurable": {"thread_id": run_id},
                "recursion_limit": recursion_limit()},
    )


async def test_recorded_incident_question_renders_verbatim_dual_sections():
    search = FakeHybridSearch(_chunks())
    deps = QaDeps(model=RecordedModelGateway(FIXTURES), search=search)

    out = await _invoke(
        deps, case_key="kill_backup", run_id="run-incident-1",
        question="kill-backup 90 秒那次事故是怎么回事？现在规范是什么？",
    )

    assert out["status"] == QaStatus.ANSWERED.value
    assert out["intent"] == QaIntent.INCIDENT.value
    assert out["intent_degraded"] is False  # 意图来自录制模型而非关键词兜底
    # 事故意图不触发 live-verify（只有 code 强制校验）
    assert search.verify_calls == []

    answer = out["final_answer"]
    # 历史证词整句等值，禁用 in 防子串假绿
    assert len(answer["history"]) == 1
    assert answer["history"][0]["text"] == HISTORY_TEXT
    assert answer["history"][0]["doc_urn"] == HIST_DOC

    md = answer["markdown"]
    assert "## 历史证词（逐字引用，未经模型改写）" in md
    assert "## 当前结论" in md
    lines = md.splitlines()
    # 引用块整行等值：模型摘要里即便出现近义表述也冒充不了逐字证词
    assert f"> {HISTORY_TEXT}" in lines
    # 出处行由装配层拼（date · version · source_uri），不经过模型
    assert any(line.startswith("> 来源：2026-01-15 · v1.2 · ") for line in lines)

    # 两节 URN 物理隔离：历史文档绝不进模型引用，模型只引当前规范文档
    citation_urns = {c["urn"] for c in answer["citations"]}
    history_doc_urns = {h["doc_urn"] for h in answer["history"]}
    assert citation_urns == {CUR_DOC}
    assert history_doc_urns.isdisjoint(citation_urns)
    # 当前段引用由 anchor 确定性渲染：文本块命中即回填 anchor 原文本身
    assert answer["citations"][0]["quote"] == CURRENT_QUOTE


async def test_graph_hard_stops_when_model_cites_history_document():
    # 模型越界：草稿直接引用历史文档（文档级 URN，anchor 逐字能过引用校验，
    # 唯一防线是 assemble_incident 的交叉守卫），图必须抛错而不是静默出答案。
    model = FakeModel({
        "qa_intent_cross": structured_envelope({"intent": "incident"}),
        "qa_answer_cross": structured_envelope({
            "summary": "错误地把历史文档当当前规范引用。",
            "sections": [],
            "citations": [{"urn": HIST_DOC, "anchor": HISTORY_TEXT}],
        }),
    })
    search = FakeHybridSearch(_chunks())
    deps = QaDeps(model=model, search=search)

    with pytest.raises(ValueError, match="交叉"):
        await _invoke(
            deps, case_key="cross", run_id="run-incident-cross",
            question="kill-backup 90 秒那次事故是怎么回事？",
        )
    assert search.verify_calls == []
    assert model.count("qa_answer_cross") == 1  # 确实走到了生成，防线在装配层
