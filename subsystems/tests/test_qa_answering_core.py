"""问答工作流 core 纯函数测试（docs/08 FR-QA-001～004，S1.4）。

全部 hostless：不启适配器、不碰文件系统。反假绿要点：
- 守卫函数（validate_citations / should_regenerate）的失败分支必须显式断言错误内容，
  开发期另做「回退守卫→测试变红」的自证（不留在 CI）；
- 历史证词用整句 == 比对，禁止 in（模型改写后的子串会蒙混过关）；
- 三态标注只允许 VersionState 三个合法字面量。
"""

import json

import pytest

from testpilot_core.answering import (
    MAX_ANSWER_ATTEMPTS,
    PARAMS_VERSION,
    QaParams,
    annotate_version_states,
    assess_sufficiency,
    assemble_final,
    assemble_incident,
    build_urn_whitelist,
    check_acl,
    extract_history_testimony,
    fallback_classify_intent,
    normalize_ws,
    qa_run_urn,
    render_citation_quote,
    should_regenerate,
    to_evidence_dict,
    validate_citations,
)
from testpilot_core.enums import (
    AuthorityLevel,
    QaIntent,
    StaleStatus,
    VersionState,
)
from testpilot_core.models import (
    AnswerDraft,
    DraftCitation,
    KnowledgeChunk,
)

DOC_URN = "urn:testpilot:fs-docs:document:docs/kill-backup.md"
CHUNK_URN = "urn:testpilot:fs-docs:chunk:docs/kill-backup.md#kill-backup"
CODE_URN = (
    "urn:testpilot:fs-docs:symbol:Sources/MissionCoordinator.swift"
    "#MissionCoordinator.runSnooze(alarmID:minutes:)"
)
LEGAL_STATES = {VersionState.CONSISTENT, VersionState.VERIFIED_AT, VersionState.UNKNOWN}


def chunk(urn=CHUNK_URN, *, doc_urn=DOC_URN, kind="text", text="证据原文", **meta):
    return KnowledgeChunk(
        urn=urn, doc_urn=doc_urn, index=0, kind=kind, text=text,
        content_hash="hash-" + urn[-6:], metadata_=meta,
    )


def evidence(urn=CHUNK_URN, *, doc_urn=DOC_URN, kind="text", text="证据原文",
             authority="", score=0.5, **extra):
    ev = {
        "urn": urn, "doc_urn": doc_urn, "kind": kind, "text": text,
        "content_hash": "h1", "authority_level": authority,
        "branch": "alarmkit-spike", "valid_from": "abc1234",
        "stale": StaleStatus.FRESH.value, "score": score,
        "source_uri": "file:///repo/docs/kill-backup.md",
        "path": urn.split(":", 3)[-1].partition("#")[0],
        "anchor": urn.partition("#")[2], "doc_content_hash": "dochash",
        "metadata": {},
    }
    ev.update(extra)
    return ev


def draft(*, summary="结论", citations=None):
    return AnswerDraft(
        summary=summary, sections=[],
        # P2 起模型只给 anchor（证据里确实出现的短文本），quote 由服务端渲染
        citations=citations if citations is not None
        else [DraftCitation(urn=CHUNK_URN, anchor="证据原文")],
    )


# ── 兜底意图分类 ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("question", [
    "你好", "您好呀", "hello", "Hi，在吗", "讲个笑话吧", "你是谁？", "谢谢你",
])
def test_fallback_intent_chitchat_is_out_of_scope(question):
    assert fallback_classify_intent(question) == QaIntent.OUT_OF_SCOPE


@pytest.mark.parametrize("question", [
    "MissionCoordinator 是怎么实现的？",
    "runSnooze(alarmID:minutes:) 的逻辑是什么",
    "AlarmKitSpike/Sources/MissionCoordinator.swift 里停钟怎么写的",
    "killBackupInterval 这个符号在哪里被调用",
    "func 声明的回调崩溃了怎么排查",
])
def test_fallback_intent_code_symbol_questions_always_code(question):
    # 误判非 code 会跳过强制 live-verify，代价不对称：符号形态 100% 归 code
    assert fallback_classify_intent(question) == QaIntent.CODE


