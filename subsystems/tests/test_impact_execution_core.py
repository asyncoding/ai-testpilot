"""impact-execution 纯函数 hostless 测试（docs/12 §4 E4）。

零 IO：手工构造 ImpactAnalysis 与 map bindings，钉死——
runnable 部分映射翻译、OID 去重、三分类分流、空选择 fail-closed、
幂等键稳定性与输入敏感、map 鲜度/版本守卫、坏 OID/坏绑定行拒绝。
"""

from __future__ import annotations

import pytest

from testpilot_core.automation_mapping import AUTOMATION_MAP_SCHEMA_VERSION
from testpilot_core.impact import ImpactAnalysis, ImpactedCase
from testpilot_core.impact_execution import (
    HEAD_SHORT_LEN,
    ImpactSelectionError,
    build_impact_selection,
    ensure_map_fresh,
    head_short,
    impact_idempotency_key,
    selection_digest,
    validate_selection_nonempty,
)

HEAD = "a" * 40
HEAD_OTHER = "b" * 40
OID_A = "AlarmKitSpikeTests/AgentTests/test_TCA_ring"
OID_B = "AlarmKitSpikeTests/AgentTests/test_TCB_calendar"
OID_C = "AlarmKitSpikeUITests/FlowUITests/test_TCC_flow"


def _case(case_id: str, classification: str = "runnable") -> ImpactedCase:
    return ImpactedCase(
        case_urn=f"urn:case:{case_id}", external_id=case_id,
        title=f"用例 {case_id}", classification=classification)  # type: ignore[arg-type]


def _analysis(*cases: ImpactedCase, head: str = HEAD) -> ImpactAnalysis:
    return ImpactAnalysis(
        base_commit="0" * 40, head_commit=head, cases=list(cases))


def _bind(case_id: str, oid: str) -> dict:
    return {"case_external_id": case_id, "only_testing_id": oid,
            "origin": "marker", "source_file": "X.swift", "line": 10}


def _map(*, head: str = HEAD, bindings=None) -> dict:
    return {
        "map_schema_version": AUTOMATION_MAP_SCHEMA_VERSION,
        "head_commit": head,
        "map_hash": "h",
        "bindings": bindings or [],
    }


def test_partial_mapping_translates_runnable_and_reports_unmapped():
    analysis = _analysis(
        _case("TC-A"), _case("TC-B"), _case("TC-N2"))
    selection = build_impact_selection(
        analysis=analysis,
        bindings=[_bind("TC-A", OID_A), _bind("TC-B", OID_B)],
        head_commit=HEAD)

    assert selection.selected_case_ids == ["TC-A", "TC-B"]
    assert selection.only_testing_ids == sorted([OID_A, OID_B])
    assert selection.unmapped_runnable_case_ids == ["TC-N2"]
    # 身份字段形态：impact:<12 位短 sha>:<16 位摘要>
    key = selection.idempotency_key
    prefix, short, digest = key.split(":")
    assert (prefix, short, len(digest)) == ("impact", "a" * HEAD_SHORT_LEN, 16)
    assert selection.request_id == f"impact-req-{short}-{digest[:8]}"
    assert selection.daily_budget_key == f"impact:{short}"


def test_one_case_many_methods_keeps_all_oids():
    selection = build_impact_selection(
        analysis=_analysis(_case("TC-C")),
        bindings=[_bind("TC-C", OID_A), _bind("TC-C", OID_C)],
        head_commit=HEAD)
    assert selection.only_testing_ids == sorted([OID_A, OID_C])
    assert selection.selected_case_ids == ["TC-C"]


def test_shared_method_between_two_cases_deduped():
    selection = build_impact_selection(
        analysis=_analysis(_case("TC-A"), _case("TC-B")),
        bindings=[_bind("TC-A", OID_A), _bind("TC-B", OID_A)],
        head_commit=HEAD)
    assert selection.only_testing_ids == [OID_A]
    assert selection.selected_case_ids == ["TC-A", "TC-B"]


def test_non_runnable_buckets_reported_but_never_selected():
    analysis = _analysis(
        _case("TC-A", "runnable"),
        _case("TC-EXP", "no_automation"),
        _case("TC-CONF", "conflict_update"))
    selection = build_impact_selection(
        analysis=analysis, bindings=[_bind("TC-A", OID_A)], head_commit=HEAD)

    assert selection.selected_case_ids == ["TC-A"]
    assert selection.no_automation_case_ids == ["TC-EXP"]
    assert selection.conflict_case_ids == ["TC-CONF"]
    # 即使冲突/无自动化用例在 map 里有残留绑定，也不得进选择集
    selection2 = build_impact_selection(
        analysis=analysis,
        bindings=[_bind("TC-A", OID_A), _bind("TC-CONF", OID_B)],
        head_commit=HEAD)
    assert OID_B not in selection2.only_testing_ids


def test_empty_selection_guard_lists_unmapped_cases_and_hint():
    selection = build_impact_selection(
        analysis=_analysis(_case("TC-N2"), _case("TC-N3")),
        bindings=[], head_commit=HEAD)
    assert selection.only_testing_ids == []
    with pytest.raises(ImpactSelectionError, match="TC-N2, TC-N3") as exc:
        validate_selection_nonempty(selection)
    assert "testpilot-scan-automation" in str(exc.value)
    assert "全量" in str(exc.value)


def test_guard_passes_when_selection_present():
    selection = build_impact_selection(
        analysis=_analysis(_case("TC-A")),
        bindings=[_bind("TC-A", OID_A)], head_commit=HEAD)
    validate_selection_nonempty(selection)  # 不抛即通过


def test_idempotency_key_order_insensitive_but_input_sensitive():
    key1 = impact_idempotency_key(HEAD, [OID_A, OID_B])
    key2 = impact_idempotency_key(HEAD, [OID_B, OID_A])
    assert key1 == key2
    assert impact_idempotency_key(HEAD_OTHER, [OID_A, OID_B]) != key1
    assert impact_idempotency_key(HEAD, [OID_A]) != key1
    assert selection_digest([]) == selection_digest([])  # 空集摘要稳定


def test_head_short_rejects_empty_sha():
    with pytest.raises(ImpactSelectionError):
        head_short("")
    with pytest.raises(ImpactSelectionError):
        head_short("   ")


def test_stale_map_and_bad_version_rejected():
    with pytest.raises(ImpactSelectionError, match="已过期"):
        ensure_map_fresh(_map(head=HEAD), HEAD_OTHER)
    bad_version = _map()
    bad_version["map_schema_version"] = "automation-map-v0"
    with pytest.raises(ImpactSelectionError, match="版本不支持"):
        ensure_map_fresh(bad_version, HEAD)
    # 同 head 同版本放行
    ensure_map_fresh(_map(head=HEAD), HEAD)


def test_illegal_oid_and_malformed_binding_row_fail_closed():
    with pytest.raises(ImpactSelectionError, match="非法"):
        build_impact_selection(
            analysis=_analysis(_case("TC-A")),
            bindings=[_bind("TC-A", "bad oid with spaces")],
            head_commit=HEAD)
    with pytest.raises(ImpactSelectionError, match="缺字段"):
        build_impact_selection(
            analysis=_analysis(_case("TC-A")),
            bindings=[{"case_external_id": "TC-A"}], head_commit=HEAD)
