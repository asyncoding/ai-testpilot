"""oracle 四源挖掘纯逻辑（docs/08 FR-TD-003，S2.4）。

零 IO、零三方：吃 S2.1 的 ``TestCase``、S2.2 的 ``RequirementParseResult`` 与
worker 检索装配的影响面证据，产出每条都能追溯来源的 oracle 草稿。纪律与
S2.1~S2.3 一致——**确定性脊柱先挖，模型只在白名单内补 oracle_missing 用例**：

- 确定性三源（无模型也能跑）：
  1. 需求验收标准（requirement_ac）：用例 FR tag 命中骨架 AC 的 linked_fr_ids，
     分类复用 S2.1 ``classify_oracle``，需求是权威源，置信度 0.7；
  2. 现有断言（existing_assertion）：用例自身 S2.1 oracle 原样保留；相似活跃
     用例的 oracle 平移**只对 oracle_missing 用例开放**（2026-11-14 R2 收口，
     红证 test_related_transfer_gated_on_oracle_missing），平移项强制追加来源
     用例 URN 到 source_refs（R3），置信度 0.5；
  3. 历史缺陷（incident）：只抽事故文本中含义务标记（必须/不得/严禁/确保/应该）
     的句子，拿不准的叙述句不升级为断言，置信度 0.5。
- 当前代码（code_symbol）**不在确定性侧编造 expectation 文本**——符号只给锚点，
  断言文本由模型在白名单内生成或人工补；无中生有的 expectation 即幻觉。
- 模型仅对 ``oracle_missing`` 用例调用（预算守卫，docs/07 §5.4），行级白名单：
  type 必须是四类枚举、source_refs 必须是「需求 URN ∪ 证据 URN ∪ 相似用例 URN」
  子集；违规逐行进 ``RejectedOracle``（worker 落 dead_letters，source=oracle-mine），
  整包形状错抛 ``OracleMiningValidationError`` 由 worker 降级；模型置信度统一 0.5，
  HITL confirmed 后 +0.3 是 S2.5 的事，模型自己不能标高置信。
- AC3：挖完仍无任何可执行 oracle（ui/log/state 三类）→ ``exploratory=True``，
  该用例不进自动化执行链路；manual 不是可执行 oracle。
"""

from __future__ import annotations

import hashlib
import re
from typing import Literal

from pydantic import BaseModel, Field

from .enums import OracleType
from .models import TestCase
from .requirement import RequirementParseResult
from .testcase import classify_oracle
from .testpoint import ImpactEvidence, _normalize_text, acceptance_urn

SourceKind = Literal[
    "requirement_ac", "existing_assertion", "incident",
    "code_symbol", "model_proposed",
]
Provenance = Literal["deterministic", "model"]
GenerationSource = Literal["deterministic", "model", "degraded"]

# 需求是 L1 权威源，置信度高于文档/事故叙述；模型与文档解析同档 0.5，
# 等 S2.5 人审 confirmed 再 +0.3（docs/08 FR-TD-004）。
_AC_CONFIDENCE = 0.7
_DOC_CONFIDENCE = 0.5
_MODEL_CONFIDENCE = 0.5
# 单条事故证据最多抽几条义务句，防一篇复盘把用例 oracle 列表灌满。
_MAX_ORACLES_PER_INCIDENT = 3
_EXECUTABLE_TYPES = frozenset({
    OracleType.UI_ASSERTION,
    OracleType.LOG_ASSERTION,
    OracleType.STATE_ASSERTION,
})
_OBLIGATION_RE = re.compile(r"(必须|不得|严禁|确保|应该|不应|不可|禁止)")
_SENTENCE_SPLIT_RE = re.compile(r"[。；;\n]+")


class OracleMiningValidationError(ValueError):
    """模型载荷整包形状非法（非 dict / oracles 非 list），调用方按降级处理。"""


class MinedOracle(BaseModel):
    """一条 oracle 草稿：Oracle 四字段 + 来源种类与出处追踪（FR-TD-003 AC2）。"""

    type: OracleType
    expectation: str
    source_refs: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    source_kind: SourceKind
    provenance: Provenance = "deterministic"


class RejectedOracle(BaseModel):
    """幻觉台账：被白名单拒绝的模型行，原样留痕供评测报告与 UI 查看。"""

    expectation: str
    type: str
    source_refs: list[str]
    reason: Literal["unknown_source", "bad_type", "empty_text", "bad_shape"]
    detail: str = ""


