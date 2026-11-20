"""失败归因混淆矩阵与门禁纯逻辑（docs/08 FR-EX-007 AC2，S3.4）。

零 IO、零三方：吃专家盲标清单（:class:`LabeledManifest`），跑
:mod:`attribution` 引擎后产出 6×6 混淆矩阵、各类 precision/recall 与四个门禁：

- 归因准确率 ≥ ``ACCURACY_GATE``（docs/06 场景③，POC ≥0.7）
- 真缺陷 recall ≥ ``REAL_DEFECT_RECALL_GATE``（漏报代价最高，≥0.95）
- 误误导率 ≤ ``MISLEADING_RATE_GATE``（护栏级，≤0.02）
- 证据充分率 ≥ ``EVIDENCE_SUFFICIENCY_GATE``（≥0.9）

误误导口径（与 S3.4 handoff 钉版一致）：ground truth 为真缺陷、却被收口进
四个"非缺陷"类别（用例过期/flaky/环境/产品改动）的比例；判 needs_human 是
弃权升级人审，不会放过缺陷，不计入误导，单独算弃权率。
"""

from __future__ import annotations

import hashlib
import json

from pydantic import BaseModel, Field

from .attribution import ATTRIBUTION_RULE_VERSION, FailureEvidence, attribute_failure
from .enums import FailureCategory

#: 门禁阈值（docs/01 §10 / docs/06 §3.3 场景③）。
ACCURACY_GATE = 0.7
REAL_DEFECT_RECALL_GATE = 0.95
MISLEADING_RATE_GATE = 0.02
EVIDENCE_SUFFICIENCY_GATE = 0.9

#: 矩阵固定类别顺序（五候选 + 弃权），报告行列口径稳定。
CATEGORIES: tuple[FailureCategory, ...] = (
    FailureCategory.REAL_DEFECT,
    FailureCategory.CASE_OUTDATED,
    FailureCategory.FLAKY,
    FailureCategory.ENVIRONMENT,
    FailureCategory.PRODUCT_CHANGE,
    FailureCategory.NEEDS_HUMAN,
)

#: 四个"非缺陷收口"类：真缺陷被误判进这里才算误误导。
NON_DEFECT_CLOSURES = frozenset(
    {
        FailureCategory.CASE_OUTDATED,
        FailureCategory.FLAKY,
        FailureCategory.ENVIRONMENT,
        FailureCategory.PRODUCT_CHANGE,
    }
)


class LabeledCase(BaseModel):
    """一条专家盲标样本：ground_truth 对评测器保密到跑分时刻才对照。"""

    case_id: str
    archetype: str
    ground_truth: FailureCategory
    evidence: FailureEvidence


class LabeledManifest(BaseModel):
    version: str = "attribution-labeled-v1"
    cases: list[LabeledCase] = Field(default_factory=list)


class CategoryMetric(BaseModel):
    category: FailureCategory
    support: int  # 该类 ground truth 样本数
    precision: float | None  # 零除（模型从未预测该类）→ None，不伪造 0
    recall: float | None  # 零除（标注集无该类）→ None


class GateResult(BaseModel):
    name: str
    actual: float | None
    threshold: float
    passed: bool
    direction: str  # "ge" / "le"


class AttributionEvalReport(BaseModel):
    manifest_seal: str
    rule_version: str
    n: int
    accuracy: float
    abstention_rate: float
    evidence_sufficiency: float
    real_defect_recall: float | None
    misleading_rate: float | None
    confusion: dict[str, int]  # 键 "ground_truth>prediction"，零格也显式列 0
    per_class: list[CategoryMetric]
    gates: list[GateResult]
    all_gates_passed: bool


