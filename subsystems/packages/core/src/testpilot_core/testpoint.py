"""测试点生成纯逻辑（docs/08 FR-TD-002，S2.3）。

零 IO、零三方：吃 S2.2 的 ``RequirementParseResult`` +  worker 检索到的影响面证据
（URN 白名单），吐带来源的测试点清单。与 S2.1/S2.2 同一纪律——**确定性脊柱 +
模型白名单补点**：

- 确定性脊柱（无模型也能跑，离线 profile 的地板）：
  每个活跃 FR 一条 functional 点；每条 NFR 一条 rule 点；每条验收标准按其链接的
  FR 产 acceptance 点（无链接则挂 fr_id="" 的通用点）；取消态 FR 不产点。
  优先级强制取 FR 自身优先级（缺省 P2），模型不允许给优先级，防 P0/P1 指标被灌水。
- 模型只补 boundary/negative/model_proposed 三类增量点，且：
  fr_id 必须是活跃 FR 白名单成员；source_refs 必须是「需求 URN ∪ 证据 URN」
  白名单子集；违反者逐行拒入 ``rejected`` 幻觉台账（不持久化、不进正式清单），
  整包形状错误抛 ``TestPointValidationError`` 由调用方降级为纯确定性结果。
- 幻觉点率分母 = 模型产出的有效点 + 被拒点（纯重复行去重不进分母，它从未成为输出）。
"""

from __future__ import annotations

import hashlib
import re
from typing import Literal

from pydantic import BaseModel, Field

from .models import KnowledgeChunk, TestPoint
from .requirement import (
    RequirementAcceptance,
    RequirementFeature,
    RequirementParseResult,
)
from .urn import make_urn

_SOURCE_SYSTEM = "fs-reqs"

# 模型补点允许的封闭词表；functional/rule/acceptance 是确定性脊柱保留类别，
# 模型若伪装脊柱类别直接拒（防重复脊柱点稀释精确率）。
MODEL_POINT_KINDS = ("boundary", "negative", "model_proposed")
_SPINE_KINDS = ("functional", "rule", "acceptance")

PointKind = Literal[
    "functional", "rule", "acceptance", "boundary", "negative", "model_proposed"
]
GenerationSource = Literal["deterministic", "model", "degraded"]
EvidenceKind = Literal["test_case", "incident", "code_symbol"]

# 规则点统一 P1：NFR 是跨功能约束，漏测代价高于普通功能点；该映射是确定性
# 保守选择并在此写死说明，不允许模型改写。
_RULE_POINT_PRIORITY = "P1"
_DEFAULT_PRIORITY = "P2"


class TestPointValidationError(ValueError):
    """模型补点载荷整包形状非法（非 dict / points 非 list），调用方按降级处理。"""


class ImpactEvidence(BaseModel):
    """影响面检索的一条证据（worker 检索后装配，core 只认 URN 白名单与摘要）。"""

    urn: str
    kind: EvidenceKind
    title: str = ""
    text: str = ""
    fr_ids: list[str] = Field(default_factory=list)


class RejectedPoint(BaseModel):
    """幻觉台账：被白名单拒绝的模型行，原样留痕供评测报告与 UI 查看。"""

    text: str
    fr_id: str
    kind: str
    source_refs: list[str]
    reason: Literal[
        "unknown_fr", "unknown_source", "empty_text", "bad_kind", "bad_shape"
    ]
    detail: str = ""


class TestPointGenerationResult(BaseModel):
    doc_urn: str
    external_id: str
    version: str | None = None
    points: list[TestPoint] = Field(default_factory=list)
    spine_count: int = 0
    rejected: list[RejectedPoint] = Field(default_factory=list)
    deduped: int = 0
    generation_source: GenerationSource = "deterministic"
    model_version: str = ""
    evidence_urns: list[str] = Field(default_factory=list)

    @property
    def model_points(self) -> list[TestPoint]:
        return [p for p in self.points if p.provenance == "model"]

    @property
    def hallucination_rate(self) -> float:
        """被拒幻觉行 /（模型有效产出 + 被拒行）；无模型产出时为 0.0。"""
        rejected = len(self.rejected)
        denominator = len(self.model_points) + rejected
        return round(rejected / denominator, 6) if denominator else 0.0