class OracleMiningResult(BaseModel):
    case_urn: str
    external_id: str
    mined: list[MinedOracle] = Field(default_factory=list)
    rejected: list[RejectedOracle] = Field(default_factory=list)
    deduped: int = 0
    exploratory: bool = False
    exploratory_reasons: list[str] = Field(default_factory=list)
    generation_source: GenerationSource = "deterministic"
    model_version: str = ""
    evidence_urns: list[str] = Field(default_factory=list)

    @property
    def executable_oracles(self) -> list[MinedOracle]:
        """可自动化断言：UI/日志/状态；manual 不算可执行（AC3 判定口径）。"""
        return [o for o in self.mined if o.type in _EXECUTABLE_TYPES]

    @property
    def model_oracles(self) -> list[MinedOracle]:
        return [o for o in self.mined if o.provenance == "model"]

    @property
    def hallucination_rate(self) -> float:
        """被拒行 /（模型有效产出 + 被拒行）；无模型产出时为 0.0。"""
        rejected = len(self.rejected)
        denominator = len(self.model_oracles) + rejected
        return round(rejected / denominator, 6) if denominator else 0.0


def suggestion_urn(external_id: str, oracle: MinedOracle) -> str:
    """oracle 草稿 URN 内容寻址：同用例同类型同期望文本 → 同 URN（重跑幂等）。"""
    digest = hashlib.sha1(
        f"{oracle.type.value}|{oracle.expectation}".encode("utf-8")
    ).hexdigest()[:10]
    return f"urn:testpilot:fs-cases:oracle_suggestion:{external_id}#{digest}"


# ── 白名单与义务句 ───────────────────────────────────────────────────────────

def build_oracle_whitelist(
    skeleton: RequirementParseResult | None,
    evidence: list[ImpactEvidence],
    related_cases: list[TestCase],
) -> set[str]:
    """模型 source_refs 封闭白名单：需求 FR/NFR/AC URN ∪ 证据 URN ∪ 相似用例 URN。"""
    allowed: set[str] = set()
    if skeleton is not None:
        allowed |= {f.urn for f in skeleton.features}
        allowed |= {r.urn for r in skeleton.rules}
        allowed |= {
            acceptance_urn(skeleton.external_id, ac) for ac in skeleton.acceptance
        }
    allowed |= {e.urn for e in evidence}
    allowed |= {c.urn for c in related_cases}
    return allowed


def _obligation_sentences(text: str) -> list[str]:
    """从事故叙述里只挑义务句：陈述句没有断言效力，抽出来就是编造。"""
    out: list[str] = []
    for raw in _SENTENCE_SPLIT_RE.split(text):
        sentence = raw.strip(" -\t")
        if sentence and _OBLIGATION_RE.search(sentence):
            out.append(sentence)
        if len(out) >= _MAX_ORACLES_PER_INCIDENT:
            break
    return out


# ── 确定性脊柱 ───────────────────────────────────────────────────────────────

def _decide_exploratory(
    case: TestCase, mined: list[MinedOracle], *, model_attempted: bool
) -> tuple[bool, list[str]]:
    """AC3：无任何可执行 oracle → 探索性用例，不进自动化链路。"""
    reasons: list[str] = []
    executable = [o for o in mined if o.type in _EXECUTABLE_TYPES]
    if executable:
        return False, []
    if case.oracle_missing:
        reasons.append("oracle_missing_no_executable_mined")
    if mined and all(o.type == OracleType.MANUAL for o in mined):
        reasons.append("manual_only")
    if not mined:
        reasons.append("no_source_available")
    if model_attempted:
        reasons.append("model_enrichment_failed_to_add_executable")
    return True, reasons


