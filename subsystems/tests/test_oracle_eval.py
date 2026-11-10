"""S2.6d oracle 有效率专家盲评纯逻辑测试（docs/06 §3.2 第五门，hostless）。

零 IO、零三方：直接构造 TestCase/OracleMiningResult/OracleExpertList，覆盖
有效率口径、探索性用例排除、missing 缺口、盲评顺序守卫与重放幂等。
"""

from __future__ import annotations

import pytest

from testpilot_core.enums import OracleType
from testpilot_core.models import TestCase
from testpilot_core.oracle_eval import (
    ORACLE_EFFECTIVENESS_GATE,
    OracleBlindError,
    OracleExpertCase,
    OracleExpertList,
    evaluate_oracles,
    seal_oracle_manifest,
)
from testpilot_core.oracle_mining import MinedOracle, OracleMiningResult

_DOC = "s2_gate/gate-cases.md"
_UI = OracleType.UI_ASSERTION
_LOG = OracleType.LOG_ASSERTION
_STATE = OracleType.STATE_ASSERTION
_MANUAL = OracleType.MANUAL


def _case(cid: str) -> TestCase:
    return TestCase(
        urn=f"urn:testpilot:fs-cases:test_case:{_DOC}#{cid}",
        external_id=cid,
        title=cid,
        steps=["执行一个步骤"],
    )


def _result(case: TestCase, types: list[OracleType], *, exploratory: bool = False,
           external_id: str = _DOC) -> OracleMiningResult:
    mined = [
        MinedOracle(
            type=t, expectation=f"预期-{t.value}",
            source_kind="existing_assertion",
        )
        for t in types
    ]
    return OracleMiningResult(
        case_urn=case.urn,
        external_id=external_id,
        mined=mined,
        exploratory=exploratory,
        exploratory_reasons=["manual_only"] if exploratory else [],
    )


def _manifest(rows: list[tuple[str, list[OracleType]]], *, doc: str = _DOC) -> OracleExpertList:
    return OracleExpertList(
        cases_doc_external_id=doc,
        sealed_at="2026-11-10T08:00:00+08:00",
        cases=[
            OracleExpertCase(case_external_id=cid, expected_executable=types)
            for cid, types in rows
        ],
    )


def test_all_valid_rate_one_and_exploratory_excluded() -> None:
    """有效 oracle 全命中专家期望；探索性用例不进分母但保留四分类盘点。"""
    g1, g2, g4 = _case("TC-G1"), _case("TC-G2"), _case("TC-G4")
    results = [
        _result(g1, [_UI, _LOG, _UI]),
        _result(g2, [_STATE, _STATE]),
        _result(g4, [_MANUAL], exploratory=True),
    ]
    manifest = _manifest([
        ("TC-G1", [_UI, _LOG]),
        ("TC-G2", [_STATE]),
        ("TC-G4", []),
    ])

    report = evaluate_oracles([g1, g2, g4], results, manifest)

    assert report.effective_rate == 1.0
    assert report.gate_ok is True
    assert report.valid_total == 5
    assert report.invalid_total == 0
    assert report.mined_executable_total == 5
    assert report.evaluated_cases == 2
    assert report.exploratory_excluded == 1
    assert report.coverage_complete is True
    # 人工类单列：探索性用例的 manual 不进 manual_total（该用例整体不参评）
    assert report.manual_total == 0
    assert report.by_type["manual"] == 1
    assert report.by_type["ui_assertion"] == 2
    assert report.by_type["log_assertion"] == 1
    assert report.by_type["state_assertion"] == 2

    verdicts = {v.case_external_id: v for v in report.verdicts}
    assert verdicts["TC-G4"].evaluated is False
    assert verdicts["TC-G1"].valid_types == ["log_assertion", "ui_assertion"]


def test_invalid_type_lowers_rate_and_fails_gate() -> None:
    """挖出专家未认可的可执行类型：有效率 0.5，门禁失败并点名 invalid。"""
    g1 = _case("TC-G1")
    results = [_result(g1, [_UI, _STATE])]
    manifest = _manifest([("TC-G1", [_UI])])

    report = evaluate_oracles([g1], results, manifest)

    assert report.effective_rate == 0.5
    assert report.gate_ok is False
    assert report.valid_total == 1
    assert report.invalid_total == 1
    verdict = report.verdicts[0]
    assert verdict.valid_types == ["ui_assertion"]
    assert verdict.invalid_types == ["state_assertion"]


