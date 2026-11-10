"""用例↔需求/符号关系派生纯逻辑（docs/08 FR-ING-004，S2.6b）。

零 IO、零三方：从既有 S2 产物（用例 FR tags、oracle 草稿 source_refs、测试点
source_refs/fr_id）派生**闭集两类关系**，不引入任何新抽取：

- ``case_validates_requirement``：用例验证某需求条目（FR/AC/NFR）；
- ``case_references_symbol``：用例引用某代码符号。

纪律与 S2.1~S2.6a 一致——worker 负责取数与落账，本模块只做确定性派生：

- 关系 URN 内容寻址（source|kind|target 的 sha1），同输入重跑 URN 稳定，
  整组替换幂等；
- 同源同目标关系合并：证据 URN 并集、置信度取 max、来源标签并集；
- 低置信关系（< ``LOW_CONFIDENCE_THRESHOLD``）默认不面向用户展示，仅作检索
  降权素材（FR-ING-004：低置信不面向用户）；
- 证据 dossier 在读取侧组装，无法解析的证据 URN 标 ``unresolved``，
  绝不编造原文（fail-closed）。

派生路径与置信度（全部硬编码，模型不可自标高置信）：

| 路径 | 关系 | 置信 | 面向用户 |
|---|---|---|---|
| 用例 FR tag → 骨架功能点 URN | validates_requirement | 0.90 | 是 |
| oracle 草稿（确定性源）引用需求 URN | validates_requirement | 0.80 | 是 |
| 测试点影响面反向命中用例且带 fr_id | validates_requirement | 0.60 | 是 |
| oracle 草稿（模型源）引用需求 URN | validates_requirement | 0.55 | 否 |
| oracle 草稿（确定性源）引用符号 URN | references_symbol | 0.70 | 是 |
| 测试点 FR 邻接传递到符号 URN | references_symbol | 0.50 | 否 |
| oracle 草稿（模型源）引用符号 URN | references_symbol | 0.50 | 否 |
"""

from __future__ import annotations

import hashlib
from typing import Literal, Sequence

from pydantic import BaseModel, Field

from .models import TestCase
from .urn import parse_urn

RelationKind = Literal["case_validates_requirement", "case_references_symbol"]
ProvenanceTag = Literal[
    "fr_tag", "oracle_suggestion", "test_point_impact", "test_point_fr"
]
EvidenceStatus = Literal["resolved", "unresolved"]

# 关系独立 scope：写入 relations.doc_urn 列作命名空间，与摄入管线的真实文档 URN
# 物理隔离，整组替换只删本 scope，绝不误伤摄入的 calls 等关系。
RELATION_SCOPE_URN = "urn:testpilot:case-relations:scope:main"
_SCOPE_PREFIX = "urn:testpilot:case-relations:"

# 低于该阈值的关系默认藏起（FR-ING-004：低置信不面向用户展示，仅检索降权）。
LOW_CONFIDENCE_THRESHOLD = 0.60

_CONF_FR_TAG = 0.90
_CONF_SUGGESTION_REQ_DETERMINISTIC = 0.80
_CONF_SUGGESTION_REQ_MODEL = 0.55
_CONF_POINT_IMPACT_REQ = 0.60
_CONF_SUGGESTION_SYMBOL_DETERMINISTIC = 0.70
_CONF_LOW = 0.50

_REQUIREMENT_RESOURCE_TYPES = frozenset({"requirement", "requirement_rule"})
_SYMBOL_RESOURCE_TYPE = "symbol"


class CaseRelationError(ValueError):
    """关系派生输入非法（空用例、坏 scope、坏 URN 等），HTTP 层映射 422。"""


class CaseRelation(BaseModel):
    """一条用例关系；结构对齐 core.models.Relation，额外带来源标签与低置信视图。"""

    urn: str
    source_urn: str
    target_urn: str
    kind: RelationKind
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_urns: list[str] = Field(default_factory=list)
    provenance: list[ProvenanceTag] = Field(default_factory=list)

    @property
    def is_low_confidence(self) -> bool:
        return self.confidence < LOW_CONFIDENCE_THRESHOLD


class SuggestionRef(BaseModel):
    """oracle 草稿的关系派生输入（worker 从 oracle_suggestions 行投影）。"""

    case_urn: str
    urn: str
    source_refs: list[str] = Field(default_factory=list)
    source_kind: str = ""
    provenance: Literal["deterministic", "model"] = "deterministic"


class PointRef(BaseModel):
    """测试点的关系派生输入（point URN 由 worker 经 point_urn 内容寻址补齐）。"""

    urn: str
    fr_id: str = ""
    source_refs: list[str] = Field(default_factory=list)


