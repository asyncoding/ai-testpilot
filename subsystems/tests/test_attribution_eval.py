"""FR-EX-007 AC2 混淆矩阵与门禁纯逻辑测试（S3.4，零 IO）。"""

from __future__ import annotations

import pytest

from testpilot_core.attribution import (
    AssertionCheck,
    DiffSignal,
    EnvironmentSignal,
    EvidenceItem,
    EvidenceKind,
    FailureEvidence,
    RerunRecord,
)
from testpilot_core.attribution_eval import (
    ACCURACY_GATE,
    EVIDENCE_SUFFICIENCY_GATE,
    MISLEADING_RATE_GATE,
    REAL_DEFECT_RECALL_GATE,
    LabeledCase,
    LabeledManifest,
    build_confusion_matrix,
    evaluate_attribution,
    misleading_rate,
    per_class_metrics,
    real_defect_recall,
    seal_manifest,
)
from testpilot_core.enums import CaseStatus, FailureCategory

COMMIT = "abc1234"


def _ledger(*kinds: str) -> list[EvidenceItem]:
    return [
        EvidenceItem(ref=f"ev-{kind}-{i}", kind=kind, summary=kind)
        for i, kind in enumerate(kinds)
    ]


def defect_packet(case_id: str = "d1") -> FailureEvidence:
    return FailureEvidence(
        case_id=case_id,
        commit_sha=COMMIT,
        stack_top="S.x()",
        diff=DiffSignal(
            base="b", head="h", changed_symbols=["S.x()"],
            stack_symbol="S.x()", stack_symbol_changed=True,
        ),
        ledger=_ledger(EvidenceKind.DIFF),
    )


def outdated_packet(case_id: str = "o1") -> FailureEvidence:
    return FailureEvidence(
        case_id=case_id,
        commit_sha=COMMIT,
        assertion=AssertionCheck(
            expected="90", actual_constant="30", matches=False, symbol_id="K.s"
        ),
        ledger=_ledger(EvidenceKind.ASSERTION),
    )


def flaky_packet(case_id: str = "f1") -> FailureEvidence:
    return FailureEvidence(
        case_id=case_id,
        commit_sha=COMMIT,
        reruns=[
            RerunRecord(run_id="r1", commit_sha=COMMIT, status=CaseStatus.FAILED),
            RerunRecord(run_id="r2", commit_sha=COMMIT, status=CaseStatus.PASSED),
        ],
        ledger=_ledger(EvidenceKind.RERUN),
    )


def environment_packet(case_id: str = "e1") -> FailureEvidence:
    return FailureEvidence(
        case_id=case_id,
        commit_sha=COMMIT,
        environment=[
            EnvironmentSignal(family="capability_gap", detail="模拟器无真机能力")
        ],
        ledger=_ledger(EvidenceKind.ENVIRONMENT),
    )


def thin_packet(case_id: str = "n1") -> FailureEvidence:
    return FailureEvidence(case_id=case_id, commit_sha=COMMIT)


def _case(case_id: str, truth: FailureCategory, packet: FailureEvidence) -> LabeledCase:
    return LabeledCase(
        case_id=case_id, archetype=f"arch-{case_id}", ground_truth=truth, evidence=packet
    )


def test_seal_deterministic_and_sensitive():
    manifest = LabeledManifest(cases=[_case("d1", FailureCategory.REAL_DEFECT, defect_packet())])
    assert seal_manifest(manifest) == seal_manifest(manifest.model_copy(deep=True))
    changed = manifest.model_copy(deep=True)
    changed.cases[0].evidence.message = "tampered"
    assert seal_manifest(changed) != seal_manifest(manifest)


def test_confusion_matrix_has_all_36_cells():
    labels = [FailureCategory.REAL_DEFECT]
    preds = [FailureCategory.FLAKY]
    matrix = build_confusion_matrix(labels, preds)
    assert len(matrix) == 36
    assert matrix["real_defect>flaky"] == 1
    assert matrix["real_defect>real_defect"] == 0


def test_confusion_matrix_length_mismatch_raises():
    with pytest.raises(ValueError):
        build_confusion_matrix([FailureCategory.FLAKY], [])


def test_per_class_metrics_hand_computed():
    labels = [
        FailureCategory.REAL_DEFECT, FailureCategory.REAL_DEFECT,
        FailureCategory.FLAKY, FailureCategory.FLAKY,
    ]
    preds = [
        FailureCategory.REAL_DEFECT, FailureCategory.FLAKY,
        FailureCategory.FLAKY, FailureCategory.NEEDS_HUMAN,
    ]
    metrics = {m.category: m for m in per_class_metrics(labels, preds)}
    # defect: TP1 FN1 → recall .5；pred defect 1 次全对 → precision 1.0
    assert metrics[FailureCategory.REAL_DEFECT].recall == 0.5
    assert metrics[FailureCategory.REAL_DEFECT].precision == 1.0
    # flaky: TP1 FN1 recall .5；pred flaky 2 次（1 错）→ precision .5
    assert metrics[FailureCategory.FLAKY].recall == 0.5
    assert metrics[FailureCategory.FLAKY].precision == 0.5
    # 标注集没有 environment 类 → recall None；从未预测 environment → precision None
    assert metrics[FailureCategory.ENVIRONMENT].recall is None
    assert metrics[FailureCategory.ENVIRONMENT].precision is None


