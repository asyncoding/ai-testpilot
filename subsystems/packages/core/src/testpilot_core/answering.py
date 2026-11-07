"""问答工作流纯逻辑（docs/08 FR-QA-001～004）：零 IO、零三方 SDK，import-lint 守门。

LangGraph 节点只负责调适配器/模型与状态搬运；所有可 hostless 测试的规则都在本模块：
意图兜底分类、ACL 前置判定、证据充分性、引用白名单/逐字校验、重生成预算、
三态版本标注、事故历史证词装配、checkpoint state 原语化。

纪律：
- 图的 state 只流通 JSON 原语（checkpoint 序列化限制），证据块统一经
  to_evidence_dict 白名单转换，pydantic/datetime 一律不进 state；
- 无证据不输出结论：引用为空/URN 不在白名单/anchor 在证据中逐字找不到 →
  校验失败，最多重生成一次（MAX_ANSWER_ATTEMPTS），仍失败由图降级为「未知」；
- P2 起模型只给短 anchor，最终 quote 由本模块服务端渲染，模型文本永不直接
  进入 QaCitation.quote；
- 三态标注里 None（无法判定）只能落 unknown，绝不能冒充 consistent。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .enums import AuthorityLevel, QaIntent, VersionState
from .models import (
    AclVerdict,
    AnswerDraft,
    AssembledAnswer,
    CitationVerdict,
    DraftCitation,
    HistoryTestimony,
    KnowledgeChunk,
    QaCitation,
    SufficiencyVerdict,
)
from .urn import parse_urn

# 全图重生成硬上限：首次生成 + 1 次修复（docs/08 FR-QA-001 AC：失败重生成一次后未知）
MAX_ANSWER_ATTEMPTS = 2
# 参数版本位：S1.6 标定只改 QaParams 默认值并 bump 本常量，机制不动（评测四元组对齐）
PARAMS_VERSION = "qa-params-1"
# run 级反馈的 target_urn 约定（feedback 表 target_urn NOT NULL，且不引入 DB FK）
RUN_URN_PREFIX = "urn:testpilot:qa-run:"

# ── 兜底意图关键词（模型分类失败时启用；显式标记优先于符号形态）──────────────
_OUT_OF_SCOPE_RE = re.compile(
    r"^(?:hi|hello|hey|thanks|thank you)\b|你好|您好|谢谢你?|讲个笑话|聊聊天?|你是谁|天气",
    re.IGNORECASE,
)
_INCIDENT_RE = re.compile(r"事故|复盘|故障|缺陷|回归|历史|以前|原本|原来|曾经|过往|踩坑|变迁")
_HOWTO_RE = re.compile(r"怎么|怎样|如何|步骤|教程|接入|配置|操作")
_CONCEPT_RE = re.compile(r"什么是|是什么|含义|概念|区别|原理|定义")
# 代码强信号：.swift 文件名 / camelCase 符号（runSnooze）/ 声明关键字 / 实现类问法
_CAMEL_RE = re.compile(r"[a-z][A-Za-z0-9]*[A-Z][A-Za-z0-9]*")
_CODE_RE = re.compile(r"\.swift\b|\bfunc\b|\bclass\b|\bstruct\b|符号|函数|方法|代码|崩溃|调用栈|源码|实现")

# 证据 dict 白名单：state/checkpoint 里只允许出现这些键（防 pydantic/datetime 混入）
_EVIDENCE_KEYS = (
    "urn", "doc_urn", "kind", "text", "content_hash", "authority_level",
    "branch", "valid_from", "stale", "score", "source_uri", "path",
    "anchor", "doc_content_hash", "metadata",
)


@dataclass(frozen=True)
class QaParams:
    """问答判定参数（保守默认；S1.6 用 golden 题集标定，改参必须 bump PARAMS_VERSION）。"""

    min_evidence: int = 1
    # 0.0 = 关闭分数门槛（FTS 裸 list 无归一化分数，默认只拦空召回）
    min_top_score: float = 0.0
    # 空元组 = 不做 scope 限制（POC 无 IAM）；注入后 acl_scope 不在名单即拒
    permitted_scopes: tuple[str, ...] = ()


def qa_run_urn(run_id: str) -> str:
    """run 级反馈 target_urn 约定：urn:testpilot:qa-run:<run_id>。"""
    return f"{RUN_URN_PREFIX}{run_id}"


# ── 意图 ──────────────────────────────────────────────────────────────────

def fallback_classify_intent(question: str) -> QaIntent:
    """模型不可用时的关键词兜底分类。

    显式历史词优先于符号形态（问「某符号以前的值」要走事故并列路径）；
    其余拿不准的一律归 code——误判非 code 会跳过强制 live-verify，代价不对称，
    宁多验不漏验。
    """
    q = question.strip()
    if _OUT_OF_SCOPE_RE.search(q):
        return QaIntent.OUT_OF_SCOPE
    if _INCIDENT_RE.search(q):
        return QaIntent.INCIDENT
    if ".swift" in q or _CAMEL_RE.search(q) or _CODE_RE.search(q):
        return QaIntent.CODE
    if _HOWTO_RE.search(q):
        return QaIntent.HOWTO
    if _CONCEPT_RE.search(q):
        return QaIntent.CONCEPT
    return QaIntent.CODE


# ── ACL ───────────────────────────────────────────────────────────────────

def check_acl(
    intent: QaIntent, acl_scope: str, *, params: QaParams | None = None
) -> AclVerdict:
    """scope 级前置判定。文档级过滤在检索适配器（hybrid 吃 acl_scope）；
    FTS 适配器当前不支持文档级过滤，调用方不得在此写会假绿的文档级断言。"""
    if intent == QaIntent.OUT_OF_SCOPE:
        return AclVerdict(outcome="out_of_scope", reason="通用聊天/闲聊不在本产品范围")
    permitted = (params or QaParams()).permitted_scopes
    if permitted and acl_scope not in permitted:
        return AclVerdict(outcome="deny", reason=f"acl_scope 不在授权名单: {acl_scope}")
    return AclVerdict(outcome="allow")


# ── 充分性 ────────────────────────────────────────────────────────────────

def assess_sufficiency(
    intent: QaIntent, evidence: list[dict], *, params: QaParams | None = None
) -> SufficiencyVerdict:
    """证据充分性判定。空召回/弱召回 → 不足，图据此早终止为「未知」，不进生成。"""
    p = params or QaParams()
    if not evidence:
        return SufficiencyVerdict(sufficient=False, reason="empty_retrieval")
    top_score = max(float(e.get("score") or 0.0) for e in evidence)
    if len(evidence) < p.min_evidence:
        return SufficiencyVerdict(sufficient=False, reason="empty_retrieval", score=top_score)
    if p.min_top_score > 0 and top_score < p.min_top_score:
        return SufficiencyVerdict(sufficient=False, reason="weak_retrieval", score=top_score)
    return SufficiencyVerdict(sufficient=True, score=top_score)


# ── state 原语化 ───────────────────────────────────────────────────────────

def to_evidence_dict(chunk: KnowledgeChunk, *, score: float | None = None) -> dict:
    """KnowledgeChunk → state 白名单 dict（全 JSON 原语，可过 checkpoint 序列化）。

    doc_uri/doc_authority/doc_content_hash 由混合检索适配器折进 chunk.metadata；
    FTS 裸 list 路径缺这些键时如实留空，不编造。
    """
    meta = chunk.metadata_ or {}
    try:
        _system, _rtype, external, _version = parse_urn(chunk.urn)
        doc_external, _sep, anchor = external.partition("#")
    except ValueError:
        doc_external, anchor = "", ""
    return {
        "urn": chunk.urn,
        "doc_urn": chunk.doc_urn,
        "kind": chunk.kind,
        "text": chunk.text,
        "content_hash": chunk.content_hash,
        "authority_level": str(meta.get("doc_authority") or ""),
        "branch": chunk.branch,
        "valid_from": chunk.valid_from,
        "stale": chunk.stale.value,
        "score": float(score) if score is not None else 0.0,
        "source_uri": str(meta.get("doc_uri") or ""),
        "path": doc_external,
        "anchor": anchor,
        "doc_content_hash": str(meta.get("doc_content_hash") or ""),
        "metadata": meta,
    }


def build_urn_whitelist(evidence: list[dict]) -> set[str]:
    """可引用 URN 白名单 = 证据块 URN ∪ 所属文档 URN（兼容 document 级引用）。"""
    allowed: set[str] = set()
    for e in evidence:
        allowed.add(e["urn"])
        if e.get("doc_urn"):
            allowed.add(e["doc_urn"])
    return allowed


# ── 锚点渲染 + 引用校验 ──────────────────────────────────────────────────────

def normalize_ws(text: str) -> str:
    return " ".join(text.split())


def _render_from_chunk(citation: DraftCitation, ev: dict) -> str | None:
    """单个证据块内的锚点渲染；未命中返回 None（调用方 fail-closed，不模糊凑句）。

    - 字面命中：code_symbol 块渲染整个符号体（切块边界即引用边界，天然逐字、
      边界天然正确）；text 等块返回锚点本身——字面 in 命中已证明它与源文该跨度
      字节相同，等价于源文切片，模型拿不到任何改写进入 quote 的机会。
    - 仅空白归一化后命中（换行/缩进差异）：兜底返回整块原文，输出绝不做归一化
      改写（normalize_ws 只用于准入判定）。
    """
    text = ev["text"]
    if citation.anchor in text:
        return text if ev.get("kind") == "code_symbol" else citation.anchor
    if normalize_ws(citation.anchor) in normalize_ws(text):
        return text
    return None


def render_citation_quote(
    citation: DraftCitation, evidence: list[dict]
) -> str | None:
    """按草稿锚点从证据渲染最终引用文本（纯函数，零 IO）。

    块级 URN 直接渲染该块；文档级 URN 在该文档的证据块里按 evidence 顺序取
    第一个命中块（确定性，不做相关性重排）。任何一级未命中都返回 None，由
    validate_citations 落 anchor_not_found——禁止服务端自行脑补引用。
    """
    if not citation.anchor.strip():
        return None
    by_urn = {e["urn"]: e for e in evidence}
    target = by_urn.get(citation.urn)
    if target is not None:
        return _render_from_chunk(citation, target)
    for ev in evidence:  # 文档级引用：子块顺序即检索融合顺序
        if ev.get("doc_urn") == citation.urn:
            rendered = _render_from_chunk(citation, ev)
            if rendered is not None:
                return rendered
    return None


def validate_citations(draft: AnswerDraft, evidence: list[dict]) -> CitationVerdict:
    """引用三招校验（模型输出侧最后防线，prompt 分隔约束是第一防线）：

    1) 草稿不允许零引用（无引用结论率=0；闲聊在 acl 节点已收口，到不了这里）；
    2) citation.urn 必须 ∈ 检索证据白名单（块 URN 或文档 URN）；
    3) anchor 必须能在对应证据里逐字（或空白归一化后）命中，渲染由
       render_citation_quote 确定性完成；未命中 fail-closed 落 anchor_not_found。
       hash 对账在装配层：草稿不带 hash，证据块自带 content_hash，由
       annotate_version_states 落到最终 QaCitation。
    """
    errors: list[str] = []
    if not draft.citations:
        return CitationVerdict(ok=False, errors=["no_citations: 草稿未给出任何引用"])

    whitelist = build_urn_whitelist(evidence)
    for c in draft.citations:
        if c.urn not in whitelist:
            errors.append(f"urn_not_in_evidence: {c.urn}")
            continue
        if not c.anchor.strip():
            errors.append(f"empty_anchor: {c.urn}")
            continue
        rendered = render_citation_quote(c, evidence)
        if rendered is None:
            errors.append(f"anchor_not_found: {c.urn}")
            continue
        # 渲染器不变量兜底：自己渲染的文本必然包含锚点（归一化口径）；
        # 不成立只可能是渲染器接线 bug，保留 quote_not_verbatim 前缀报警
        if normalize_ws(c.anchor) not in normalize_ws(rendered):
            errors.append(f"quote_not_verbatim: {c.urn}")
    return CitationVerdict(ok=not errors, errors=errors)


def should_regenerate(attempt: int, verdict: CitationVerdict) -> bool:
    """重生成预算唯一事实源。attempt 为已生成次数（1-based）；上限 2。"""
    return not verdict.ok and attempt < MAX_ANSWER_ATTEMPTS


# ── 三态版本标注 ───────────────────────────────────────────────────────────

def _state_for_chunk(ev: dict, verify_results: list[dict], refreshed: set[str]) -> tuple[VersionState, str]:
    if ev.get("kind") != "code_symbol":
        # 非代码证据：新鲜检索所得，无 live-verify 口径，按一致输出
        return VersionState.CONSISTENT, ""
    vr = next((v for v in verify_results if v.get("urn") == ev["urn"]), None)
    if vr is None:
        # 代码意图的代码证据必有校验行（节点 100% 调用承诺）；缺失只可能是接线 bug
        return VersionState.UNKNOWN, ""
    if vr.get("degraded") or vr.get("exists") is None:
        # None 不冒充 False：git 不可达等降级场景一律未知
        return VersionState.UNKNOWN, ""
    if ev["urn"] in refreshed and vr.get("head_commit"):
        return VersionState.VERIFIED_AT, str(vr["head_commit"])[:7]
    if vr.get("verified"):
        return VersionState.CONSISTENT, ""
    # exists=False / body_changed 且刷新失败：事实存疑，未知收口
    return VersionState.UNKNOWN, ""


def annotate_version_states(
    citations: list[DraftCitation],
    evidence: list[dict],
    verify_results: list[dict],
    *,
    head_refreshed_urns: list[str] | None = None,
    doc_hashes: dict[str, str] | None = None,
) -> list[QaCitation]:
    """把草稿引用升级为带 URI/hash/三态的最终引用。

    文档级引用按其全部证据块聚合：任一未知→未知；否则任一验证于新版本→该态；
    否则一致。content_hash 块级取证据块（HEAD 刷新后即为新 hash），文档级取
    调用方反查 SourceDocument 得到的 doc_hashes。
    """
    refreshed = set(head_refreshed_urns or ())
    by_urn = {e["urn"]: e for e in evidence}
    chunks_by_doc: dict[str, list[dict]] = {}
    for e in evidence:
        chunks_by_doc.setdefault(e["doc_urn"], []).append(e)

    out: list[QaCitation] = []
    for c in citations:
        # 能进装配说明 validate_citations 已过：渲染必然命中；None 只可能是
        # 绕过校验的接线 bug，quote 留空让三态/URN 仍可落，不凭空造文本
        rendered = render_citation_quote(c, evidence) or ""
        ev = by_urn.get(c.urn)
        if ev is not None:  # 块级引用
            state, ref = _state_for_chunk(ev, verify_results, refreshed)
            out.append(QaCitation(
                urn=c.urn, uri=ev.get("source_uri", ""), quote=rendered,
                version_state=state, version_ref=ref,
                content_hash=ev.get("content_hash", ""),
            ))
            continue
        # 文档级引用：子块状态聚合
        children = chunks_by_doc.get(c.urn, [])
        states = [_state_for_chunk(ch, verify_results, refreshed) for ch in children]
        refs = [ref for _s, ref in states if ref]
        if not states or any(s == VersionState.UNKNOWN for s, _ in states):
            state, ref = VersionState.UNKNOWN, ""
        elif any(s == VersionState.VERIFIED_AT for s, _ in states):
            state, ref = VersionState.VERIFIED_AT, refs[0] if refs else ""
        else:
            state, ref = VersionState.CONSISTENT, ""
        out.append(QaCitation(
            urn=c.urn,
            uri=children[0].get("source_uri", "") if children else "",
            quote=rendered, version_state=state, version_ref=ref,
            content_hash=(doc_hashes or {}).get(c.urn, ""),
        ))
    return out


# ── 事故历史证词（FR-QA-003）───────────────────────────────────────────────

def extract_history_testimony(evidence: list[dict]) -> list[HistoryTestimony]:
    """从 L2_experience 文档证据块抽历史证词；text 保持逐字原文，模型永不接触。"""
    out: list[HistoryTestimony] = []
    seen: set[str] = set()
    for e in evidence:
        if e.get("authority_level") != AuthorityLevel.L2_EXPERIENCE.value:
            continue
        if e["urn"] in seen:
            continue
        seen.add(e["urn"])
        meta = e.get("metadata") or {}
        out.append(HistoryTestimony(
            text=e["text"],
            doc_urn=e["doc_urn"],
            source_uri=e.get("source_uri", ""),
            date=str(meta.get("date") or ""),
            version=str(meta.get("version") or ""),
        ))
    return out


def assemble_incident(
    history: list[HistoryTestimony],
    draft: AnswerDraft,
    citations: list[QaCitation],
    *,
    evidence: list[dict],
) -> AssembledAnswer:
    """事故回答双段装配：历史段逐字引用（模型不碰原文），当前段才是模型摘要。

    两节 URN 不交叉：历史证词文档不得同时出现在模型引用里，防止改写污染历史。
    """
    history_docs = {h.doc_urn for h in history}
    # 引用粒度有两种（chunk URN / document URN，后者是录制夹具在用的受支持形态）：
    # citation 命中证据块取其 doc_urn，直接命中文档 URN 也算该文档被引用，
    # 只比对 chunk URN 会让文档级交叉静默放行（S1.4 图级 incident 测试补红）。
    citation_urns = {c.urn for c in citations}
    cited_docs = {
        e["doc_urn"] for e in evidence
        if e["urn"] in citation_urns or e.get("doc_urn") in citation_urns
    }
    overlap = history_docs & cited_docs
    if overlap:
        raise ValueError(f"历史证词与模型引用的文档交叉，拒绝装配: {sorted(overlap)}")

    blocks = ["## 历史证词（逐字引用，未经模型改写）"]
    for h in history:
        provenance = " · ".join(p for p in (h.date, h.version, h.source_uri) if p)
        blocks.append(f"\n> {h.text.strip()}" + (f"\n>\n> 来源：{provenance}" if provenance else ""))
    blocks.append("\n## 当前结论\n")
    blocks.append(draft.summary.strip())
    blocks.extend(s.strip() for s in draft.sections if s.strip())
    return AssembledAnswer(
        markdown="\n".join(blocks),
        sections=list(draft.sections),
        citations=citations,
        history=history,
    )


def assemble_final(draft: AnswerDraft, citations: list[QaCitation]) -> AssembledAnswer:
    """非事故意图的常规装配。"""
    parts = [draft.summary.strip()]
    parts.extend(s.strip() for s in draft.sections if s.strip())
    return AssembledAnswer(
        markdown="\n\n".join(p for p in parts if p),
        sections=list(draft.sections),
        citations=citations,
    )