# ── URN 与文本工具 ───────────────────────────────────────────────────────────

def acceptance_urn(external_id: str, ac: RequirementAcceptance) -> str:
    """验收标准 URN：与 FR/RN 同 source_system，序号 1-based 对人类可读。"""
    return make_urn(_SOURCE_SYSTEM, "requirement", f"{external_id}#AC-{ac.index + 1}")


def point_urn(external_id: str, point: TestPoint) -> str:
    """测试点 URN 内容寻址：同文档同键同文本 → 同 URN（重跑幂等的基础）。

    TestPoint 是 S0 冻结模型、没有 urn 字段；URN 由持久化/评测层经本函数派生，
    不污染模型面。
    """
    digest = hashlib.sha1(point.text.encode("utf-8")).hexdigest()[:10]
    key = point.fr_id or point.kind
    return make_urn(_SOURCE_SYSTEM, "test_point", f"{external_id}#{key}-{digest}")


def _normalize_text(text: str) -> str:
    """去空白/标点并小写：重复检测与盲标关键词匹配共用同一口径。"""
    return re.sub(r"[\s\W_]+", "", text, flags=re.UNICODE).lower()


def _priority_of(feature: RequirementFeature) -> str:
    return feature.priority or _DEFAULT_PRIORITY


# ── 确定性脊柱 ───────────────────────────────────────────────────────────────

def deterministic_test_points(result: RequirementParseResult) -> list[TestPoint]:
    """无模型地板：FR 功能点 + NFR 规则点 + AC 验收点，全部回链需求 URN。"""
    points: list[TestPoint] = []
    external_id = result.external_id

    for feature in result.active_features:
        text = f"验证「{feature.text}」主流程可用"
        points.append(TestPoint(
            text=text,
            priority=_priority_of(feature),  # type: ignore[arg-type]
            source_refs=[feature.urn],
            fr_id=feature.fr_id,
            kind="functional",
            provenance="deterministic",
        ))

    for rule in result.rules:
        points.append(TestPoint(
            text=f"约束验证：{rule.text}",
            priority=_RULE_POINT_PRIORITY,  # type: ignore[arg-type]
            source_refs=[rule.urn],
            fr_id="",
            kind="rule",
            provenance="deterministic",
        ))

    for ac in result.acceptance:
        ac_urn = acceptance_urn(external_id, ac)
        if not ac.linked_fr_ids:
            # 验收条目没链接到任何 FR：不编造归属，fr_id 留空但点必须产出
            points.append(TestPoint(
                text=f"验收口径验证：{ac.text}",
                priority=_DEFAULT_PRIORITY,
                source_refs=[ac_urn],
                fr_id="",
                kind="acceptance",
                provenance="deterministic",
            ))
            continue
        by_id = {f.fr_id: f for f in result.active_features}
        for fr_id in ac.linked_fr_ids:
            feature = by_id.get(fr_id)
            points.append(TestPoint(
                text=f"验收口径验证：{ac.text}",
                priority=_priority_of(feature) if feature else _DEFAULT_PRIORITY,  # type: ignore[arg-type]
                source_refs=[ac_urn, feature.urn] if feature else [ac_urn],
                fr_id=fr_id,
                kind="acceptance",
                provenance="deterministic",
            ))

    # TestPoint 是 S0 冻结模型、没有 urn 字段；持久化 URN 由 point_urn() 派生。
    return points


# ── 白名单与影响面 ───────────────────────────────────────────────────────────

def build_source_whitelist(
    result: RequirementParseResult, evidence: list[ImpactEvidence]
) -> set[str]:
    """模型 source_refs 的封闭白名单：需求自身 URN（FR/NFR/AC）∪ 证据 URN。"""
    allowed = {f.urn for f in result.features}
    allowed |= {r.urn for r in result.rules}
    allowed |= {acceptance_urn(result.external_id, ac) for ac in result.acceptance}
    allowed |= {e.urn for e in evidence}
    return allowed


def build_impact_query(result: RequirementParseResult, *, max_features: int = 8,
                       max_chars: int = 400) -> str:
    """把活跃 FR 文本拼成一条检索查询（喂 FTS/混合检索，worker 侧 IO）。"""
    texts = [f.text for f in result.active_features[:max_features]]
    query = " ".join(texts)
    return query[:max_chars]


