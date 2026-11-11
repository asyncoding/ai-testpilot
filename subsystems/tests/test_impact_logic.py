"""影响面分析 / MR 摘要 / 回归选择精度 hostless 测试（FR-FRS-003，core/impact.py）。

零 IO、零三方：全部喂合成模型对象，验三分类闭集、原因码闭集、diff 反推摘要、
模型补点白名单拒入与 docs/06 精度口径。存储/scm 编排见 test_impact_service.py。
"""

from __future__ import annotations

import pytest

from testpilot_core.case_relations import CaseRelation
from testpilot_core.enums import CaseStatus, ChangeType
from testpilot_core.impact import (
    CaseExecutionOutcome,
    ChangedSymbol,
    ChangeSummaryResult,
    ImpactError,
    RejectedNote,
    UnresolvedSymbol,
    build_impact_analysis,
    regression_selection_precision,
    summarize_change_set,
    supplement_summary,
)
from testpilot_core.models import (
    ChangeSet,
    FileRename,
    Issue,
    RepoRef,
    SymbolChange,
    SymbolRename,
    TestCase,
)

BASE = "1" * 40
HEAD = "2" * 40
REPO = RepoRef(project="demo", repo_path="/tmp/demo-repo")

CASE_URN = "urn:testpilot:fs-cases:case:{cid}"
SYM_URN = "urn:testpilot:fs-docs:symbol:{name}"
REQ_URN = "urn:testpilot:fs-req:requirement:FR-1"
ISSUE_URN = "urn:testpilot:json-tickets:issue:{tid}"


def _case(cid: str, *, exploratory: bool = False,
          lifecycle: str = "active") -> TestCase:
    return TestCase(
        urn=CASE_URN.format(cid=cid),
        external_id=cid,
        title=f"用例 {cid}",
        steps=["步骤一", "步骤二"],
        exploratory=exploratory,
        lifecycle=lifecycle,  # type: ignore[arg-type]
    )


def _refs(case_id: str, symbol_name: str,
          confidence: float = 0.7) -> CaseRelation:
    return CaseRelation(
        urn=f"urn:testpilot:case-relations:rel:{case_id}-{symbol_name}",
        source_urn=CASE_URN.format(cid=case_id),
        target_urn=SYM_URN.format(name=symbol_name),
        kind="case_references_symbol",
        confidence=confidence,
        provenance=["oracle_suggestion"],
    )


def _validates(case_id: str, requirement_urn: str,
               confidence: float = 0.9) -> CaseRelation:
    return CaseRelation(
        urn=f"urn:testpilot:case-relations:rel:val-{case_id}",
        source_urn=CASE_URN.format(cid=case_id),
        target_urn=requirement_urn,
        kind="case_validates_requirement",
        confidence=confidence,
        provenance=["fr_tag"],
    )


def _changed(name: str, change_type: ChangeType,
             renamed_to: str = "") -> ChangedSymbol:
    return ChangedSymbol(
        urn=SYM_URN.format(name=name), change_type=change_type,
        renamed_to=renamed_to,
    )


def _issue(tid: str, related: list[str]) -> Issue:
    return Issue(
        urn=ISSUE_URN.format(tid=tid), external_id=tid,
        title=f"缺陷 {tid}", status="closed", severity="P1",
        related_case_ids=related,
    )


def _analysis(**overrides):
    params = dict(
        base_commit=BASE, head_commit=HEAD,
        changed_symbols=[_changed("sched", ChangeType.MODIFIED)],
        relations=[_refs("TC-1", "sched")],
        cases=[_case("TC-1")],
    )
    params.update(overrides)
    return build_impact_analysis(**params)


# ── 影响链：三分类闭集 ───────────────────────────────────────────────────────


def test_modified_symbol_case_is_runnable():
    analysis = _analysis()
    assert analysis.classification_counts == {
        "runnable": 1, "no_automation": 0, "conflict_update": 0}
    row = analysis.cases[0]
    assert row.classification == "runnable"
    assert row.reasons == ["symbol_modified"]
    assert row.symbol_urns == [SYM_URN.format(name="sched")]
    assert analysis.recommended_case_urns == [row.case_urn]
    assert analysis.affected_symbols[0].case_count == 1