def test_real_defect_recall_threshold_boundary():
    labels = [FailureCategory.REAL_DEFECT] * 20
    preds = [FailureCategory.REAL_DEFECT] * 19 + [FailureCategory.NEEDS_HUMAN]
    assert real_defect_recall(labels, preds) == 0.95
    assert real_defect_recall([], []) is None


def test_misleading_rate_excludes_abstention():
    labels = [FailureCategory.REAL_DEFECT] * 4
    preds = [
        FailureCategory.REAL_DEFECT,       # 正确
        FailureCategory.FLAKY,             # 误导
        FailureCategory.CASE_OUTDATED,     # 误导
        FailureCategory.NEEDS_HUMAN,       # 弃权：不算误导
    ]
    assert misleading_rate(labels, preds) == 0.5
    assert misleading_rate([FailureCategory.FLAKY], [FailureCategory.FLAKY]) is None


def test_evaluate_full_manifest_all_gates_pass():
    manifest = LabeledManifest(
        cases=[
            _case("d1", FailureCategory.REAL_DEFECT, defect_packet()),
            _case("o1", FailureCategory.CASE_OUTDATED, outdated_packet()),
            _case("f1", FailureCategory.FLAKY, flaky_packet()),
            _case("e1", FailureCategory.ENVIRONMENT, environment_packet()),
            _case("n1", FailureCategory.NEEDS_HUMAN, thin_packet()),
        ]
    )
    report = evaluate_attribution(manifest)
    assert report.n == 5
    assert report.accuracy == 1.0
    assert report.evidence_sufficiency == 0.8  # 5 例中 1 例弃权
    assert report.abstention_rate == 0.2
    assert report.real_defect_recall == 1.0
    assert report.misleading_rate == 0.0
    # 小清单弃权占比高：准确率/recall/误导三门禁过，证据充分率门禁如实失败
    gate_map = {g.name: g for g in report.gates}
    assert gate_map["归因准确率"].passed is True
    assert gate_map["真缺陷recall"].passed is True
    assert gate_map["误误导率"].passed is True
    assert gate_map["证据充分率"].passed is False
    assert report.all_gates_passed is False
    assert report.rule_version
    assert len(report.manifest_seal) == 16


def test_misleading_defect_fails_gate():
    # 构造 GT 缺陷但证据像 flaky 的包：让引擎把缺陷收口成 flaky
    packet = flaky_packet("d-bad")
    manifest = LabeledManifest(
        cases=[
            _case("d1", FailureCategory.REAL_DEFECT, defect_packet()),
            _case("d-bad", FailureCategory.REAL_DEFECT, packet),
        ]
    )
    report = evaluate_attribution(manifest)
    assert report.misleading_rate == 0.5
    gate = next(g for g in report.gates if g.name == "误误导率")
    assert gate.passed is False
    assert gate.threshold == MISLEADING_RATE_GATE
    assert report.all_gates_passed is False


def test_gate_constants_match_docs():
    assert ACCURACY_GATE == 0.7
    assert REAL_DEFECT_RECALL_GATE == 0.95
    assert MISLEADING_RATE_GATE == 0.02
    assert EVIDENCE_SUFFICIENCY_GATE == 0.9


def test_evaluation_order_independent():
    cases = [
        _case("d1", FailureCategory.REAL_DEFECT, defect_packet()),
        _case("o1", FailureCategory.CASE_OUTDATED, outdated_packet()),
        _case("f1", FailureCategory.FLAKY, flaky_packet()),
    ]
    first = evaluate_attribution(LabeledManifest(cases=cases))
    shuffled = evaluate_attribution(LabeledManifest(cases=list(reversed(cases))))
    assert first.confusion == shuffled.confusion
    assert first.accuracy == shuffled.accuracy
    assert first.manifest_seal != shuffled.manifest_seal  # 封存哈希仍如实反映清单变化


def test_empty_manifest_rejected():
    with pytest.raises(ValueError):
        evaluate_attribution(LabeledManifest(cases=[]))


def test_missing_defect_class_makes_recall_gate_fail():
    # 无缺陷样本时 recall=None，门禁必须显式失败而不是伪造通过
    manifest = LabeledManifest(
        cases=[_case("f1", FailureCategory.FLAKY, flaky_packet())]
    )
    report = evaluate_attribution(manifest)
    recall_gate = next(g for g in report.gates if g.name == "真缺陷recall")
    assert recall_gate.actual is None
    assert recall_gate.passed is False
    assert report.all_gates_passed is False