def partition_impact_chunks(
    chunks: list[KnowledgeChunk],
) -> tuple[list[ImpactEvidence], list[ImpactEvidence]]:
    """检索切块按 kind 分流：code_symbol→符号证据，其余→事故/经验证据。

    test_case/rule/incident 等 kind 是 S0 契约预留；当前切块器实际只产
    text/code_symbol，text 一律按事故/经验证据处理，拿不准不升级为符号。
    """
    incidents: list[ImpactEvidence] = []
    symbols: list[ImpactEvidence] = []
    for chunk in chunks:
        if chunk.kind == "code_symbol":
            symbols.append(ImpactEvidence(
                urn=chunk.urn, kind="code_symbol",
                title=chunk.symbol_urn or "", text=chunk.text,
            ))
        else:
            incidents.append(ImpactEvidence(
                urn=chunk.urn, kind="incident", title=chunk.doc_urn, text=chunk.text,
            ))
    return incidents, symbols


# ── 模型补点 ─────────────────────────────────────────────────────────────────

def build_testpoint_messages(
    result: RequirementParseResult,
    evidence: list[ImpactEvidence],
    spine: list[TestPoint],
) -> list[dict[str, str]]:
    """生成补点 prompt：只给白名单 ID/URN，明令禁止虚构与重复脊柱点。"""
    # 真模型探针（qwen2.5:7b）实证：只给裸 ID 时模型会把 "FR-1" 当 source_refs
    # 回显，12/12 被白名单拒入。白名单 URN 必须逐字出现在 prompt 里供模型抄写，
    # 不能假设它能按约定自行拼出全限定 URN。
    feature_lines = [
        f"- {f.fr_id}（{f.priority or '无优先级'}，模块={f.module or '未分模块'}）"
        f"URN={f.urn}：{f.text}"
        for f in result.active_features
    ]
    rule_lines = [f"- {r.rule_id} URN={r.urn}：{r.text}" for r in result.rules]
    ac_lines = [
        f"- AC-{ac.index + 1} URN={acceptance_urn(result.external_id, ac)}"
        f"：{ac.text}（链接：{', '.join(ac.linked_fr_ids) or '无'}）"
        for ac in result.acceptance
    ]
    evidence_lines = [
        f"- [{e.kind}] {e.urn}：{(e.title or '') + ' ' + e.text[:120]}".strip()
        for e in evidence
    ]
    spine_lines = [f"- ({p.kind}) {p.fr_id or '-'}：{p.text}" for p in spine]
    user = (
        "【功能点白名单（fr_id 只能从这里选）】\n" + "\n".join(feature_lines) + "\n\n"
        "【规则】\n" + ("\n".join(rule_lines) or "- 无") + "\n\n"
        "【验收标准】\n" + ("\n".join(ac_lines) or "- 无") + "\n\n"
        "【影响面证据白名单（source_refs 只能引用这些 URN 与上面的需求 URN）】\n"
        + ("\n".join(evidence_lines) or "- 无检索证据，source_refs 只引用需求 URN") + "\n\n"
        "【已存在的确定性测试点，禁止重复】\n" + "\n".join(spine_lines)
    )
    system = (
        "你是资深测试设计专家。基于给定需求与影响面证据，补充确定性清单遗漏的"
        "边界值、异常/负向、取消态、并发幂等等测试点。铁律："
        "1) fr_id 必须逐字取自功能点白名单，禁止发明 ID；"
        "2) source_refs 必须逐字抄写各行「URN=」后的完整 URN（证据段同理），"
        "禁止只写 FR-1 这类裸 ID，禁止编造 URN；"
        "3) kind 只能是 boundary / negative / model_proposed，禁止输出 "
        "functional/rule/acceptance（脊柱已覆盖）；"
        "4) 不要输出优先级字段，优先级由系统按 FR 强制映射；"
        "5) 一条测试点只验证一个意图，text 用中文动宾结构，不写步骤细节。"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def testpoint_json_schema() -> dict:
    """模型结构化输出契约（与 build_testpoint_messages 的禁令一一对应）。"""
    return {
        "type": "object",
        "properties": {
            "points": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "fr_id": {"type": "string"},
                        "text": {"type": "string"},
                        "kind": {"type": "string", "enum": list(MODEL_POINT_KINDS)},
                        "source_refs": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["fr_id", "text", "kind", "source_refs"],
                },
            }
        },
        "required": ["points"],
    }