def test_removed_symbol_is_conflict_update():
    analysis = _analysis(
        changed_symbols=[_changed("sched", ChangeType.REMOVED)])
    row = analysis.cases[0]
    assert row.classification == "conflict_update"
    assert row.reasons == ["symbol_removed"]
    assert analysis.recommended_case_urns == []


def test_renamed_symbol_conflicts_with_rename_reason_and_target():
    new_urn = SYM_URN.format(name="schedV2")
    analysis = _analysis(
        changed_symbols=[_changed(
            "sched", ChangeType.REMOVED, renamed_to=new_urn)])
    row = analysis.cases[0]
    assert row.classification == "conflict_update"
    assert row.reasons == ["symbol_renamed"]
    assert analysis.affected_symbols[0].renamed_to == new_urn


def test_signature_changed_is_conflict_update():
    analysis = _analysis(
        changed_symbols=[_changed("sched", ChangeType.SIGNATURE_CHANGED)])
    assert analysis.cases[0].classification == "conflict_update"
    assert analysis.cases[0].reasons == ["signature_changed"]


def test_exploratory_case_is_no_automation_but_keeps_conflict_reasons():
    analysis = _analysis(
        changed_symbols=[_changed("sched", ChangeType.REMOVED)],
        cases=[_case("TC-1", exploratory=True)])
    row = analysis.cases[0]
    assert row.classification == "no_automation"
    # 冲突事实不丢：更新文档时仍需知道锚点已失效
    assert "symbol_removed" in row.reasons
    assert "exploratory_case" in row.reasons


def test_cancelled_case_is_skipped_and_recorded():
    analysis = _analysis(cases=[_case("TC-1", lifecycle="cancelled")])
    assert analysis.cases == []
    assert analysis.cancelled_cases_skipped == ["TC-1"]
    assert analysis.affected_symbols[0].case_count == 0


def test_requirement_entry_pulls_in_case_without_symbol_link():
    analysis = build_impact_analysis(
        base_commit=BASE, head_commit=HEAD,
        changed_symbols=[_changed("sched", ChangeType.MODIFIED)],
        relations=[_validates("TC-9", REQ_URN)],
        cases=[_case("TC-9")],
        requirement_urns=[REQ_URN],
    )
    row = analysis.cases[0]
    assert row.classification == "runnable"
    assert row.reasons == ["requirement_entry"]
    assert row.requirement_urns == [REQ_URN]
    assert row.symbol_urns == []


def test_three_way_mixed_buckets_and_counts():
    # 探索性用例经需求入口纳入，落 no_automation；三桶各一
    analysis = build_impact_analysis(
        base_commit=BASE, head_commit=HEAD,
        changed_symbols=[
            _changed("mod", ChangeType.MODIFIED),
            _changed("gone", ChangeType.REMOVED),
        ],
        relations=[
            _refs("TC-RUN", "mod"),
            _refs("TC-CONF", "gone"),
            _validates("TC-EXP", REQ_URN),
        ],
        cases=[
            _case("TC-RUN"),
            _case("TC-CONF"),
            _case("TC-EXP", exploratory=True),
        ],
        requirement_urns=[REQ_URN],
    )
    buckets = {c.external_id: c.classification for c in analysis.cases}
    assert buckets == {
        "TC-RUN": "runnable",
        "TC-CONF": "conflict_update",
        "TC-EXP": "no_automation",
    }
    assert analysis.classification_counts == {
        "runnable": 1, "no_automation": 1, "conflict_update": 1}


def test_historical_defects_link_only_to_in_scope_cases():
    analysis = _analysis(issues=[
        _issue("TKT-1", ["TC-1", "TC-NOPE"]),
        _issue("TKT-2", ["TC-NOPE"]),
    ])
    assert len(analysis.historical_defects) == 1
    link = analysis.historical_defects[0]
    assert link.external_id == "TKT-1"
    assert link.matched_case_ids == ["TC-1"]


def test_relation_to_unaffected_symbol_does_not_seat_case():
    analysis = _analysis(
        changed_symbols=[_changed("sched", ChangeType.MODIFIED)],
        relations=[_refs("TC-1", "sched"), _refs("TC-2", "untouched")],
        cases=[_case("TC-1"), _case("TC-2")],
    )
    assert {c.external_id for c in analysis.cases} == {"TC-1"}


