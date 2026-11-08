"""E5 语义裁判（docs/06 §6/§7，FR-EV-003）：LLM-as-judge 的 hostless 纯逻辑。

模型调用在 workers/adapters 适配层（裁判模型 deepseek-r1:7b 必须与生成模型
qwen2.5:7b 不同体，禁止同体自评，docs/06 §6.3）；本模块零 IO 零三方，import-lint 守门。
只负责：
- 结构化裁判信封的 fail-closed 解析（畸形/缺字段 → parse_error，两维都不放行，
  且不抛穿——G22 事故同款纪律，单题问题不能炸掉整批评测）；
- 忠实度按 docs/06 的「被支撑陈述占比」陈述级口径；
- 相关性逐题布尔判定聚合；
- 人工抽检的 Cohen's kappa（无三方依赖，边际退化时返回 None 而非造数）。
"""

from __future__ import annotations

from dataclasses import dataclass

# docs/06 §7 门禁：忠实度 ≥.9（S1 四门禁之一）、答案相关性 ≥.85
FAITHFULNESS_GATE = 0.9
RELEVANCE_GATE = 0.85
# 逐题全支撑才计入忠实度通过（聚合另有陈述级微平均作为主指标）
CASE_FAITHFULNESS_TOLERANCE = 1.0


@dataclass(frozen=True)
class JudgeClaim:
    """裁判模型拆出的单条答案陈述及其证据核验结果。"""

    claim: str
    supported: bool
    evidence_urn: str
    reason: str


@dataclass(frozen=True)
class SemanticVerdict:
    case_id: str
    faithfulness_passed: bool
    relevance_passed: bool
    claims: tuple[JudgeClaim, ...]
    unsupported_claims: tuple[JudgeClaim, ...]
    faithfulness_ratio: float
    relevance_reason: str
    parse_error: str = ""  # 非空表示裁判信封畸形，该题语义判定 fail-closed 记 0

    @property
    def judged_ok(self) -> bool:
        return self.parse_error == ""


@dataclass(frozen=True)
class Adjudication:
    """专家人工裁定（盲标/抽检数据结构；采集在适配层，判定在本模块）。"""

    case_id: str
    faithfulness_passed: bool
    relevance_passed: bool


def _fail(case_id: str, error: str) -> SemanticVerdict:
    return SemanticVerdict(
        case_id=case_id, faithfulness_passed=False, relevance_passed=False,
        claims=(), unsupported_claims=(), faithfulness_ratio=0.0,
        relevance_reason="", parse_error=error,
    )


def failed_semantic_judgement(case_id: str, error: str) -> SemanticVerdict:
    """适配层传输异常/结构化降级的 fail-closed 构造（保留错误文案供报告分诊）。"""
    return _fail(case_id, error[:300])


def parse_semantic_judgement(case_id: str, payload: object) -> SemanticVerdict:
    """解析裁判模型的结构化输出；任何形状问题 fail-closed，绝不放行。"""
    if not isinstance(payload, dict):
        return _fail(case_id, "裁判输出不是 JSON 对象")

    raw_claims = payload.get("claims")
    raw_relevance = payload.get("relevance")
    if not isinstance(raw_claims, list) or not raw_claims:
        return _fail(case_id, "claims 缺失或为空（已回答题必须拆陈述）")
    if not isinstance(raw_relevance, dict) or not isinstance(
        raw_relevance.get("passed"), bool
    ):
        return _fail(case_id, "relevance.passed 缺失或非布尔")

    claims: list[JudgeClaim] = []
    for item in raw_claims:
        if not isinstance(item, dict):
            return _fail(case_id, "claims 元素不是对象")
        claim_text = item.get("claim")
        supported = item.get("supported")
        urn = item.get("evidence_urn", "")
        reason = item.get("reason", "")
        if not isinstance(claim_text, str) or not claim_text.strip():
            return _fail(case_id, "claim 文本缺失")
        if not isinstance(supported, bool):
            return _fail(case_id, "claim.supported 缺失或非布尔")
        if not isinstance(urn, str) or not isinstance(reason, str):
            return _fail(case_id, "evidence_urn/reason 类型非法")
        # 声称被支撑却给不出证据 URN：无法核验，按不支撑处理（fail-closed）
        effective_supported = supported and bool(urn.strip())
        claims.append(JudgeClaim(
            claim=claim_text.strip(), supported=effective_supported,
            evidence_urn=urn.strip(), reason=reason.strip(),
        ))

    unsupported = tuple(c for c in claims if not c.supported)
    ratio = sum(1 for c in claims if c.supported) / len(claims)
    return SemanticVerdict(
        case_id=case_id,
        faithfulness_passed=ratio >= CASE_FAITHFULNESS_TOLERANCE,
        relevance_passed=bool(raw_relevance["passed"]),
        claims=tuple(claims),
        unsupported_claims=unsupported,
        faithfulness_ratio=ratio,
        relevance_reason=str(raw_relevance.get("reason", "")).strip(),
    )


def aggregate_semantic(verdicts: list[SemanticVerdict]) -> dict[str, float]:
    """聚合语义裁判结果；空输入返回 {}（无分母不造数，FR-EV-003 AC2）。"""
    if not verdicts:
        return {}
    total_claims = sum(len(v.claims) for v in verdicts)
    supported_claims = sum(
        sum(1 for c in v.claims if c.supported) for v in verdicts
    )
    return {
        "cases_judged": float(len(verdicts)),
        # docs/06 主口径：全部陈述中被证据支撑的占比（微平均，避免短答案题等权稀释）
        "faithfulness": supported_claims / total_claims if total_claims else 0.0,
        "faithfulness_case_pass_rate": sum(
            1 for v in verdicts if v.faithfulness_passed) / len(verdicts),
        "answer_relevance": sum(
            1 for v in verdicts if v.relevance_passed) / len(verdicts),
        "parse_error_count": float(
            sum(1 for v in verdicts if not v.judged_ok)),
        "faithfulness_gate": float(FAITHFULNESS_GATE),
        "answer_relevance_gate": float(RELEVANCE_GATE),
    }


def cohen_kappa(judge: list[bool], expert: list[bool]) -> float | None:
    """Cohen's kappa（人工抽检一致性）；边际退化致分母为 0 返回 None，不造数。"""
    if len(judge) != len(expert) or not judge:
        raise ValueError("裁判与专家标签长度必须一致且非空")
    n = len(judge)
    po = sum(1 for a, b in zip(judge, expert) if a == b) / n
    p_judge = sum(judge) / n
    p_expert = sum(expert) / n
    pe = p_judge * p_expert + (1 - p_judge) * (1 - p_expert)
    if pe == 1.0:  # 两边都没有方差（全 True/全 False），kappa 无定义
        return None
    return (po - pe) / (1 - pe)