def _reject(rejected: list[RejectedPoint], row: dict, reason: str, detail: str) -> None:
    rejected.append(RejectedPoint(
        text=str(row.get("text", ""))[:200],
        fr_id=str(row.get("fr_id", "")),
        kind=str(row.get("kind", "")),
        source_refs=[str(x) for x in row.get("source_refs", [])]
        if isinstance(row.get("source_refs"), list) else [],
        reason=reason,  # type: ignore[arg-type]
        detail=detail,
    ))


def apply_model_points(
    result: RequirementParseResult,
    spine: list[TestPoint],
    payload: dict,
    evidence: list[ImpactEvidence],
) -> TestPointGenerationResult:
    """校验并合并模型补点；逐行白名单拒入，整包形状错抛 TestPointValidationError。

    - 未知 fr_id / 白名单外 source_ref / 空 text / 脊柱保留 kind → 进 rejected；
    - 与脊柱或已接受点规范化重复 → deduped 记账（不是幻觉，不进幻觉率分母）；
    - 优先级强制取所属 FR（模型无法影响 P0/P1 统计）；
    - 输出点序 = 脊柱在前、模型补点在后，稳定可对账。
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("points"), list):
        raise TestPointValidationError("载荷不是含 points 列表的对象")

    features = {f.fr_id: f for f in result.active_features}
    urn_whitelist = build_source_whitelist(result, evidence)
    seen_texts = {_normalize_text(p.text) for p in spine}
    accepted: list[TestPoint] = []
    rejected: list[RejectedPoint] = []
    deduped = 0

    for row in payload["points"]:
        if not isinstance(row, dict):
            raise TestPointValidationError(f"points 元素不是对象: {type(row).__name__}")
        fr_id = row.get("fr_id")
        text = row.get("text")
        kind = row.get("kind")
        refs = row.get("source_refs")
        if not isinstance(fr_id, str) or not isinstance(text, str) \
                or not isinstance(kind, str) or not isinstance(refs, list):
            _reject(rejected, row if isinstance(row, dict) else {}, "bad_shape",
                    "fr_id/text/kind 须为字符串，source_refs 须为列表")
            continue
        if not text.strip():
            _reject(rejected, row, "empty_text", "测试点文本为空")
            continue
        if fr_id not in features:
            _reject(rejected, row, "unknown_fr", f"fr_id 不在活跃功能点白名单: {fr_id}")
            continue
        if kind not in MODEL_POINT_KINDS:
            _reject(rejected, row, "bad_kind",
                    f"kind 仅允许 {MODEL_POINT_KINDS}，模型不得占用脊柱类别")
            continue
        if any(not isinstance(ref, str) or ref not in urn_whitelist for ref in refs):
            _reject(rejected, row, "unknown_source",
                    "source_refs 存在白名单外 URN（需求 FR/NFR/AC 或证据 URN）")
            continue
        normalized = _normalize_text(text)
        if normalized in seen_texts:
            deduped += 1
            continue
        seen_texts.add(normalized)
        feature = features[fr_id]
        accepted.append(TestPoint(
            text=text.strip(),
            priority=_priority_of(feature),
            source_refs=refs,
            fr_id=fr_id,
            kind=kind,  # type: ignore[arg-type]
            provenance="model",
        ))

    return TestPointGenerationResult(
        doc_urn=result.doc_urn,
        external_id=result.external_id,
        version=result.version,
        points=[*spine, *accepted],
        spine_count=len(spine),
        rejected=rejected,
        deduped=deduped,
        generation_source="model",
        evidence_urns=sorted({e.urn for e in evidence}),
    )


def deterministic_result(
    result: RequirementParseResult, evidence: list[ImpactEvidence] | None = None
) -> TestPointGenerationResult:
    """无模型/降级出口：纯脊柱结果，generation_source=deterministic。"""
    evidence = evidence or []
    spine = deterministic_test_points(result)
    return TestPointGenerationResult(
        doc_urn=result.doc_urn,
        external_id=result.external_id,
        version=result.version,
        points=spine,
        spine_count=len(spine),
        generation_source="deterministic",
        evidence_urns=sorted({e.urn for e in evidence}),
    )
