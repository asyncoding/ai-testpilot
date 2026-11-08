"""S1.6 离线评测裁判：golden set 逐题判定与指标聚合（FR-EV-002/003）。

纯函数、零 IO、零三方依赖：runner 负责把 AskResponse/trace 适配成 CaseResult，
本模块只做结构化断言与可复算的指标计算。语义类指标（忠实度等）由 E5 的隔离
裁判模型产出，不在此模块。

错误标签按 FR-EV-002 AC2 二分：
- 召回侧：intent_mismatch / empty_retrieval / recall_miss
- 生成侧：unexpected_unknown / no_citations / citation_forgery /
  wrong_answer_on_negative / strict_reason_mismatch
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .models import EvalCase

# 标签二分（报告里召回侧/生成侧错误分别统计，禁止只报一个总分）
RECALL_SIDE_LABELS = frozenset(
    {"intent_mismatch", "empty_retrieval", "recall_miss"}
)
GENERATION_SIDE_LABELS = frozenset(
    {
        "unexpected_unknown",
        "no_citations",
        "citation_forgery",
        "wrong_answer_on_negative",
        "strict_reason_mismatch",
    }
)


@dataclass(frozen=True)
class CaseResult:
    """单次问答的可裁判投影（runner 从 AskResponse + trace 适配，本结构零 IO）。"""

    status: str  # answered / unknown / denied / error
    intent: str
    evidence_urns: list[str]
    citation_urns: list[str]
    whitelist_urns: list[str]  # 该 run 实际生效的引用白名单（块 URN ∪ 文档 URN）
    unknown_reason: str = ""
    intent_source: str = ""
    degraded: list[str] = field(default_factory=list)
    attempts: int = 0
    latency_ms: int = 0
    tokens_in: int = 0
    tokens_out: int = 0


@dataclass(frozen=True)
class CaseVerdict:
    case_id: str
    kind: str  # qa / negative
    passed: bool
    labels: list[str]
    detail: dict[str, Any]

    @property
    def recall_labels(self) -> list[str]:
        return [label for label in self.labels if label in RECALL_SIDE_LABELS]

    @property
    def generation_labels(self) -> list[str]:
        return [label for label in self.labels if label in GENERATION_SIDE_LABELS]


def judge_case(case: "EvalCase", result: CaseResult) -> CaseVerdict:
    """逐题结构化断言；收集全部失败标签而非首错即停，保证报告可分诊。"""
    gt = case.ground_truth
    labels: list[str] = []
    detail: dict[str, Any] = {}

    if result.intent != gt.get("expected_intent"):
        labels.append("intent_mismatch")
        detail["intent_expected"] = gt.get("expected_intent")
        detail["intent_actual"] = result.intent

    if gt.get("expected_status") == "answered":
        if result.status != "answered":
            labels.append("unexpected_unknown")
            detail["unknown_reason"] = result.unknown_reason
        else:
            # 召回侧：白名单证据为空，或 required_evidence 任一 URN 子串未召回
            if not result.evidence_urns:
                labels.append("empty_retrieval")
            missing = [
                sub
                for sub in case.required_evidence
                if not any(sub in urn for urn in result.evidence_urns)
            ]
            if missing:
                labels.append("recall_miss")
                detail["missing_evidence"] = missing
            # 生成侧：回答了就必须带引用；引用 URN 必须落在 run 自己的白名单内
            if not result.citation_urns:
                labels.append("no_citations")
            forged = [
                urn for urn in result.citation_urns if urn not in result.whitelist_urns
            ]
            if forged:
                labels.append("citation_forgery")
                detail["forged_urns"] = forged
    else:
        # 负例题：任何 answered 都是错误放行（拒答正确率的分子是正确收口为 unknown）
        if result.status == "answered":
            labels.append("wrong_answer_on_negative")
        strict_reason = gt.get("expected_unknown_reason")
        if strict_reason and result.unknown_reason != strict_reason:
            labels.append("strict_reason_mismatch")
            detail["reason_expected"] = strict_reason
            detail["reason_actual"] = result.unknown_reason

    return CaseVerdict(
        case_id=case.case_id,
        kind=case.kind,
        passed=not labels,
        labels=labels,
        detail=detail,
    )


def detect_holdout_leakage(
    holdout_markers: list[str], indexed_doc_urns: list[str]
) -> list[str]:
    """FR-EV-001 泄漏检查：holdout 文档标识子串不得出现在任何已索引 URN 中。"""
    return [
        marker
        for marker in holdout_markers
        if any(marker in urn for urn in indexed_doc_urns)
    ]


def _percentile(values: list[int], pct: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    idx = round((len(ordered) - 1) * pct)
    return ordered[idx]


def aggregate_metrics(
    judgements: list[tuple["EvalCase", CaseVerdict, CaseResult]],
) -> dict[str, float]:
    """聚合 docs/06 §7 中 S1 可自动采集的指标；无分母的指标不输出（无基线不造数）。"""
    total = len(judgements)
    positives = [(c, v, r) for c, v, r in judgements if c.kind != "negative"]
    negatives = [(c, v, r) for c, v, r in judgements if c.kind == "negative"]
    passed = sum(1 for _, verdict, _ in judgements if verdict.passed)

    recall_ok = sum(
        1
        for _, verdict, _ in positives
        if not any(label in RECALL_SIDE_LABELS for label in verdict.labels)
    )
    refusals_ok = sum(1 for _, verdict, _ in negatives if verdict.passed)
    intent_ok = sum(
        1
        for _, verdict, _ in judgements
        if "intent_mismatch" not in verdict.labels
    )

    answered = [r for _, _, r in judgements if r.status == "answered"]
    forgeries = sum(
        1
        for _, verdict, _ in judgements
        if "citation_forgery" in verdict.labels
    )
    # 引用准确率：已回答 run 中「有引用且无伪造」的占比（伪造=0 是 S1 硬护栏）
    cited_clean = sum(
        1
        for r in answered
        if r.citation_urns
        and all(urn in r.whitelist_urns for urn in r.citation_urns)
    )

    label_counts: dict[str, int] = {}
    for _, verdict, _ in judgements:
        for label in verdict.labels:
            label_counts[label] = label_counts.get(label, 0) + 1

    latencies = [r.latency_ms for _, _, r in judgements]
    metrics: dict[str, float] = {
        "cases_total": float(total),
        "golden_pass_rate": passed / total if total else 0.0,
        "golden_passed": float(passed),
        "recall_at_k": recall_ok / len(positives) if positives else 0.0,
        "refusal_accuracy": refusals_ok / len(negatives) if negatives else 0.0,
        "intent_accuracy": intent_ok / total if total else 0.0,
        "citation_accuracy": cited_clean / len(answered) if answered else 0.0,
        "citation_forgery_count": float(forgeries),
        "answered_count": float(len(answered)),
        "latency_median_ms": float(statistics.median(latencies) if latencies else 0),
        "latency_p95_ms": float(_percentile(latencies, 0.95)),
        "tokens_in_total": float(sum(r.tokens_in for _, _, r in judgements)),
        "tokens_out_total": float(sum(r.tokens_out for _, _, r in judgements)),
        "attempts_total": float(sum(r.attempts for _, _, r in judgements)),
    }
    for label, count in sorted(label_counts.items()):
        metrics[f"label_{label}"] = float(count)
    return metrics