def test_fallback_intent_explicit_history_words_beat_symbol_shape():
    # 同时含 camelCase 符号与历史词：历史证词并列路径优先
    assert fallback_classify_intent("killBackupInterval 以前是多少秒？") == QaIntent.INCIDENT
    assert fallback_classify_intent("复盘一下停钟事故的变迁") == QaIntent.INCIDENT


def test_fallback_intent_howto_concept_and_conservative_default():
    assert fallback_classify_intent("如何配置接管页？") == QaIntent.HOWTO
    assert fallback_classify_intent("什么是四层兜底？") == QaIntent.CONCEPT
    # 无任何信号：保守归 code（宁多验不漏验）
    assert fallback_classify_intent("随便一个没有关键词的问题") == QaIntent.CODE


# ── ACL ─────────────────────────────────────────────────────────────────────

def test_acl_out_of_scope_short_circuits():
    v = check_acl(QaIntent.OUT_OF_SCOPE, "default")
    assert v.outcome == "out_of_scope" and v.reason


def test_acl_default_profile_allows_every_scope():
    assert check_acl(QaIntent.CODE, "restricted").outcome == "allow"


def test_acl_denies_scope_outside_permitted_list():
    p = QaParams(permitted_scopes=("default",))
    v = check_acl(QaIntent.CODE, "restricted", params=p)
    assert v.outcome == "deny" and "restricted" in v.reason
    assert check_acl(QaIntent.HOWTO, "default", params=p).outcome == "allow"


# ── 充分性 ──────────────────────────────────────────────────────────────────

def test_sufficiency_empty_retrieval_is_insufficient():
    v = assess_sufficiency(QaIntent.CODE, [])
    assert not v.sufficient and v.reason == "empty_retrieval"


def test_sufficiency_weak_score_floor_when_calibrated():
    p = QaParams(min_top_score=0.8)
    weak = assess_sufficiency(QaIntent.HOWTO, [evidence(score=0.2), evidence(score=0.3)], params=p)
    assert not weak.sufficient and weak.reason == "weak_retrieval"
    ok = assess_sufficiency(QaIntent.HOWTO, [evidence(score=0.9)], params=p)
    assert ok.sufficient and ok.score == pytest.approx(0.9)


def test_sufficiency_default_floor_off_only_blocks_empty():
    # S1.4 默认不启用分数门槛（FTS 裸 list 无归一化分数），S1.6 再标定
    assert assess_sufficiency(QaIntent.CODE, [evidence(score=0.0)]).sufficient


# ── state 原语化 ────────────────────────────────────────────────────────────

def test_to_evidence_dict_is_whitelist_and_json_roundtrip():
    c = chunk(
        text="含中文与 unicode 的证据 🚀",
        doc_uri="file:///repo/docs/kill-backup.md",
        doc_authority=AuthorityLevel.L2_EXPERIENCE.value,
        doc_content_hash="dochash",
    )
    ev = to_evidence_dict(c, score=0.91)
    assert set(ev.keys()) == {
        "urn", "doc_urn", "kind", "text", "content_hash", "authority_level",
        "branch", "valid_from", "stale", "score", "source_uri", "path",
        "anchor", "doc_content_hash", "metadata",
    }
    # checkpoint 只能序列化原语：json 往返后逐字段相等
    assert json.loads(json.dumps(ev, ensure_ascii=False)) == ev
    assert ev["stale"] == StaleStatus.FRESH.value  # enum 已转字符串
    assert ev["score"] == 0.91
    assert ev["authority_level"] == AuthorityLevel.L2_EXPERIENCE.value
    assert ev["anchor"] == "kill-backup"
    assert ev["path"] == "docs/kill-backup.md"


