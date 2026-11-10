"""S3.4 归因盲标夹具守卫测：夹具-引擎-门禁三方契约。"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

import pytest

from testpilot_core.attribution import EvidenceKind, attribute_failure
from testpilot_core.attribution_eval import (
    LabeledManifest,
    evaluate_attribution,
    seal_manifest,
)
from testpilot_core.enums import FailureCategory

FIXTURE = Path(__file__).parent / "fixtures" / "attribution" / "labeled_cases.json"


@pytest.fixture(scope="module")
def manifest() -> LabeledManifest:
    return LabeledManifest.model_validate_json(FIXTURE.read_text())


def test_manifest_shape_and_distribution(manifest: LabeledManifest):
    assert manifest.version == "attribution-labeled-v1"
    assert len(manifest.cases) == 40
    distribution = Counter(c.ground_truth for c in manifest.cases)
    assert distribution == Counter(
        {
            FailureCategory.REAL_DEFECT: 20,
            FailureCategory.FLAKY: 5,
            FailureCategory.CASE_OUTDATED: 4,
            FailureCategory.ENVIRONMENT: 4,
            FailureCategory.PRODUCT_CHANGE: 4,
            FailureCategory.NEEDS_HUMAN: 3,
        }
    )


def test_ac1_four_historical_archetypes_present(manifest: LabeledManifest):
    archetypes = {c.archetype for c in manifest.cases}
    # docs/08 FR-EX-007 AC1 点名的四类历史原型
    assert "case_outdated_90_to_30" in archetypes
    assert "environment_authorization_race" in archetypes
    assert "environment_device_only_capability" in archetypes
    assert any(a.startswith("flaky_") for a in archetypes)


def test_every_prediction_matches_ground_truth_and_gates_pass(manifest: LabeledManifest):
    for labeled in manifest.cases:
        verdict = attribute_failure(labeled.evidence)
        assert verdict.category == labeled.ground_truth, (
            labeled.case_id,
            verdict.category,
            verdict.needs_human_reason,
        )
    report = evaluate_attribution(manifest)
    assert report.all_gates_passed is True
    assert report.accuracy == 1.0
    assert report.real_defect_recall == 1.0
    assert report.misleading_rate == 0.0
    assert report.evidence_sufficiency >= 0.9


def test_seal_stable_on_reload(manifest: LabeledManifest):
    reloaded = LabeledManifest.model_validate_json(FIXTURE.read_text())
    assert seal_manifest(manifest) == seal_manifest(reloaded)
    assert re.fullmatch(r"[0-9a-f]{16}", seal_manifest(manifest))


def test_ledger_refs_unique_and_signals_backed(manifest: LabeledManifest):
    for labeled in manifest.cases:
        ev = labeled.evidence
        refs = [item.ref for item in ev.ledger]
        assert len(refs) == len(set(refs)), labeled.case_id
        kinds = {item.kind for item in ev.ledger}
        if ev.reruns:
            assert EvidenceKind.RERUN in kinds
        if ev.assertion is not None:
            assert EvidenceKind.ASSERTION in kinds
        if ev.log_signals:
            assert EvidenceKind.LOG in kinds
        if ev.diff is not None:
            assert EvidenceKind.DIFF in kinds
        if ev.environment:
            assert EvidenceKind.ENVIRONMENT in kinds
        if ev.change_records:
            assert EvidenceKind.CHANGE_RECORD in kinds


def test_fixture_contains_no_machine_paths_or_corpus_content():
    raw = FIXTURE.read_text()
    assert "/Users/" not in raw
    assert "app-calendar-alarm" not in raw
    # JSON 本身合法且顶层结构固定
    data = json.loads(raw)
    assert set(data.keys()) == {"version", "cases"}