class CaseRelationBuildResult(BaseModel):
    relations: list[CaseRelation] = Field(default_factory=list)
    cases_seen: int = 0
    cases_cancelled: int = 0
    # 同一 fr_id 在多份骨架中重复出现：裸 tag 消歧不了，fail-closed 跳过不猜
    ambiguous_fr_tags: list[str] = Field(default_factory=list)
    # tag 指向的 fr_id 在任何骨架里都不存在（可能语料未摄入/已取消）
    unknown_fr_tags: list[str] = Field(default_factory=list)
    # 不符合 urn:testpilot 约定的引用，跳过并记账
    unparseable_refs: list[str] = Field(default_factory=list)

    @property
    def low_confidence_count(self) -> int:
        return sum(1 for r in self.relations if r.is_low_confidence)


class EvidenceRow(BaseModel):
    """证据 URN 的可读解析结果（store 组装，core 只定义形状与 dossier 拼装）。"""

    urn: str
    status: EvidenceStatus
    kind: str = ""
    label: str = ""
    excerpt: str = ""
    doc_uri: str = ""


class EvidenceDossier(BaseModel):
    """单条关系的证据档案；complete=False 表示存在无法解析的证据 URN。"""

    relation_urn: str
    complete: bool
    items: list[EvidenceRow] = Field(default_factory=list)


def case_relation_urn(source_urn: str, kind: str, target_urn: str) -> str:
    """关系 URN 内容寻址：同 source/kind/target 三元组恒等 URN（重跑幂等基础）。"""
    digest = hashlib.sha1(
        f"{source_urn}|{kind}|{target_urn}".encode("utf-8")
    ).hexdigest()[:12]
    return f"urn:testpilot:case-relations:rel:{digest}"


def ensure_relation_scope(scope_urn: str) -> None:
    """scope 守卫：只允许本模块命名空间，防止整组替换误伤摄入管线关系。"""
    if not scope_urn.startswith(_SCOPE_PREFIX):
        raise CaseRelationError(
            f"非法关系 scope（必须以 {_SCOPE_PREFIX} 开头）: {scope_urn!r}"
        )


def classify_resource(urn: str) -> str | None:
    """取 URN 资源类型；非 testpilot URN 返回 None（调用方记 unparseable 跳过）。"""
    try:
        _, resource_type, _, _ = parse_urn(urn)
    except ValueError:
        return None
    return resource_type


def build_fr_index(
    skeletons: Sequence[tuple[str, Sequence[tuple[str, str]]]],
) -> tuple[dict[str, str], list[str]]:
    """从多份骨架的 (fr_id, feature_urn) 序列构建裸 FR-ID → 功能点 URN 索引。

    同一 fr_id 在多份骨架重复 → 歧义，不进索引并记账（用例的裸 tag 无法可靠
    消歧，fail-closed 不猜目标）。
    """
    index: dict[str, str] = {}
    collisions: set[str] = set()
    for _external_id, features in skeletons:
        for fr_id, feature_urn in features:
            if fr_id in index and index[fr_id] != feature_urn:
                collisions.add(fr_id)
                index.pop(fr_id, None)
            elif fr_id not in collisions:
                index.setdefault(fr_id, feature_urn)
    return index, sorted(collisions)


class _Accumulator:
    """按 (source, kind, target) 合并：证据并集、置信 max、来源并集。"""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str, str], CaseRelation] = {}

    def add(
        self,
        *,
        source_urn: str,
        kind: RelationKind,
        target_urn: str,
        confidence: float,
        evidence: Sequence[str],
        provenance: ProvenanceTag,
    ) -> None:
        key = (source_urn, kind, target_urn)
        existing = self._rows.get(key)
        if existing is None:
            self._rows[key] = CaseRelation(
                urn=case_relation_urn(source_urn, kind, target_urn),
                source_urn=source_urn,
                target_urn=target_urn,
                kind=kind,
                confidence=confidence,
                evidence_urns=sorted({e for e in evidence if e}),
                provenance=[provenance],
            )
            return
        existing.confidence = max(existing.confidence, confidence)
        existing.evidence_urns = sorted(set(existing.evidence_urns) | set(evidence))
        if provenance not in existing.provenance:
            existing.provenance.append(provenance)

    def sorted(self) -> list[CaseRelation]:
        return [
            self._rows[key]
            for key in sorted(self._rows, key=lambda k: (k[0], k[1], k[2]))
        ]