def test_urn_whitelist_includes_chunk_and_doc():
    evs = [evidence(urn=CHUNK_URN), evidence(urn="urn:testpilot:fs-docs:chunk:docs/x.md#s",
                                             doc_urn="urn:testpilot:fs-docs:document:docs/x.md")]
    wl = build_urn_whitelist(evs)
    assert CHUNK_URN in wl and DOC_URN in wl
    assert "urn:testpilot:fs-docs:document:docs/x.md" in wl


# ── 锚点渲染（P2：模型只给 anchor，quote 服务端确定性渲染，fail-closed）────────

def test_render_code_symbol_returns_whole_chunk_on_anchor_hit():
    body = "func keep() {\n  print(\"changed\", interval)\n}"
    ev = evidence(urn=CODE_URN, kind="code_symbol", text=body)
    # 短锚点命中 → 渲染整个符号块原文（边界由切块保证，模型不碰 quote）
    assert render_citation_quote(DraftCitation(urn=CODE_URN, anchor="func keep()"), [ev]) == body


def test_render_text_chunk_returns_exact_source_span_on_literal_hit():
    text = "# 标题\n\n项目通过 kill-backup 机制在 30 秒后兜底。超时任务由看门狗回收。"
    ev = evidence(text=text)
    out = render_citation_quote(
        DraftCitation(urn=CHUNK_URN, anchor="kill-backup 机制在 30 秒后兜底"), [ev])
    # 逐字源文切片，不是整段、不是模型转述
    assert out == "kill-backup 机制在 30 秒后兜底"
    assert out in text


def test_render_text_chunk_whitespace_fallback_returns_whole_chunk_verbatim():
    text = "第一行\n  第二行\t收尾"
    ev = evidence(text=text)
    # 锚点空白与源文不一致：归一化能命中时兜底返回整块原文（不做归一化改写输出）
    out = render_citation_quote(
        DraftCitation(urn=CHUNK_URN, anchor="第一行 第二行 收尾"), [ev])
    assert out == text


def test_render_anchor_miss_is_fail_closed_none():
    ev = evidence(text="证据原文")
    # 禁止模糊凑句：锚点不在证据里 → None，由 validate 落 anchor_not_found
    assert render_citation_quote(
        DraftCitation(urn=CHUNK_URN, anchor="模型自己编的句子"), [ev]) is None


def test_render_first_occurrence_is_deterministic():
    text = "let id = 1\nprint(id)"
    ev = evidence(text=text)
    out = render_citation_quote(DraftCitation(urn=CHUNK_URN, anchor="id"), [ev])
    assert out == "id"
    assert text.index(out) == text.index("id")  # 首次命中位置，确定性不靠运气


def test_render_doc_level_uses_first_child_chunk_containing_anchor():
    c1 = evidence(urn="urn:testpilot:fs-docs:chunk:docs/kb.md#s1", text="段落甲内容")
    c2 = evidence(urn="urn:testpilot:fs-docs:chunk:docs/kb.md#s2", text="段落乙内容")
    out = render_citation_quote(DraftCitation(urn=DOC_URN, anchor="段落乙"), [c1, c2])
    assert out == "段落乙"  # 在第二个子块字面命中，返回源文切片
    assert render_citation_quote(
        DraftCitation(urn=DOC_URN, anchor="两段都没有的句子"), [c1, c2]) is None


# ── 引用校验 ────────────────────────────────────────────────────────────────

def test_validate_citations_happy_path():
    v = validate_citations(draft(), [evidence()])
    assert v.ok and not v.errors


def test_validate_citations_rejects_empty_citations():
    v = validate_citations(draft(citations=[]), [evidence()])
    assert not v.ok and any("no_citations" in e for e in v.errors)


