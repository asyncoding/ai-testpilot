"""case-export-v1 导出纯函数 hostless 测试（docs/01 §3.1 / E1）。

零 IO、零容器：直接手工构造 core 模型，钉死契约规则——
cancelled 只进 excluded、探索性打标、空库响亮失败、内容 hash 跨时间戳稳定、
TC-ID → Swift token 转换、挖掘草稿按 case_urn 挂载且保留人审状态。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from testpilot_core.case_export import (
    CASE_EXPORT_SCHEMA_VERSION,
    CaseExportError,
    GenerationConvention,
    VersionQuadruple,
    build_case_export,
    canonical_content_hash,
    export_filename,
    tc_id_to_swift_token,
    utc_export_stamp,
)
from testpilot_core.enums import OracleType
from testpilot_core.models import Oracle, TestCase, TestPoint
from testpilot_core.oracle_mining import MinedOracle
from testpilot_core.requirement import (
    RequirementAcceptance,
    RequirementFeature,
    RequirementParseResult,
    RequirementRule,
)

_T0 = datetime(2026, 11, 13, 8, 0, tzinfo=timezone.utc)
_T1 = _T0 + timedelta(hours=2)
_DOC_URN = "urn:testpilot:fs-reqs:requirement:req/alarm.md"
_CASE_URN_A = "urn:testpilot:fs-cases:test_case:cases.md#TC-A1"
_CASE_URN_B = "urn:testpilot:fs-cases:test_case:cases.md#TC-B2"
_CASE_URN_C = "urn:testpilot:fs-cases:test_case:cases.md#TC-C3"


def _requirement() -> RequirementParseResult:
    return RequirementParseResult(
        doc_urn=_DOC_URN,
        external_id="REQ-ALARM-1",
        features=[RequirementFeature(
            urn="urn:fr1", fr_id="FR-1", text="倒计时响铃", priority="P0",
            module="RoutineAlarm")],
        rules=[RequirementRule(urn="urn:r1", rule_id="R-1", text="铃声不可静音")],
        acceptance=[RequirementAcceptance(index=0, text="倒计时结束弹出全屏响铃页")],
    )


def _point() -> TestPoint:
    return TestPoint(
        text="倒计时各档保存后重启仍生效", priority="P0", fr_id="FR-1",
        kind="functional", provenance="deterministic",
        source_refs=[_DOC_URN])


def _case(case_id: str, urn: str, **over) -> TestCase:
    kwargs = dict(
        urn=urn, external_id=case_id, title=f"用例 {case_id}",
        module="RoutineAlarm", priority="P0", steps=["步骤1", "步骤2"],
        oracles=[Oracle(type=OracleType.STATE_ASSERTION,
                        expectation="闹钟快照存在", confidence=0.8)],
        requirement_urn=_DOC_URN, tags=["regression"])
    kwargs.update(over)
    return TestCase(**kwargs)


def _suggestion(case_urn: str, external_id: str, *, status: str = "approved") -> dict:
    return {
        "case_urn": case_urn,
        "external_id": external_id,
        "status": status,
        "exploratory": False,
        "oracle": MinedOracle(
            type=OracleType.LOG_ASSERTION,
            expectation="os_log 出现 ring fire 事件",
            source_kind="incident", provenance="deterministic",
            source_refs=["urn:incident:INC-9"], confidence=0.7),
    }


def _build(**over):
    kwargs = dict(
        profile="ios-local-poc", project="AlarmKitSpike",
        source_branch="alarmkit-spike",
        requirements=[_requirement()], test_points=[_point()],
        cases=[
            _case("TC-A1", _CASE_URN_A),
            _case("TC-B2", _CASE_URN_B, exploratory=True, oracle_missing=True,
                  oracles=[]),
            _case("TC-C3", _CASE_URN_C, lifecycle="cancelled"),
        ],
        now=_T0,
    )
    kwargs.update(over)
    return build_case_export(**kwargs)


def test_payload_contract_sections_and_convention_defaults():
    payload = _build()
    assert payload["export_schema_version"] == CASE_EXPORT_SCHEMA_VERSION
    assert payload["exported_at"] == "2026-11-13T08:00:00Z"
    assert payload["profile"] == "ios-local-poc"
    assert payload["project"] == "AlarmKitSpike"
    assert payload["source_branch"] == "alarmkit-spike"
    convention = payload["generation_convention"]
    assert convention["marker_comment"] == "// testpilot-case: <TC-ID>"
    assert convention["target_target"] == "AlarmKitSpikeTests"
    assert convention["ui_test_target"] == "AlarmKitSpikeUITests"
    assert payload["version_quadruple"] == {
        "data_version": "", "model_version": "",
        "prompt_version": "", "params_version": ""}


def test_requirement_testpoint_serialization_uses_source_fields():
    payload = _build()
    req = payload["requirements"][0]
    assert req["urn"] == _DOC_URN
    assert req["external_id"] == "REQ-ALARM-1"
    assert req["features"][0]["fr_id"] == "FR-1"
    assert req["features"][0]["lifecycle"] == "active"
    assert req["rules"][0]["rule_id"] == "R-1"
    # AC 序号 0-based 存储，导出 1-based 对外
    assert req["acceptance_criteria"] == [
        {"id": "AC-1", "text": "倒计时结束弹出全屏响铃页"}]
    point = payload["test_points"][0]
    assert point["fr_id"] == "FR-1"
    assert point["provenance"] == "deterministic"
    assert point["source_refs"] == [_DOC_URN]


def test_cancelled_case_never_enters_cases_but_is_listed_in_excluded():
    """红证（docs/01 E1）：cancelled 混入 cases 的实现必须让本测试变红。"""
    payload = _build()
    exported_ids = {c["external_id"] for c in payload["cases"]}
    assert "TC-C3" not in exported_ids
    assert all(c["lifecycle"] == "active" for c in payload["cases"])
    assert payload["excluded"]["cancelled_case_ids"] == ["TC-C3"]
    assert len(payload["cases"]) == 2


def test_exploratory_case_exported_with_flag_by_default():
    payload = _build()
    by_id = {c["external_id"]: c for c in payload["cases"]}
    assert by_id["TC-B2"]["exploratory"] is True
    assert by_id["TC-B2"]["oracle_missing"] is True
    assert payload["excluded"]["exploratory_case_ids"] == []


def test_exclude_exploratory_moves_ids_to_excluded():
    payload = _build(include_exploratory=False)
    exported_ids = {c["external_id"] for c in payload["cases"]}
    assert "TC-B2" not in exported_ids
    assert payload["excluded"]["exploratory_case_ids"] == ["TC-B2"]


def test_empty_library_and_all_cancelled_fail_loudly():
    common = dict(
        profile="p", project="prj", source_branch="main",
        requirements=[], test_points=[], now=_T0)
    with pytest.raises(CaseExportError):
        build_case_export(**common, cases=[])
    with pytest.raises(CaseExportError):
        build_case_export(**common, cases=[_case("TC-C3", _CASE_URN_C,
                                                 lifecycle="cancelled")])
    # 全是探索性用例且显式排除时同样拒绝空壳
    with pytest.raises(CaseExportError):
        build_case_export(**common,
                          cases=[_case("TC-B2", _CASE_URN_B, exploratory=True)],
                          include_exploratory=False)


def test_duplicate_external_id_rejected():
    with pytest.raises(CaseExportError, match="重复"):
        _build(cases=[_case("TC-A1", _CASE_URN_A),
                      _case("TC-A1", "urn:other")])


def test_content_hash_stable_across_timestamps_but_sensitive_to_content():
    first = _build(now=_T0)
    second = _build(now=_T1)
    assert first["exported_at"] != second["exported_at"]
    assert first["content_hash"] == second["content_hash"]
    # 手工归一化口径与载荷内 hash 一致
    assert canonical_content_hash(first) == first["content_hash"]
    changed = _build(now=_T0,
                     cases=[_case("TC-A1", _CASE_URN_A, steps=["改了步骤"])])
    assert changed["content_hash"] != first["content_hash"]


def test_mined_suggestions_attached_by_case_urn_with_review_status():
    payload = _build(oracle_suggestions=[
        _suggestion(_CASE_URN_A, "TC-A1", status="approved"),
        _suggestion(_CASE_URN_B, "TC-B2", status="pending"),
        _suggestion("urn:unknown", "TC-X9", status="approved"),
    ])
    by_id = {c["external_id"]: c for c in payload["cases"]}
    suggestions_a = by_id["TC-A1"]["mined_oracle_suggestions"]
    assert len(suggestions_a) == 1
    assert suggestions_a[0]["type"] == "log_assertion"
    assert suggestions_a[0]["status"] == "approved"
    assert suggestions_a[0]["source_kind"] == "incident"
    assert by_id["TC-B2"]["mined_oracle_suggestions"][0]["status"] == "pending"
    # 悬空 URN 的草稿不挂到任何用例
    assert all(not c["mined_oracle_suggestions"] or
               c["external_id"] in {"TC-A1", "TC-B2"} for c in payload["cases"])


def test_version_quadruple_and_custom_convention_passthrough():
    payload = _build(
        version=VersionQuadruple(data_version="ingest-7f3a91",
                                 model_version="ollama:q3"),
        convention=GenerationConvention(target_target="DemoTests"))
    assert payload["version_quadruple"]["data_version"] == "ingest-7f3a91"
    assert payload["version_quadruple"]["model_version"] == "ollama:q3"
    assert payload["generation_convention"]["target_target"] == "DemoTests"


def test_tc_id_to_swift_token():
    assert tc_id_to_swift_token("TC-C1") == "TCC1"
    assert tc_id_to_swift_token("TC-C1a") == "TCC1a"
    assert tc_id_to_swift_token("TC-HOLIDAY-7") == "TCHOLIDAY7"
    with pytest.raises(CaseExportError):
        tc_id_to_swift_token("")
    with pytest.raises(CaseExportError):
        tc_id_to_swift_token("TC-坏ID")


def test_export_filename_and_stamp_format():
    assert utc_export_stamp(_T0) == "2026-11-13T08:00:00Z"
    name = export_filename(_T0, "0123456789abcdef")
    assert name == "case-set-20261113T080000Z-01234567.json"
    with pytest.raises(CaseExportError):
        utc_export_stamp(datetime(2026, 11, 13, 8, 0))  # naive 时区拒绝猜测


def test_payload_is_json_serializable_and_roundtrips():
    payload = _build()
    blob = json.dumps(payload, ensure_ascii=False)
    restored = json.loads(blob)
    assert restored["content_hash"] == payload["content_hash"]
    assert restored["cases"][0]["oracles"][0]["type"] == "state_assertion"