def test_missing_types_break_coverage_but_not_rate() -> None:
    """专家期望 log 但没挖到：missing 记账、coverage_complete=False，有效率仍按挖出的算。"""
    g1 = _case("TC-G1")
    results = [_result(g1, [_UI])]
    manifest = _manifest([("TC-G1", [_UI, _LOG])])

    report = evaluate_oracles([g1], results, manifest)

    assert report.effective_rate == 1.0
    assert report.gate_ok is True
    assert report.missing_total == 1
    assert report.coverage_complete is False
    assert report.verdicts[0].missing_types == ["log_assertion"]


def test_exploratory_case_with_expert_expectation_records_missing() -> None:
    """探索性用例若专家给了非空期望，不允许静默排除：missing 照记。"""
    g4 = _case("TC-G4")
    results = [_result(g4, [_MANUAL], exploratory=True)]
    manifest = _manifest([("TC-G4", [_UI])])

    report = evaluate_oracles([g4], results, manifest)

    assert report.exploratory_excluded == 1
    assert report.mined_executable_total == 0
    assert report.missing_total == 1
    assert report.coverage_complete is False
    # 分母为零时有效率归零而不是除零，门禁失败
    assert report.effective_rate == 0.0
    assert report.gate_ok is False


def test_manual_oracle_counted_separately_in_evaluated_case() -> None:
    """非探索性用例里的 manual 单列统计，绝不进有效率分母。"""
    g1 = _case("TC-G1")
    results = [_result(g1, [_UI, _MANUAL])]
    manifest = _manifest([("TC-G1", [_UI])])

    report = evaluate_oracles([g1], results, manifest)

    assert report.manual_total == 1
    assert report.mined_executable_total == 1
    assert report.effective_rate == 1.0
    assert report.verdicts[0].manual_count == 1


def test_zero_executable_non_exploratory_rate_zero() -> None:
    """非探索性用例却挖不到任何可执行 oracle：零分母保护，门禁失败。"""
    g1 = _case("TC-G1")
    results = [_result(g1, [_MANUAL])]  # 非 exploratory
    manifest = _manifest([("TC-G1", [])])

    report = evaluate_oracles([g1], results, manifest)

    assert report.mined_executable_total == 0
    assert report.effective_rate == 0.0
    assert report.gate_ok is False


def test_gate_threshold_constant_matches_docs() -> None:
    assert ORACLE_EFFECTIVENESS_GATE == 0.7


def test_empty_manifest_raises() -> None:
    g1 = _case("TC-G1")
    with pytest.raises(OracleBlindError, match="盲评清单为空"):
        evaluate_oracles([g1], [_result(g1, [_UI])], _manifest([]))


def test_missing_case_judgment_raises() -> None:
    g1, g2 = _case("TC-G1"), _case("TC-G2")
    results = [_result(g1, [_UI]), _result(g2, [_STATE])]
    with pytest.raises(OracleBlindError, match="TC-G2"):
        evaluate_oracles([g1, g2], results, _manifest([("TC-G1", [_UI])]))


def test_doc_mismatch_raises() -> None:
    # URN 文档段与专家清单归属不同：防止拿 A 文档的清单评 B 文档的挖掘结果
    foreign = TestCase(
        urn=f"urn:testpilot:fs-cases:test_case:other/doc.md#TC-G1",
        external_id="TC-G1",
        title="TC-G1",
        steps=["执行一个步骤"],
    )
    results = [_result(foreign, [_UI])]
    with pytest.raises(OracleBlindError, match="不一致"):
        evaluate_oracles([foreign], results, _manifest([("TC-G1", [_UI])]))


def test_duplicate_expert_entry_and_count_mismatch_raise() -> None:
    g1 = _case("TC-G1")
    with pytest.raises(OracleBlindError, match="重复用例判定"):
        evaluate_oracles(
            [g1], [_result(g1, [_UI])],
            _manifest([("TC-G1", [_UI]), ("TC-G1", [_LOG])]),
        )
    with pytest.raises(OracleBlindError, match="不一致"):
        evaluate_oracles([], [], _manifest([("TC-G1", [_UI])]))


def test_seal_stable_and_replay_idempotent() -> None:
    """封存哈希内容寻址；同输入重放报告逐字段相等。"""
    g1 = _case("TC-G1")
    manifest = _manifest([("TC-G1", [_UI, _LOG])])
    results = [_result(g1, [_UI, _LOG])]

    seal_a = seal_oracle_manifest(manifest)
    seal_b = seal_oracle_manifest(manifest.model_copy(deep=True))
    assert seal_a == seal_b and len(seal_a) == 16

    report_a = evaluate_oracles([g1], results, manifest)
    report_b = evaluate_oracles([g1], results, manifest.model_copy(deep=True))
    assert report_a.model_dump() == report_b.model_dump()
    assert report_a.expert_seal_hash == seal_a