def test_validate_citations_rejects_urn_outside_whitelist():
    forged = draft(citations=[DraftCitation(
        urn="urn:testpilot:fs-docs:document:docs/never-retrieved.md", anchor="x")])
    v = validate_citations(forged, [evidence()])
    assert not v.ok and any("urn_not_in_evidence" in e for e in v.errors)


def test_validate_citations_rejects_empty_anchor_and_anchor_not_found():
    v1 = validate_citations(
        draft(citations=[DraftCitation(urn=CHUNK_URN, anchor=" ")]), [evidence()])
    assert any("empty_anchor" in e for e in v1.errors)

    v2 = validate_citations(
        draft(citations=[DraftCitation(urn=CHUNK_URN, anchor="模型自己编的句子")]),
        [evidence()])
    # fail-closed：不做模糊匹配，锚点未命中直接判失败（旧 quote_not_verbatim 语义收口到这里）
    assert any("anchor_not_found" in e for e in v2.errors)


def test_validate_citations_normalizes_whitespace_before_anchor_match():
    messy = evidence(text="第一行\n  第二行\t收尾")
    d = draft(citations=[DraftCitation(urn=CHUNK_URN, anchor="第一行 第二行 收尾")])
    assert validate_citations(d, [messy]).ok
    assert normalize_ws("a\t\n b") == "a b"


def test_validate_citations_doc_level_matches_any_child_chunk():
    evs = [
        evidence(urn="urn:testpilot:fs-docs:chunk:docs/kb.md#s1", text="段落甲内容"),
        evidence(urn="urn:testpilot:fs-docs:chunk:docs/kb.md#s2", text="段落乙内容"),
    ]
    d = draft(citations=[DraftCitation(urn=DOC_URN, anchor="段落乙内容")])
    assert validate_citations(d, evs).ok


# ── 重生成预算（回退自证：把上限临时改 3，test_regen_hard_caps_at_two 必须变红）──

def test_should_regenerate_budget():
    bad = validate_citations(draft(citations=[]), [evidence()])
    good = validate_citations(draft(), [evidence()])
    assert should_regenerate(1, bad) is True
    assert should_regenerate(MAX_ANSWER_ATTEMPTS, bad) is False
    assert should_regenerate(1, good) is False


# ── 三态版本标注 ────────────────────────────────────────────────────────────

def verify_result(urn=CODE_URN, *, degraded=False, exists=True, verified=False,
                  body_changed=None, head_commit="7936295a700c8542034573e64f40bced3d5f80e5"):
    return {"urn": urn, "degraded": degraded, "exists": exists, "verified": verified,
            "body_changed": body_changed, "head_commit": head_commit, "reason": ""}


def annotated(citations, evs, vrs, *, refreshed=None, doc_hashes=None):
    out = annotate_version_states(
        citations, evs, vrs,
        head_refreshed_urns=refreshed, doc_hashes=doc_hashes,
    )
    # 三态穷举：任何产出都必须是合法字面量
    assert all(c.version_state in LEGAL_STATES for c in out)
    return out


def test_annotate_text_chunk_is_consistent_without_verify():
    out = annotated([DraftCitation(urn=CHUNK_URN, anchor="证据原文")], [evidence()], [])
    assert out[0].version_state == VersionState.CONSISTENT
    assert out[0].uri and out[0].content_hash == "h1"
    # quote 是服务端按 anchor 渲染的源文切片，模型草稿里不再有 quote 字段
    assert out[0].quote == "证据原文"


def test_annotate_code_unchanged_consistent_renders_whole_symbol_block():
    body = "func keep() {\n  print(interval)\n}"
    evs = [evidence(urn=CODE_URN, kind="code_symbol", text=body)]
    vrs = [verify_result(verified=True, body_changed=False)]
    vrs[0]["reason"] = "unchanged"
    out = annotated([DraftCitation(urn=CODE_URN, anchor="func keep()")], evs, vrs)
    assert out[0].version_state == VersionState.CONSISTENT
    assert out[0].quote == body  # 代码块引用渲染整个符号体


