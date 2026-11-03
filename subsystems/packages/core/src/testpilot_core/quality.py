"""知识条目质量评分（docs/08 FR-ING-003）。

排序公式在检索侧使用：相关性 × quality_score × 新鲜度衰减（S1.2 接入）。
原则：烂数据只降权不删除。每个分项 0..1 可解释，权威等级权重允许人工配置。
纯函数、零 IO、零三方依赖。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .enums import AuthorityLevel

# 权威等级的基线分：官方规范 > 规则资产 > 经验复盘 > 未分级实时源
DEFAULT_AUTHORITY_SCORE: dict[str, float] = {
    AuthorityLevel.L1_AUTHORITATIVE: 1.0,
    AuthorityLevel.L3_RULE: 0.9,
    AuthorityLevel.L2_EXPERIENCE: 0.6,
    AuthorityLevel.L0_REALTIME: 0.5,
}

DEFAULT_WEIGHTS: dict[str, float] = {
    "structure": 0.35,
    "evidence": 0.25,
    "completeness": 0.25,
    "authority": 0.15,
}

_LINK_RE = re.compile(r"\[[^\]]+\]\(([^)]+)\)|https?://\S+")
_BACKTICK_RE = re.compile(r"`[^`\n]+`")
_EVIDENCE_KEYWORD_RE = re.compile(r"验证于|根因|复盘|事故|版本|commit|x?cuitest|日志", re.IGNORECASE)
_LIST_RE = re.compile(r"(?m)^\s*(?:[-*]|\d+[.)])\s+\S")
_CODE_FENCE_RE = re.compile(r"```")
_TABLE_RE = re.compile(r"(?m)^\s*\|.*\|\s*$")
_SUBHEADING_RE = re.compile(r"(?m)^#{2,6}\s+\S")


@dataclass(frozen=True)
class QualityScore:
    score: float
    components: dict[str, float]
    weights: dict[str, float]
    reasons: list[str]


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, round(x, 4)))


def _blend(parts: list[tuple[float, float]]) -> float:
    """parts: (子项分, 子项权重)，权重和归一为 1。"""
    total_w = sum(w for _, w in parts) or 1.0
    return _clamp(sum(s * w for s, w in parts) / total_w)


def score_text(
    text: str,
    *,
    heading_path: tuple[str, ...] = (),
    authority_level: str = AuthorityLevel.L2_EXPERIENCE,
    weights: dict[str, float] | None = None,
    authority_score: dict[str, float] | None = None,
) -> QualityScore:
    weights = weights or DEFAULT_WEIGHTS
    authority_score = authority_score or DEFAULT_AUTHORITY_SCORE
    reasons: list[str] = []

    # 结构化：标题层级、列表、代码块、表格
    structure_parts = [
        (1.0 if heading_path else 0.3, 0.25),
        (_clamp(len(heading_path) / 3), 0.15),
        (1.0 if _LIST_RE.search(text) else 0.0, 0.2),
        (1.0 if _CODE_FENCE_RE.search(text) else 0.0, 0.2),
        (1.0 if _TABLE_RE.search(text) else 0.0, 0.1),
        (1.0 if len(text) >= 120 else 0.4, 0.1),
    ]
    structure = _blend(structure_parts)
    if heading_path:
        reasons.append(f"标题层级 {'/'.join(heading_path)}")
    if not _LIST_RE.search(text) and not _CODE_FENCE_RE.search(text):
        reasons.append("缺少列表/代码块，结构化弱")

    # 证据链接：外链、行内代码引用、证据关键词
    links = len(_LINK_RE.findall(text))
    backticks = len(_BACKTICK_RE.findall(text))
    keywords = len(_EVIDENCE_KEYWORD_RE.findall(text))
    evidence = _blend([
        (_clamp(links / 2), 0.4),
        (_clamp(backticks / 3), 0.3),
        (_clamp(keywords / 3), 0.3),
    ])
    if links:
        reasons.append(f"外链/引用 {links} 处")
    if backticks:
        reasons.append(f"行内代码引用 {backticks} 处")
    if links == 0 and backticks == 0:
        reasons.append("无外链或代码引用，证据弱")

    # 完整度：长度饱和 + 段落/子标题覆盖
    length_score = _clamp(len(text) / 500)
    paragraphs = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
    completeness = _blend([
        (length_score, 0.5),
        (_clamp(len(paragraphs) / 3), 0.25),
        (_clamp(len(_SUBHEADING_RE.findall(text)) / 2), 0.25),
    ])
    reasons.append(f"正文 {len(text)} 字/{len(paragraphs)} 段")

    authority = _clamp(authority_score.get(authority_level, 0.5))
    reasons.append(f"权威等级 {authority_level} → {authority}")

    components = {
        "structure": _clamp(structure),
        "evidence": _clamp(evidence),
        "completeness": _clamp(completeness),
        "authority": authority,
    }
    total_w = sum(weights[k] for k in components)
    score = _clamp(sum(components[k] * weights[k] for k in components) / total_w)
    return QualityScore(score=score, components=components, weights=dict(weights), reasons=reasons)


def score_code_symbol(
    text: str,
    *,
    signature: str = "",
    has_doc_comment: bool = False,
    call_count: int = 0,
    authority_level: str = AuthorityLevel.L1_AUTHORITATIVE,
    weights: dict[str, float] | None = None,
    authority_score: dict[str, float] | None = None,
) -> QualityScore:
    """代码符号块：结构=签名/函数体/doc comment/调用关系；证据=doc comment+调用。"""
    weights = weights or DEFAULT_WEIGHTS
    authority_score = authority_score or DEFAULT_AUTHORITY_SCORE
    reasons: list[str] = []

    body_lines = [ln for ln in text.splitlines() if ln.strip()]
    has_body = len(text.splitlines()) > 1
    structure = _blend([
        (1.0 if signature else 0.2, 0.3),
        (1.0 if has_body else 0.2, 0.2),
        (1.0 if has_doc_comment else 0.0, 0.3),
        (_clamp(call_count / 3), 0.2),
    ])
    if has_doc_comment:
        reasons.append("带 doc comment")
    else:
        reasons.append("无 doc comment")

    evidence = _blend([
        (1.0 if has_doc_comment else 0.0, 0.5),
        (_clamp(call_count / 3), 0.5),
    ])
    if call_count:
        reasons.append(f"同文件调用 {call_count} 条")

    # 完整度：函数体行数在 5–60 行之间最饱和；过短可能是桩、过长扣分另由 S2 设计域处理
    body_len = max(0, len(body_lines) - (1 if has_doc_comment else 0))
    completeness = _clamp(body_len / 25)
    reasons.append(f"函数体约 {body_len} 行")

    authority = _clamp(authority_score.get(authority_level, 0.5))
    reasons.append(f"权威等级 {authority_level} → {authority}")

    components = {
        "structure": _clamp(structure),
        "evidence": _clamp(evidence),
        "completeness": _clamp(completeness),
        "authority": authority,
    }
    total_w = sum(weights[k] for k in components)
    score = _clamp(sum(components[k] * weights[k] for k in components) / total_w)
    return QualityScore(score=score, components=components, weights=dict(weights), reasons=reasons)