def mine_deterministic(
    case: TestCase,
    skeleton: RequirementParseResult | None,
    evidence: list[ImpactEvidence],
    related_cases: list[TestCase] | None = None,
) -> OracleMiningResult:
    """确定性三源挖掘；代码符号只登记 URN 不造断言。"""
    related_cases = related_cases or []
    mined: list[MinedOracle] = []
    seen: set[str] = set()

    def add(expectation: str, oracle_type: OracleType, refs: list[str],
            confidence: float, kind: SourceKind) -> None:
        normalized = _normalize_text(expectation)
        if not normalized or normalized in seen:
            return
        seen.add(normalized)
        mined.append(MinedOracle(
            type=oracle_type, expectation=expectation, source_refs=refs,
            confidence=confidence, source_kind=kind, provenance="deterministic",
        ))

    # 源②-a：用例自身 S2.1 已解析的断言（文档源，置信度不动）
    for oracle in case.oracles:
        refs = oracle.source_refs or [case.urn]
        add(oracle.expectation, oracle.type, refs,
            oracle.confidence or _DOC_CONFIDENCE, "existing_assertion")

    # 源①：需求验收标准（FR tag ↔ AC 链接的确定性join）
    # L3-6：优先只 join AC 句首标题声明的主级 FR；无标题习惯的文档 primary 为空，
    # 回退主+次并集 linked_fr_ids（旧口径）。正文提及的 FR 不再把无关用例拖进
    # 跨 AC join（W2 红证：FR-10 正文提及导致 W2-AC-4 反向 join 到 AC-6）。
    if skeleton is not None:
        fr_tags = {tag for tag in case.tags if tag.startswith("FR-")}
        if fr_tags:
            by_id = {f.fr_id: f for f in skeleton.active_features}
            for ac in skeleton.acceptance:
                join_ids = ac.primary_fr_ids or ac.linked_fr_ids
                if not fr_tags.intersection(join_ids):
                    continue
                feature = next(
                    (by_id[fid] for fid in ac.linked_fr_ids if fid in fr_tags), None
                )
                ac_urn = acceptance_urn(skeleton.external_id, ac)
                refs = [ac_urn, feature.urn] if feature else [ac_urn]
                add(ac.text, classify_oracle(ac.text), refs,
                    _AC_CONFIDENCE, "requirement_ac")

    # 源②-b：相似活跃用例的现有断言（平移不升置信度）
    # R2（2026-11-14 串案修复）：平移只服务 oracle_missing 用例。已有自身断言的
    # 用例再整组灌入相似用例断言即纯噪声——V1 语料 276 行 existing_assertion 中
    # 257 行错挂（93.1%），主因就是无界平移叠加检索饱和（红证见
    # test_related_transfer_gated_on_oracle_missing）。
    if case.oracle_missing:
        for related in related_cases:
            if related.urn == case.urn:
                continue
            for oracle in related.oracles:
                # R3：S2.1 解析器给 oracle 盖的是文档级 source_refs，平移时必须
                # 追加来源用例 URN（去重保序），否则草稿在审批页与自有断言不可
                # 区分；旧 ``or [related.urn]`` 兜底在生产路径永不触发。
                refs = list(dict.fromkeys([*(oracle.source_refs or []), related.urn]))
                add(oracle.expectation, oracle.type, refs,
                    _DOC_CONFIDENCE, "existing_assertion")

    # 源③：历史缺陷义务句（叙述句不抽，防编造）
    for ev in evidence:
        if ev.kind != "incident":
            continue
        for sentence in _obligation_sentences(ev.text):
            add(sentence, classify_oracle(sentence), [ev.urn],
                _DOC_CONFIDENCE, "incident")

    exploratory, reasons = _decide_exploratory(
        case, mined, model_attempted=False
    )
    return OracleMiningResult(
        case_urn=case.urn,
        external_id=case.external_id,
        mined=mined,
        exploratory=exploratory,
        exploratory_reasons=reasons,
        generation_source="deterministic",
        evidence_urns=sorted({e.urn for e in evidence}),
    )


# ── 模型补点 ─────────────────────────────────────────────────────────────────

def build_oracle_messages(
    case: TestCase,
    skeleton: RequirementParseResult | None,
    evidence: list[ImpactEvidence],
    spine: list[MinedOracle],
) -> list[dict[str, str]]:
    """生成补 oracle prompt：只给白名单 URN 与四类枚举，禁止虚构与重复。"""
    case_lines = [
        f"- 用例 {case.external_id}（tags={', '.join(case.tags) or '无'}）：{case.title}",
        *[f"  步骤：{s}" for s in case.steps],
    ]
    ac_lines: list[str] = []
    if skeleton is not None:
        for ac in skeleton.acceptance:
            ac_lines.append(
                f"- {acceptance_urn(skeleton.external_id, ac)}：{ac.text}"
                f"（链接：{', '.join(ac.linked_fr_ids) or '无'}）"
            )
        for f in skeleton.active_features:
            ac_lines.append(f"- {f.urn}：{f.text}")
    evidence_lines = [
        f"- [{e.kind}] {e.urn}：{(e.title or '') + ' ' + e.text[:160]}".strip()
        for e in evidence
    ]
    spine_lines = [
        f"- ({o.type.value}) {o.expectation}" for o in spine
    ]
    user = (
        "【待补 oracle 的用例】\n" + "\n".join(case_lines) + "\n\n"
        "【需求白名单 URN】\n" + ("\n".join(ac_lines) or "- 无需求骨架，URN 只能取自证据段")
        + "\n\n【影响面证据白名单（source_refs 只能引用这些 URN）】\n"
        + ("\n".join(evidence_lines) or "- 无检索证据")
        + "\n\n【已挖出的确定性 oracle，禁止重复】\n"
        + ("\n".join(spine_lines) or "- 无")
    )
    system = (
        "你是资深测试设计专家。为缺少预期断言的用例补充可执行 oracle。铁律："
        "1) type 只能是 ui_assertion/log_assertion/state_assertion/manual 四者之一；"
        "2) source_refs 必须逐字抄写白名单中的完整 URN，禁止裸 ID、禁止编造 URN；"
        "3) expectation 是一条可判定真假的断言，中文动宾结构，不写步骤；"
        "4) 不要输出 confidence 字段，置信度由系统统一标定；"
        "5) 只能依据白名单材料推断，材料不足以支撑可执行断言时输出 manual 类型。"
    )
    return [{"role": "system", "content": system},
            {"role": "user", "content": user}]