def test_annotate_degraded_verify_goes_unknown_not_consistent():
    evs = [evidence(urn=CODE_URN, kind="code_symbol")]
    vrs = [verify_result(degraded=True, exists=None)]
    out = annotated([DraftCitation(urn=CODE_URN, anchor="证据原文")], evs, vrs)
    assert out[0].version_state == VersionState.UNKNOWN


def test_annotate_stale_index_refreshed_at_head():
    evs = [evidence(urn=CODE_URN, kind="code_symbol", content_hash="head-new-hash")]
    vrs = [verify_result(verified=False, body_changed=True)]
    out = annotated([DraftCitation(urn=CODE_URN, anchor="证据原文")], evs, vrs,
                    refreshed=[CODE_URN])
    assert out[0].version_state == VersionState.VERIFIED_AT
    assert out[0].version_ref == "7936295"  # 短 sha
    assert out[0].content_hash == "head-new-hash"  # 刷新后的证据块带新 hash


def test_annotate_stale_index_refresh_failed_goes_unknown():
    evs = [evidence(urn=CODE_URN, kind="code_symbol")]
    vrs = [verify_result(verified=False, body_changed=True)]
    # body_changed 但不在 head_refreshed_urns（HEAD 重读失败路径）
    out = annotated([DraftCitation(urn=CODE_URN, anchor="证据原文")], evs, vrs, refreshed=[])
    assert out[0].version_state == VersionState.UNKNOWN


def test_annotate_symbol_missing_and_missing_verify_row_go_unknown():
    evs = [evidence(urn=CODE_URN, kind="code_symbol")]
    out1 = annotated([DraftCitation(urn=CODE_URN, anchor="证据原文")], evs,
                     [verify_result(exists=False, body_changed=None)])
    assert out1[0].version_state == VersionState.UNKNOWN
    # 接线 bug 防御：code 证据没有对应 verify 行 → 未知，绝不默认一致
    out2 = annotated([DraftCitation(urn=CODE_URN, anchor="证据原文")], evs, [])
    assert out2[0].version_state == VersionState.UNKNOWN


def test_annotate_doc_level_aggregation():
    c1 = evidence(urn="urn:testpilot:fs-docs:symbol:doc#a", kind="code_symbol", doc_urn=DOC_URN)
    c2 = evidence(urn="urn:testpilot:fs-docs:symbol:doc#b", kind="code_symbol", doc_urn=DOC_URN)
    vrs = [
        verify_result(urn=c1["urn"], verified=True, body_changed=False),
        verify_result(urn=c2["urn"], verified=False, body_changed=True),
    ]
    # 一个刷新一个一致 → 文档级 verified_at
    out = annotated([DraftCitation(urn=DOC_URN, anchor="证据原文")], [c1, c2], vrs,
                    refreshed=[c2["urn"]], doc_hashes={DOC_URN: "dochash"})
    assert out[0].version_state == VersionState.VERIFIED_AT
    assert out[0].content_hash == "dochash"

    # 任一未知 → 文档级未知
    vrs[1] = verify_result(urn=c2["urn"], degraded=True, exists=None)
    out2 = annotated([DraftCitation(urn=DOC_URN, anchor="证据原文")], [c1, c2], vrs,
                     doc_hashes={DOC_URN: "dochash"})
    assert out2[0].version_state == VersionState.UNKNOWN


# ── 事故历史证词（FR-QA-003）────────────────────────────────────────────────