def test_unresolved_symbols_are_carried_verbatim():
    unresolved = UnresolvedSymbol(
        symbol_id="Sources/X.swift#Ghost.gone()",
        change_type=ChangeType.REMOVED)
    analysis = _analysis(unresolved_symbols=[unresolved])
    assert analysis.unresolved_symbols == [unresolved]


def test_confidence_takes_max_across_relations():
    analysis = build_impact_analysis(
        base_commit=BASE, head_commit=HEAD,
        changed_symbols=[_changed("sched", ChangeType.MODIFIED)],
        relations=[
            _refs("TC-1", "sched", confidence=0.5),
            CaseRelation(
                urn="urn:testpilot:case-relations:rel:second",
                source_urn=CASE_URN.format(cid="TC-1"),
                target_urn=SYM_URN.format(name="sched"),
                kind="case_references_symbol", confidence=0.9,
                provenance=["oracle_suggestion"],
            ),
        ],
        cases=[_case("TC-1")],
    )
    assert analysis.cases[0].confidence == 0.9


def test_empty_commit_range_raises():
    with pytest.raises(ImpactError, match="base_commit/head_commit"):
        build_impact_analysis(
            base_commit="", head_commit=HEAD,
            changed_symbols=[], relations=[], cases=[],
        )


def test_relation_pointing_to_missing_case_is_ignored():
    analysis = _analysis(
        relations=[_refs("TC-GHOST", "sched")], cases=[_case("TC-1")])
    assert analysis.cases == []


# ── MR 摘要：diff 反推 + 模型白名单补点 ─────────────────────────────────────


def _change_set() -> ChangeSet:
    return ChangeSet(
        repo=REPO, base_commit=BASE, head_commit=HEAD,
        changed_files=[
            "Sources/New.swift", "Sources/Old.swift",
            "Sources/Keep.swift", "README.md",
        ],
        symbol_changes=[
            SymbolChange(symbol_id="Sources/New.swift#Fresh.go()",
                         change_type=ChangeType.ADDED),
            SymbolChange(symbol_id="Sources/Old.swift#Ghost.gone()",
                         change_type=ChangeType.REMOVED),
            SymbolChange(symbol_id="Sources/Keep.swift#Keep.tweak()",
                         change_type=ChangeType.MODIFIED),
        ],
        file_renames=[
            FileRename(old_path="Sources/A.swift", new_path="Sources/B.swift",
                       similarity=100),
        ],
    )


def test_summary_is_reverse_derived_from_diff_with_counts_and_evidence():
    renames = [SymbolRename(
        old_symbol_id="Sources/Old.swift#Ghost.gone()",
        new_symbol_id="Sources/New.swift#Fresh.go()",
        confidence=0.95, reason="file_move",
    )]
    summary = summarize_change_set(_change_set(), renames)

    assert summary.generated_from == "diff"
    assert summary.files_changed == 4
    assert summary.files_added == 1 and summary.files_deleted == 1
    assert (summary.symbols_added == 1 and summary.symbols_removed == 1
            and summary.symbols_modified == 1)
    assert summary.rename_pairs == renames
    kinds = {b.kind for b in summary.bullets}
    assert kinds == {"file", "rename", "file_rename"}
    # 每条结论都带 diff 证据，可回查
    assert all(b.evidence for b in summary.bullets)
    rename_bullet = next(b for b in summary.bullets if b.kind == "rename")
    assert "Ghost.gone()" in rename_bullet.text
    deleted_bullet = next(
        b for b in summary.bullets
        if b.kind == "file" and "Old.swift" in b.text)
    assert "删除 1" in deleted_bullet.text
    modified_bullet = next(
        b for b in summary.bullets
        if b.kind == "file" and "Keep.swift" in b.text)
    assert "函数体修改 1" in modified_bullet.text


def test_doc_only_change_is_neither_added_nor_deleted():
    cs = ChangeSet(
        repo=REPO, base_commit=BASE, head_commit=HEAD,
        changed_files=["README.md"], symbol_changes=[],
    )
    summary = summarize_change_set(cs)
    assert summary.files_changed == 1
    assert summary.files_added == 0 and summary.files_deleted == 0
    assert summary.bullets == []