def build_case_relations(
    *,
    cases: Sequence[TestCase],
    fr_index: dict[str, str],
    suggestions: Sequence[SuggestionRef],
    points: Sequence[PointRef],
    ambiguous_fr_tags: Sequence[str] = (),
) -> CaseRelationBuildResult:
    """从 S2 既有产物派生用例关系闭集（纯函数，无 IO）。

    取消态用例整体跳过（与 S2.3/S2.4 同口径）；所有非 testpilot URN 引用记
    unparseable 后跳过，绝不基于坏 URN 造关系。``ambiguous_fr_tags`` 由
    ``build_fr_index`` 产出，原样透传到结果诊断。
    """
    acc = _Accumulator()
    unknown: set[str] = set()
    unparseable: set[str] = set()

    active_cases = [c for c in cases if c.lifecycle != "cancelled"]
    cases_cancelled = len(cases) - len(active_cases)
    case_urns = {c.urn for c in active_cases}
    tags_by_case = {
        c.urn: sorted({t for t in c.tags if t.startswith("FR-")})
        for c in active_cases
    }

    # 路径①：用例 FR tag → 骨架功能点（确定性直连，0.90）
    for case in active_cases:
        for tag in tags_by_case[case.urn]:
            feature_urn = fr_index.get(tag)
            if feature_urn is None:
                unknown.add(tag)
                continue
            acc.add(
                source_urn=case.urn,
                kind="case_validates_requirement",
                target_urn=feature_urn,
                confidence=_CONF_FR_TAG,
                evidence=[case.urn],
                provenance="fr_tag",
            )

    # 路径②：oracle 草稿 source_refs → 需求/符号（确定性 0.80/0.70，模型降为低置信）
    suggestions_by_case: dict[str, list[SuggestionRef]] = {}
    for suggestion in suggestions:
        if suggestion.case_urn not in case_urns:
            continue  # 取消/已删除用例的草稿不派生关系
        suggestions_by_case.setdefault(suggestion.case_urn, []).append(suggestion)

    for case_urn, case_suggestions in suggestions_by_case.items():
        for suggestion in case_suggestions:
            for ref in suggestion.source_refs:
                resource_type = classify_resource(ref)
                if resource_type is None:
                    unparseable.add(ref)
                    continue
                if resource_type in _REQUIREMENT_RESOURCE_TYPES:
                    acc.add(
                        source_urn=case_urn,
                        kind="case_validates_requirement",
                        target_urn=ref,
                        confidence=(
                            _CONF_SUGGESTION_REQ_DETERMINISTIC
                            if suggestion.provenance == "deterministic"
                            else _CONF_SUGGESTION_REQ_MODEL
                        ),
                        evidence=[suggestion.urn, ref],
                        provenance="oracle_suggestion",
                    )
                elif resource_type == _SYMBOL_RESOURCE_TYPE:
                    acc.add(
                        source_urn=case_urn,
                        kind="case_references_symbol",
                        target_urn=ref,
                        confidence=(
                            _CONF_SUGGESTION_SYMBOL_DETERMINISTIC
                            if suggestion.provenance == "deterministic"
                            else _CONF_LOW
                        ),
                        evidence=[suggestion.urn, ref],
                        provenance="oracle_suggestion",
                    )
                # 相似用例/文档/切块等引用只作草稿证据，不产生本闭集外的新关系

    # 路径③：测试点影响面反推（point 引用了用例 → 用例验证 point 的 FR，0.60）
    points_by_fr: dict[str, list[PointRef]] = {}
    for point in points:
        if point.fr_id:
            points_by_fr.setdefault(point.fr_id, []).append(point)
        if point.fr_id not in fr_index:
            continue
        for ref in point.source_refs:
            if ref not in case_urns:
                continue
            acc.add(
                source_urn=ref,
                kind="case_validates_requirement",
                target_urn=fr_index[point.fr_id],
                confidence=_CONF_POINT_IMPACT_REQ,
                evidence=[point.urn, ref],
                provenance="test_point_impact",
            )

    # 路径④：FR 邻接传递（用例 tag FR-x ∧ 同 FR 测试点引用符号 → 用例引用符号，0.50）
    for case in active_cases:
        for tag in tags_by_case[case.urn]:
            for point in points_by_fr.get(tag, ()):
                for ref in point.source_refs:
                    if classify_resource(ref) != _SYMBOL_RESOURCE_TYPE:
                        continue
                    acc.add(
                        source_urn=case.urn,
                        kind="case_references_symbol",
                        target_urn=ref,
                        confidence=_CONF_LOW,
                        evidence=[point.urn, ref],
                        provenance="test_point_fr",
                    )

    return CaseRelationBuildResult(
        relations=acc.sorted(),
        cases_seen=len(cases),
        cases_cancelled=cases_cancelled,
        ambiguous_fr_tags=sorted(ambiguous_fr_tags),
        unknown_fr_tags=sorted(unknown),
        unparseable_refs=sorted(unparseable),
    )


def assemble_evidence_dossier(
    relation: CaseRelation, resolved: Sequence[EvidenceRow]
) -> EvidenceDossier:
    """证据 URN + 已解析行 → dossier；缺失 URN 标 unresolved（fail-closed 不编造）。"""
    by_urn = {row.urn: row for row in resolved if row.status == "resolved"}
    items: list[EvidenceRow] = []
    for urn in relation.evidence_urns:
        if urn in by_urn:
            items.append(by_urn[urn])
        else:
            items.append(EvidenceRow(urn=urn, status="unresolved"))
    return EvidenceDossier(
        relation_urn=relation.urn,
        complete=all(item.status == "resolved" for item in items),
        items=items,
    )