def seal_manifest(manifest: LabeledManifest) -> str:
    """封存哈希：规范 JSON 后 sha256 截 16 位，证明报告基于哪版盲标清单。"""
    canonical = json.dumps(
        manifest.model_dump(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _matrix_key(truth: FailureCategory, pred: FailureCategory) -> str:
    return f"{truth.value}>{pred.value}"


def build_confusion_matrix(
    labels: list[FailureCategory], predictions: list[FailureCategory]
) -> dict[str, int]:
    """6×6 混淆矩阵；长度不一致是调用方 bug，直接抛。"""
    if len(labels) != len(predictions):
        raise ValueError("labels 与 predictions 长度不一致")
    matrix = {
        _matrix_key(truth, pred): 0
        for truth in CATEGORIES
        for pred in CATEGORIES
    }
    for truth, pred in zip(labels, predictions):
        matrix[_matrix_key(truth, pred)] += 1
    return matrix


def _safe_ratio(numerator: int, denominator: int) -> float | None:
    if denominator == 0:
        return None
    return numerator / denominator


def per_class_metrics(
    labels: list[FailureCategory],
    predictions: list[FailureCategory],
) -> list[CategoryMetric]:
    matrix = build_confusion_matrix(labels, predictions)
    metrics: list[CategoryMetric] = []
    for category in CATEGORIES:
        support = sum(
            1 for truth in labels if truth == category
        )
        tp = matrix[_matrix_key(category, category)]
        fp = sum(
            matrix[_matrix_key(truth, category)]
            for truth in CATEGORIES
            if truth != category
        )
        fn = sum(
            matrix[_matrix_key(category, pred)]
            for pred in CATEGORIES
            if pred != category
        )
        metrics.append(
            CategoryMetric(
                category=category,
                support=support,
                precision=_safe_ratio(tp, tp + fp),
                recall=_safe_ratio(tp, tp + fn),
            )
        )
    return metrics


def real_defect_recall(
    labels: list[FailureCategory], predictions: list[FailureCategory]
) -> float | None:
    """真缺陷 recall：GT 缺陷中被正确识别的比例（无缺陷样本 → None 不参评）。"""
    defects = [
        pred for truth, pred in zip(labels, predictions)
        if truth == FailureCategory.REAL_DEFECT
    ]
    return _safe_ratio(
        sum(1 for pred in defects if pred == FailureCategory.REAL_DEFECT),
        len(defects),
    )


def misleading_rate(
    labels: list[FailureCategory], predictions: list[FailureCategory]
) -> float | None:
    """误误导率：GT 缺陷被收口进非缺陷类的比例；needs_human 弃权不计。"""
    defects = [
        pred for truth, pred in zip(labels, predictions)
        if truth == FailureCategory.REAL_DEFECT
    ]
    if not defects:
        return None
    misled = sum(1 for pred in defects if pred in NON_DEFECT_CLOSURES)
    return misled / len(defects)


def _gate(name: str, actual: float | None, threshold: float, direction: str) -> GateResult:
    if actual is None:
        return GateResult(
            name=name, actual=None, threshold=threshold, passed=False, direction=direction
        )
    passed = actual >= threshold if direction == "ge" else actual <= threshold
    return GateResult(
        name=name, actual=actual, threshold=threshold, passed=passed, direction=direction
    )


def evaluate_attribution(manifest: LabeledManifest) -> AttributionEvalReport:
    """对盲标清单跑引擎并出完整评测报告（纯函数，可离线 hermetic 复跑）。"""
    if not manifest.cases:
        raise ValueError("标注清单为空，无法评测")
    labels = [case.ground_truth for case in manifest.cases]
    verdicts = [attribute_failure(case.evidence) for case in manifest.cases]
    predictions = [v.category for v in verdicts]

    matrix = build_confusion_matrix(labels, predictions)
    n = len(manifest.cases)
    correct = sum(1 for truth, pred in zip(labels, predictions) if truth == pred)
    abstentions = sum(
        1 for pred in predictions if pred == FailureCategory.NEEDS_HUMAN
    )
    abstention = abstentions / n
    defect_recall = real_defect_recall(labels, predictions)
    misled = misleading_rate(labels, predictions)

    gates = [
        _gate("归因准确率", correct / n, ACCURACY_GATE, "ge"),
        _gate("真缺陷recall", defect_recall, REAL_DEFECT_RECALL_GATE, "ge"),
        _gate("误误导率", misled, MISLEADING_RATE_GATE, "le"),
        _gate(
            "证据充分率", 1 - abstention, EVIDENCE_SUFFICIENCY_GATE, "ge"
        ),
    ]

    return AttributionEvalReport(
        manifest_seal=seal_manifest(manifest),
        rule_version=ATTRIBUTION_RULE_VERSION,
        n=n,
        accuracy=correct / n,
        abstention_rate=abstention,
        evidence_sufficiency=1 - abstention,
        real_defect_recall=defect_recall,
        misleading_rate=misled,
        confusion=matrix,
        per_class=per_class_metrics(labels, predictions),
        gates=gates,
        all_gates_passed=all(g.passed for g in gates),
    )
