"""专家盲标评测纯逻辑（docs/08 FR-TD-002 AC1/AC2，docs/06 §3.2 指标定义）。

盲标纪律（docs/06 §3.2：专家必须在看到 AI 输出**之前**独立列点，否则被锚定、
覆盖率虚高）：

- 专家清单以 ``ExpertBlindList`` 落盘并先封存（``seal_expert_manifest`` 哈希），
  评测函数要求清单已存在且非空，否则 ``BlindOrderError``——流程上杜绝「先看 AI
  再补专家」；
- 匹配在 hostless 纯函数内完成：同 fr_id 且专家关键词全部命中 AI 文本；严格命中
  失败（含有关键词但近义词改写）或无关键词时退 token Jaccard ≥.5（L3-1，W1 红证），
  可解释、可复现，不依赖嵌入服务；
- 指标：覆盖率（专家必须测点被 AI 覆盖比例）、精确率（AI 点被专家认可比例）、
  漏点（专家有 AI 无）、幻觉点率（白名单拒入行 / 模型产出），P0/P1 漏点与幻觉
  按 AC2 单列；阈值取 docs/06 POC 门禁（覆盖 ≥.75、精确 ≥.8、P0/P1 漏点=0、
  幻觉 ≤.05）。
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Literal

from pydantic import BaseModel, Field

from .testpoint import TestPointGenerationResult, _normalize_text

# docs/06 §3.2 POC 阈值（集中常量，S2.6 五需求总评复用，不散落调用方）
COVERAGE_GATE = 0.75
PRECISION_GATE = 0.8
HALLUCINATION_GATE = 0.05
_JACCARD_GATE = 0.5

Priority = Literal["P0", "P1", "P2", "P3"]


class BlindOrderError(ValueError):
    """专家清单缺失/未封存/文档不匹配：盲标流程顺序被破坏，拒绝评测。"""


class ExpertPoint(BaseModel):
    expert_id: str  # 专家侧稳定编号（E1、E2…），报告漏点按编号点名
    text: str
    fr_id: str
    priority: Priority
    match_keywords: list[str] = Field(default_factory=list)
    must_test: bool = True


class ExpertBlindList(BaseModel):
    doc_external_id: str
    sealed_at: str  # ISO 时间戳，封存动作发生在 AI 生成之前（流程证据）
    expert: str = "blind-expert"
    points: list[ExpertPoint] = Field(default_factory=list)


class GateVerdict(BaseModel):
    coverage_ok: bool
    precision_ok: bool
    p0p1_miss_zero: bool
    hallucination_ok: bool

    @property
    def passed(self) -> bool:
        return (
            self.coverage_ok and self.precision_ok
            and self.p0p1_miss_zero and self.hallucination_ok
        )


class BlindEvalReport(BaseModel):
    doc_external_id: str
    expert_seal_hash: str
    generation_source: str
    expert_total: int
    expert_must_test: int
    expert_covered: int
    coverage: float
    ai_total: int
    ai_recognized: int
    precision: float
    # L3-2：分类型精确率视图。冻结字段 precision 的口径（recognized/ai_total）不变；
    # 新增按 kind 分列，解决 AC 点按链接 FR 文本倍增稀释整体 precision 的读数问题
    # （W1 红证：整体 0.5294，functional 口径 0.7143 需手工另算）。
    ai_total_by_kind: dict[str, int] = Field(default_factory=dict)
    precision_by_kind: dict[str, float] = Field(default_factory=dict)
    missed: list[ExpertPoint] = Field(default_factory=list)
    p0p1_misses: list[ExpertPoint] = Field(default_factory=list)
    hallucinations: list[dict] = Field(default_factory=list)
    hallucination_rate: float
    gate: GateVerdict


def seal_expert_manifest(manifest: ExpertBlindList) -> str:
    """封存哈希：规范 JSON 后 sha256 截 16 位，进报告证明评测基于哪版专家清单。"""
    canonical = json.dumps(
        manifest.model_dump(), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _tokens(text: str) -> set[str]:
    """CJK 字符二元组 + ASCII 整词：无三方依赖的确定性 token 集。"""
    normalized = _normalize_text(text)
    ascii_words = set(re.findall(r"[a-z0-9_]+", normalized))
    cjk = "".join(re.findall(r"[一-鿿]", normalized))
    bigrams = {cjk[i : i + 2] for i in range(len(cjk) - 1)}
    return ascii_words | bigrams


def _jaccard_match(expert_text: str, ai_text: str) -> bool:
    """token Jaccard 兜底：专家文本 token 集在 AI 文本中的命中比例 ≥ 阈值。"""
    expert_tokens = _tokens(expert_text)
    if not expert_tokens:
        return False
    overlap = len(expert_tokens & _tokens(ai_text))
    return overlap / len(expert_tokens) >= _JACCARD_GATE


def _keyword_match(expert: ExpertPoint, ai_text: str) -> bool:
    normalized_ai = _normalize_text(ai_text)
    if expert.match_keywords:
        # L3-1（W1 红证 E4/E8/E9）：严格全关键词命中失败后，再用专家原句全文
        # token 做 Jaccard 兜底，吸收「停铃/取消补发」「3 道 5 道/题数难度」
        # 「换一道新题/答错重答换题」这类近义词改写。严格命中仍是第一口径，
        # 兜底阈值复用 _JACCARD_GATE，不会放宽无关键词专家点的既有判定。
        if all(
            _normalize_text(kw) in normalized_ai for kw in expert.match_keywords
        ):
            return True
        return _jaccard_match(expert.text, ai_text)
    return _jaccard_match(expert.text, ai_text)


def _is_match(expert: ExpertPoint, point: dict) -> bool:
    if expert.fr_id != point.get("fr_id", ""):
        return False
    return _keyword_match(expert, point.get("text", ""))


def evaluate_blind(
    generation: TestPointGenerationResult, manifest: ExpertBlindList
) -> BlindEvalReport:
    """对一份生成结果跑盲标评测；专家清单必须先封存且与文档对齐。"""
    if not manifest.points:
        raise BlindOrderError(
            "专家盲标清单为空：必须先独立列点并封存，再揭示 AI 清单（docs/06 §3.2）"
        )
    if manifest.doc_external_id != generation.external_id:
        raise BlindOrderError(
            f"专家清单归属 {manifest.doc_external_id} 与生成结果 "
            f"{generation.external_id} 不一致，禁止跨文档评测"
        )

    seal_hash = seal_expert_manifest(manifest)
    ai_dicts = [p.model_dump() for p in generation.points]

    matched_expert: set[str] = set()
    recognized_ai: set[int] = set()
    for expert in manifest.points:
        for idx, point in enumerate(ai_dicts):
            if _is_match(expert, point):
                matched_expert.add(expert.expert_id)
                recognized_ai.add(idx)

    must_points = [e for e in manifest.points if e.must_test]
    missed = [e for e in must_points if e.expert_id not in matched_expert]
    p0p1_misses = [e for e in missed if e.priority in ("P0", "P1")]

    expert_must = len(must_points)
    covered = expert_must - len(missed)
    ai_total = len(ai_dicts)
    coverage = round(covered / expert_must, 6) if expert_must else 0.0
    precision = round(len(recognized_ai) / ai_total, 6) if ai_total else 0.0

    # L3-2：分类型精确率。冻结字段 precision 口径不变；这里只把同一批
    # recognized 集合按点 kind 拆开，回答「functional 点本身准不准」，
    # 避免 AC 点按链接 FR 文本倍增后稀释整体读数。
    ai_total_by_kind: dict[str, int] = {}
    recognized_by_kind: dict[str, int] = {}
    for idx, point in enumerate(ai_dicts):
        kind = str(point.get("kind", "")) or "unknown"
        ai_total_by_kind[kind] = ai_total_by_kind.get(kind, 0) + 1
        if idx in recognized_ai:
            recognized_by_kind[kind] = recognized_by_kind.get(kind, 0) + 1
    precision_by_kind = {
        kind: round(recognized_by_kind.get(kind, 0) / total, 6)
        for kind, total in sorted(ai_total_by_kind.items())
    }

    gate = GateVerdict(
        coverage_ok=coverage >= COVERAGE_GATE,
        precision_ok=precision >= PRECISION_GATE,
        p0p1_miss_zero=not p0p1_misses,
        hallucination_ok=generation.hallucination_rate <= HALLUCINATION_GATE,
    )
    return BlindEvalReport(
        doc_external_id=generation.external_id,
        expert_seal_hash=seal_hash,
        generation_source=generation.generation_source,
        expert_total=len(manifest.points),
        expert_must_test=expert_must,
        expert_covered=covered,
        coverage=coverage,
        ai_total=ai_total,
        ai_recognized=len(recognized_ai),
        precision=precision,
        ai_total_by_kind=ai_total_by_kind,
        precision_by_kind=precision_by_kind,
        missed=missed,
        p0p1_misses=p0p1_misses,
        hallucinations=[r.model_dump() for r in generation.rejected],
        hallucination_rate=generation.hallucination_rate,
        gate=gate,
    )