def test_extract_history_testimony_filters_l2_and_dedupes():
    evs = [
        evidence(urn="urn:testpilot:fs-docs:chunk:kb#s1",
                 authority=AuthorityLevel.L2_EXPERIENCE.value, text="早期版本 killBackupInterval = 90s"),
        evidence(urn="urn:testpilot:fs-docs:chunk:kb#s1",  # 同 URN 重复
                 authority=AuthorityLevel.L2_EXPERIENCE.value, text="早期版本 killBackupInterval = 90s"),
        evidence(urn="urn:testpilot:fs-docs:chunk:kb#s2",
                 authority=AuthorityLevel.L1_AUTHORITATIVE.value, text="当前规范 30s"),
    ]
    history = extract_history_testimony(evs)
    assert len(history) == 1
    # 整句等值，不用 in
    assert history[0].text == "早期版本 killBackupInterval = 90s"


def test_assemble_incident_verbatim_sections_and_disjoint_urns():
    from testpilot_core.models import HistoryTestimony, QaCitation

    history = [HistoryTestimony(
        text="早期版本 killBackupInterval = 90s；2026-09 调整为 30s",
        doc_urn="urn:testpilot:fs-docs:document:docs/incident.md",
        source_uri="file:///repo/docs/incident.md", date="2026-09",
    )]
    cite = QaCitation(urn=CHUNK_URN, uri="file:///repo/docs/current.md",
                      version_state=VersionState.CONSISTENT)
    d = draft(summary="当前为 30s")
    evs = [evidence()]  # 引用文档 DOC_URN，与历史文档不同
    answer = assemble_incident(history, d, [cite], evidence=evs)

    assert "## 历史证词（逐字引用，未经模型改写）" in answer.markdown
    assert "## 当前结论" in answer.markdown
    assert history[0].text in answer.markdown  # 渲染包含；逐字等值已在上面整句断言
    assert answer.history[0].text == history[0].text
    assert answer.citations[0].urn == CHUNK_URN

    # URN 交叉 → 拒绝装配（防模型引用污染历史文档）
    cross_ev = evidence(doc_urn="urn:testpilot:fs-docs:document:docs/incident.md")
    cross_cite = QaCitation(urn=cross_ev["urn"], version_state=VersionState.CONSISTENT)
    cross_draft = draft(citations=[DraftCitation(urn=cross_ev["urn"])])
    with pytest.raises(ValueError, match="交叉"):
        assemble_incident(history, cross_draft, [cross_cite], evidence=[cross_ev])

    # 文档级引用同样必须拦：citation.urn 直接等于历史文档 URN。录制夹具的
    # qa_answer_kill_backup 就是文档级引用，证明该粒度是受支持形态，不能留洞
    # （旧实现只比对 chunk URN，文档级交叉会静默放行，污染历史证词边界）。
    incident_doc = "urn:testpilot:fs-docs:document:docs/incident.md"
    doc_cross_cite = QaCitation(urn=incident_doc, version_state=VersionState.CONSISTENT)
    doc_cross_draft = draft(citations=[DraftCitation(urn=incident_doc)])
    with pytest.raises(ValueError, match="交叉"):
        assemble_incident(history, doc_cross_draft, [doc_cross_cite],
                          evidence=[cross_ev])


def test_assemble_final_joins_summary_and_sections():
    d = AnswerDraft(summary="摘要", sections=["分节一", "分节二"],
                    citations=[DraftCitation(urn=CHUNK_URN)])
    from testpilot_core.models import QaCitation
    cite = QaCitation(urn=CHUNK_URN, version_state=VersionState.CONSISTENT)
    answer = assemble_final(d, [cite])
    assert answer.markdown == "摘要\n\n分节一\n\n分节二"
    assert answer.citations[0].urn == CHUNK_URN


# ── 反馈 URN 约定与版本位 ───────────────────────────────────────────────────

def test_qa_run_urn_convention():
    assert qa_run_urn("run-42") == "urn:testpilot:qa-run:run-42"


def test_params_version_pinned():
    # 改 QaParams 默认值必须 bump PARAMS_VERSION（评测四元组可复算）
    assert PARAMS_VERSION == "qa-params-1"
    assert QaParams().min_evidence == 1