def test_supplement_whitelist_accepts_and_rejects_with_closed_reasons():
    summary = summarize_change_set(_change_set())
    good_urn = SYM_URN.format(name="sched")
    result = supplement_summary(
        summary,
        changed_urns=[good_urn],
        model_rows=[
            {"text": " 风险：调度路径变更需关注响铃时序 ", "symbol_urn": good_urn},
            {"text": "越界备注", "symbol_urn": SYM_URN.format(name="evil")},
            {"text": "   ", "symbol_urn": good_urn},
            "not-a-dict",
            {"symbol_urn": good_urn},
        ],
        model_version="whitelist-model-1",
    )
    assert isinstance(result, ChangeSummaryResult)
    assert len(result.notes) == 1
    assert result.notes[0].text == "风险：调度路径变更需关注响铃时序"
    assert result.generation_source == "model"
    assert [r.reason for r in result.rejected] == [
        "unknown_symbol", "empty_text", "bad_shape", "bad_shape"]
    # 确定性脊柱只读不写：模型行改不了 bullets
    assert result.summary.bullets == summary.bullets
    assert result.hallucination_rate == pytest.approx(0.8)
    assert all(isinstance(r, RejectedNote) for r in result.rejected)


def test_supplement_all_rejected_stays_deterministic():
    summary = summarize_change_set(_change_set())
    result = supplement_summary(
        summary, changed_urns=[SYM_URN.format(name="sched")],
        model_rows=[{"text": "x", "symbol_urn": "urn:none"}],
    )
    assert result.notes == []
    assert result.generation_source == "deterministic"
    assert result.rejected[0].reason == "unknown_symbol"


# ── 回归选择精度（docs/06 口径，门限 0.8） ──────────────────────────────────


def _outcome(cid: str, status: CaseStatus) -> CaseExecutionOutcome:
    return CaseExecutionOutcome(case_urn=CASE_URN.format(cid=cid), status=status)


def test_precision_hits_gate_at_0_8_and_reduction_ratio():
    recommended = [CASE_URN.format(cid=f"TC-{i}") for i in range(5)]
    executed = [_outcome(f"TC-{i}", CaseStatus.PASSED) for i in range(5)]
    truth = set(recommended[:4])  # 4/5 确实相关
    report = regression_selection_precision(
        recommended=recommended, executed=executed,
        related_truth=truth, full_suite_size=20,
    )
    assert report.evaluated == 5 and report.related_hits == 4
    assert report.precision == pytest.approx(0.8)
    assert report.gate_passed is True
    assert report.reduction_ratio == pytest.approx(0.75)


def test_empty_denominator_yields_none_precision_and_fails_gate():
    report = regression_selection_precision(
        recommended=[CASE_URN.format(cid="TC-1")],
        executed=[], related_truth=[],
    )
    assert report.precision is None
    assert report.gate_passed is False
    assert report.reduction_ratio is None


def test_recommended_but_not_executed_excluded_and_truth_missed_tracked():
    recommended = [CASE_URN.format(cid=c) for c in ("TC-1", "TC-2", "TC-3")]
    executed = [
        _outcome("TC-1", CaseStatus.FAILED),
        _outcome("TC-2", CaseStatus.PASSED),
        _outcome("TC-9", CaseStatus.PASSED),  # 执行了但没推荐：不进分母
    ]
    truth = {
        CASE_URN.format(cid="TC-1"),
        CASE_URN.format(cid="TC-9"),  # 实际相关但推荐集漏了
    }
    report = regression_selection_precision(
        recommended=recommended, executed=executed, related_truth=truth,
    )
    # 分母只有 TC-1/TC-2；命中仅 TC-1 → 0.5，过门限失败
    assert report.evaluated == 2 and report.related_hits == 1
    assert report.precision == pytest.approx(0.5)
    assert report.gate_passed is False
    assert report.truth_missed == 1


def test_precision_dedupes_repeated_inputs():
    urn = CASE_URN.format(cid="TC-1")
    report = regression_selection_precision(
        recommended=[urn, urn], executed=[_outcome("TC-1", CaseStatus.PASSED)],
        related_truth=[urn, urn],
    )
    assert report.recommended_total == 1
    assert report.evaluated == 1 and report.related_hits == 1
    assert report.precision == 1.0 and report.gate_passed is True