def oracle_json_schema() -> dict:
    """模型结构化输出契约（与 prompt 铁律一一对应）。"""
    return {
        "type": "object",
        "properties": {
            "oracles": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string",
                                 "enum": [t.value for t in OracleType]},
                        "expectation": {"type": "string"},
                        "source_refs": {"type": "array",
                                        "items": {"type": "string"}},
                    },
                    "required": ["type", "expectation", "source_refs"],
                },
            }
        },
        "required": ["oracles"],
    }


def _reject(rejected: list[RejectedOracle], row: dict, reason: str,
            detail: str) -> None:
    rejected.append(RejectedOracle(
        expectation=str(row.get("expectation", ""))[:200],
        type=str(row.get("type", "")),
        source_refs=[str(x) for x in row.get("source_refs", [])]
        if isinstance(row.get("source_refs"), list) else [],
        reason=reason,  # type: ignore[arg-type]
        detail=detail,
    ))


def apply_model_oracles(
    case: TestCase,
    spine_result: OracleMiningResult,
    payload: dict,
    skeleton: RequirementParseResult | None,
    evidence: list[ImpactEvidence],
    related_cases: list[TestCase],
) -> OracleMiningResult:
    """校验并合并模型 oracle；逐行白名单拒入，整包形状错抛异常供调用方降级。"""
    if not isinstance(payload, dict) or not isinstance(payload.get("oracles"), list):
        raise OracleMiningValidationError("载荷不是含 oracles 列表的对象")

    whitelist = build_oracle_whitelist(skeleton, evidence, related_cases)
    allowed_types = {t.value for t in OracleType}
    seen_texts = {_normalize_text(o.expectation) for o in spine_result.mined}
    accepted: list[MinedOracle] = []
    rejected: list[RejectedOracle] = list(spine_result.rejected)
    deduped = spine_result.deduped

    for row in payload["oracles"]:
        if not isinstance(row, dict):
            raise OracleMiningValidationError(
                f"oracles 元素不是对象: {type(row).__name__}")
        otype = row.get("type")
        expectation = row.get("expectation")
        refs = row.get("source_refs")
        if not isinstance(otype, str) or not isinstance(expectation, str) \
                or not isinstance(refs, list):
            _reject(rejected, row, "bad_shape",
                    "type/expectation 须为字符串，source_refs 须为列表")
            continue
        if not expectation.strip():
            _reject(rejected, row, "empty_text", "oracle 期望文本为空")
            continue
        if otype not in allowed_types:
            _reject(rejected, row, "bad_type", f"type 不在四类枚举: {otype}")
            continue
        if any(not isinstance(ref, str) or ref not in whitelist for ref in refs):
            _reject(rejected, row, "unknown_source",
                    "source_refs 存在白名单外 URN（需求/证据/相似用例 URN）")
            continue
        normalized = _normalize_text(expectation)
        if normalized in seen_texts:
            deduped += 1
            continue
        seen_texts.add(normalized)
        accepted.append(MinedOracle(
            type=OracleType(otype),  # type: ignore[arg-type]
            expectation=expectation.strip(),
            source_refs=refs,
            confidence=_MODEL_CONFIDENCE,  # 模型不能自标高置信
            source_kind="model_proposed",
            provenance="model",
        ))

    mined = [*spine_result.mined, *accepted]
    exploratory, reasons = _decide_exploratory(case, mined, model_attempted=True)
    return OracleMiningResult(
        case_urn=case.urn,
        external_id=case.external_id,
        mined=mined,
        rejected=rejected,
        deduped=deduped,
        exploratory=exploratory,
        exploratory_reasons=reasons,
        generation_source="model",
        evidence_urns=spine_result.evidence_urns,
    )
