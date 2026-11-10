"""S3.3 FR-EX-005 xcresulttool JSON 纯解析测试（core 零 IO，hostless）。

字段形态经 2026-11-10 真实探针校准（xcresulttool 25115 / schema 0.4.0）：
nodeType "Test Plan"/"Test Case" 大写形态、duration 字符串 + durationInSeconds
双源、失败证据是 Failure Message 子节点、附件 manifest 为顶层数组（无 mime，
按扩展名分型）。覆盖：树展平与 external_id 路径、三态 + Expected Failure 映射、
时长解析、失败首行截断、manifest 归属后缀匹配、畸形/未知形态 fail-closed、
TestRun 归一身份透传。全部自造合成 JSON，零语料仓内容。
"""

from __future__ import annotations

import pytest

from testpilot_core.enums import ArtifactType, CaseStatus, RunStatus
from testpilot_core.errors import SchemaValidationError
from testpilot_core.models import CaseResult, TestExecutionRequest
from testpilot_core.xcresult_parse import (
    link_attachment_urns,
    parse_attachment_manifest,
    parse_test_nodes,
    test_run_from_xcresult as build_run_from_xcresult,
)


def _req() -> TestExecutionRequest:
    return TestExecutionRequest(
        request_id="req-1", idempotency_key="idem-1", project="ios-demo",
        selector={"test_ids": ["AlarmKitSpikeTests"]},
        commit_sha="9f8e7d6c", build_id="xcode-1007",
    )


def _failure_node(text: str) -> dict:
    return {"nodeType": "Failure Message", "name": text, "details": text}


def _case_node(
    name: str,
    result: str,
    *,
    seconds: float | None = 0.1,
    human: str | None = None,
    failure_text: str | None = None,
) -> dict:
    """对齐真实 Test Case 节点：duration 字符串 + durationInSeconds 并存。"""
    node: dict = {"nodeType": "Test Case", "name": name, "result": result}
    if seconds is not None:
        node["durationInSeconds"] = seconds
        node["duration"] = human if human is not None else f"{seconds}s"
    if failure_text is not None:
        node["children"] = [_failure_node(failure_text)]
    return node


def _tree() -> dict:
    return {
        "testNodes": [
            {
                "nodeType": "Test Plan",
                "name": "AlarmKitSpike",
                "result": "Failed",
                "children": [
                    {
                        "nodeType": "Unit test bundle",
                        "name": "AlarmKitSpikeTests",
                        "result": "Failed",
                        "children": [
                            {
                                "nodeType": "Test Suite",
                                "name": "ReminderSchedulerTests",
                                "result": "Failed",
                                "children": [
                                    _case_node("testPass()", "Passed", seconds=0.03),
                                    _case_node("testSkip()", "Skipped", seconds=None),
                                    _case_node(
                                        "testFail()",
                                        "Failed",
                                        seconds=0.4,
                                        failure_text=(
                                            "XCTAssertEqual failed: (1) != (2)\n"
                                            "AlarmKitSpikeTests.swift:42"
                                        ),
                                    ),
                                ],
                            }
                        ],
                    }
                ],
            }
        ]
    }


_BASE = "AlarmKitSpike/AlarmKitSpikeTests/ReminderSchedulerTests"


def _suite(tree: dict) -> dict:
    # Test Plan → Unit test bundle → Test Suite
    return tree["testNodes"][0]["children"][0]["children"][0]


def _case(tree: dict, index: int) -> dict:
    return _suite(tree)["children"][index]


def test_parse_tree_flattens_cases_with_path_ids():
    cases = parse_test_nodes(_tree())
    assert [c.name for c in cases] == ["testPass()", "testSkip()", "testFail()"]
    assert cases[0].external_id == f"{_BASE}/testPass()"
    # node_id 定位到所属套件路径（不含自身）
    assert cases[0].node_id == _BASE
    assert cases[0].status is CaseStatus.PASSED
    assert cases[1].status is CaseStatus.SKIPPED and cases[1].duration_ms is None
    assert cases[2].status is CaseStatus.FAILED


def test_duration_dual_source_string_and_numeric_to_ms():
    cases = parse_test_nodes(_tree())
    assert cases[0].duration_ms == 30  # 0.03s
    assert cases[2].duration_ms == 400  # 0.4s
    # 无数值字段时回退解析 human-readable（schema 文档允许 d/h/m/s 分量）
    tree = _tree()
    leaf = _case(tree, 0)
    del leaf["durationInSeconds"]
    leaf["duration"] = "1m 2.5s"
    assert parse_test_nodes(tree)[0].duration_ms == 62500


def test_failure_text_keeps_first_line_only_and_truncates_500():
    cases = parse_test_nodes(_tree())
    failed = cases[2]
    assert failed.message == "XCTAssertEqual failed: (1) != (2)"
    assert failed.stack_top == "XCTAssertEqual failed: (1) != (2)"
    long_line = "x" * 800
    tree = {"testNodes": [
        {"nodeType": "Test Suite", "name": "S", "result": "Failed", "children": [
            _case_node("testLong()", "Failed", seconds=0.1,
                       failure_text=f"{long_line}\nsecond line")]}]}
    parsed = parse_test_nodes(tree)[0]
    assert len(parsed.message) == 500 and len(parsed.stack_top) == 500


def test_accepts_dict_and_json_string_equivalently():
    import json

    from_dict = parse_test_nodes(_tree())
    from_text = parse_test_nodes(json.dumps(_tree()))
    assert [c.external_id for c in from_dict] == [c.external_id for c in from_text]


def test_ui_bundle_and_nested_suites_walk_full_path():
    tree = {
        "testNodes": [{
            "nodeType": "UI test bundle", "name": "AlarmKitSpikeUITests",
            "result": "Passed",
            "children": [{
                "nodeType": "Test Suite", "name": "ProductFlowUITests",
                "result": "Passed",
                "children": [{
                    "nodeType": "Test Suite", "name": "LoginGroup", "result": "Passed",
                    "children": [_case_node("test01_login()", "Passed", seconds=12.5)],
                }],
            }],
        }]
    }
    case = parse_test_nodes(tree)[0]
    assert case.external_id == (
        "AlarmKitSpikeUITests/ProductFlowUITests/LoginGroup/test01_login()"
    )
    assert case.duration_ms == 12500


def test_expected_failure_maps_to_skipped_without_message():
    # XCTExpectFailure：XCTest 语义不红 bundle，映射 failed 会制造假缺陷信号
    tree = {"testNodes": [
        {"nodeType": "Test Suite", "name": "S", "result": "Passed", "children": [
            _case_node("testKnown()", "Expected Failure", seconds=0.2,
                       failure_text="known issue")]}]}
    case = parse_test_nodes(tree)[0]
    assert case.status is CaseStatus.SKIPPED and case.message == ""


@pytest.mark.parametrize(
    "payload",
    [
        "",
        "   ",
        "{not json",
        "[]",
        "{}",
        '{"testNodes": []}',
    ],
)
def test_malformed_top_level_payloads_fail_closed(payload):
    with pytest.raises(SchemaValidationError):
        parse_test_nodes(payload)


def test_unknown_node_type_fails_closed():
    tree = _tree()
    tree["testNodes"][0]["children"][0]["nodeType"] = "Mystery Node"
    with pytest.raises(SchemaValidationError, match="未知 nodeType"):
        parse_test_nodes(tree)


def test_unknown_case_result_fails_closed():
    tree = _tree()
    _case(tree, 0)["result"] = "Flaky"
    with pytest.raises(SchemaValidationError, match="result 未知"):
        parse_test_nodes(tree)


def test_official_unknown_result_still_fails_closed():
    tree = _tree()
    _case(tree, 0)["result"] = "unknown"
    with pytest.raises(SchemaValidationError, match="语义未校准"):
        parse_test_nodes(tree)


def test_unknown_container_result_fails_closed():
    tree = _tree()
    tree["testNodes"][0]["result"] = "Banana"
    with pytest.raises(SchemaValidationError):
        parse_test_nodes(tree)


def test_missing_name_fails_closed():
    tree = _tree()
    del _case(tree, 0)["name"]
    with pytest.raises(SchemaValidationError, match="缺少 name"):
        parse_test_nodes(tree)


def test_container_without_children_fails_closed():
    tree = _tree()
    del _suite(tree)["children"]
    with pytest.raises(SchemaValidationError, match="缺少 children"):
        parse_test_nodes(tree)


def test_bad_duration_fails_closed():
    tree = _tree()
    leaf = _case(tree, 0)
    # 真实形态下字符串是合法时长；垃圾字符串才 fail-closed
    del leaf["durationInSeconds"]
    leaf["duration"] = "soon"
    with pytest.raises(SchemaValidationError, match="非法 duration"):
        parse_test_nodes(tree)
    tree2 = _tree()
    leaf2 = _case(tree2, 0)
    leaf2["durationInSeconds"] = -1
    with pytest.raises(SchemaValidationError, match="不能为负"):
        parse_test_nodes(tree2)
    # bool 是 int 子类，明确拒绝防手滑
    tree3 = _tree()
    leaf3 = _case(tree3, 0)
    leaf3["durationInSeconds"] = True
    with pytest.raises(SchemaValidationError):
        parse_test_nodes(tree3)


def test_non_failed_case_carrying_failure_message_is_contradiction():
    tree = _tree()
    _case(tree, 0)["children"] = [_failure_node("surprise")]
    with pytest.raises(SchemaValidationError, match="状态与证据矛盾"):
        parse_test_nodes(tree)


def test_failed_case_without_failure_message_allowed_evidence_in_raw_bundle():
    tree = _tree()
    failed = _case(tree, 2)
    failed["children"] = []
    case = parse_test_nodes(tree)[2]
    assert case.status is CaseStatus.FAILED and case.message == ""


def test_malformed_failure_node_fails_closed():
    tree = _tree()
    _case(tree, 2)["children"] = [
        {"nodeType": "Failure Message"}  # 缺 details/name
    ]
    with pytest.raises(SchemaValidationError, match="Failure Message"):
        parse_test_nodes(tree)


def test_uncalibrated_child_under_case_fails_closed():
    tree = _tree()
    _case(tree, 2)["children"] = [
        {"nodeType": "Source Code Reference", "name": "x"}
    ]
    with pytest.raises(SchemaValidationError, match="未校准的子节点类型"):
        parse_test_nodes(tree)


# ── 附件 manifest（export attachments 顶层数组形态）─────────────────────────


def _case_ids() -> list[str]:
    return [c.external_id for c in parse_test_nodes(_tree())]


def _manifest_item(test_identifier: str, files: list[str], *, failure: bool = True):
    return {
        "testIdentifier": test_identifier,
        "testIdentifierURL": f"test://x/{test_identifier}",
        "attachments": [
            {
                "exportedFileName": f,
                "suggestedHumanReadableName": f,
                "isAssociatedWithFailure": failure,
                "configurationName": "Test Scheme Action",
                "deviceName": "testpilot-probe",
                "deviceId": "18BC020D",
            }
            for f in files
        ],
    }


def test_parse_manifest_suffix_match_groups_and_extension_types():
    fail_id = _case_ids()[2]
    payload = [
        _manifest_item(
            "ReminderSchedulerTests/testFail()",
            ["Screenshot-1.png", "Screenshot-2.jpg", "Recording.mp4"],
        ),
        _manifest_item(
            "ReminderSchedulerTests/testPass()", ["note.txt"], failure=False
        ),
    ]
    grouped = parse_attachment_manifest(payload, _case_ids())
    assert len(grouped[fail_id]) == 3
    assert [a.artifact_type for a in grouped[fail_id]] == [
        ArtifactType.SCREENSHOT, ArtifactType.SCREENSHOT, ArtifactType.VIDEO
    ]
    assert grouped[fail_id][0].is_failure_associated is True
    # 未登记扩展名落 other，原件仍保留不丢证据
    payload.append(_manifest_item("ReminderSchedulerTests/testFail()", ["blob.bin"]))
    grouped = parse_attachment_manifest(payload, _case_ids())
    assert grouped[fail_id][-1].artifact_type is ArtifactType.OTHER


def test_parse_manifest_empty_array_is_normal_all_green_run():
    # 真实全绿 run 实测 manifest.json 就是 []（rc=0），不是错误
    assert parse_attachment_manifest("[]", _case_ids()) == {}


def test_parse_manifest_identifier_zero_match_fails_closed():
    payload = [_manifest_item("GhostSuite/testGhost()", ["a.png"])]
    with pytest.raises(SchemaValidationError, match="归属匹配 0 条"):
        parse_attachment_manifest(payload, _case_ids())


def test_parse_manifest_identifier_ambiguous_match_fails_closed():
    # 两个外部 id 都以后缀结尾 → 归属猜不得
    ids = _case_ids() + ["Bundle/ReminderSchedulerTests/testFail()"]
    payload = [_manifest_item("ReminderSchedulerTests/testFail()", ["a.png"])]
    with pytest.raises(SchemaValidationError, match="归属匹配 2 条"):
        parse_attachment_manifest(payload, ids)


@pytest.mark.parametrize(
    "item",
    [
        {"attachments": [{"exportedFileName": "a.png"}]},  # 缺 testIdentifier
        {"testIdentifier": "ReminderSchedulerTests/testFail()",
         "attachments": [{}]},                              # 缺 exportedFileName
        {"testIdentifier": "  ", "attachments": [
            {"exportedFileName": "a.png"}]},                # 空白 testIdentifier
    ],
)
def test_parse_manifest_malformed_entry_fails_closed(item):
    with pytest.raises(SchemaValidationError):
        parse_attachment_manifest([item], _case_ids())


def test_parse_manifest_top_level_must_be_array():
    with pytest.raises(SchemaValidationError, match="顶层应为数组"):
        parse_attachment_manifest({"attachments": []}, _case_ids())


def test_link_attachment_urns_copies_without_mutating_input():
    cases = parse_test_nodes(_tree())
    run_cases = [
        CaseResult(external_case_id=c.external_id, node_id=c.node_id,
                   status=c.status, duration_ms=c.duration_ms,
                   message=c.message, stack_top=c.stack_top)
        for c in cases
    ]
    ref = run_cases[2].external_case_id
    urns = {ref: ["urn:testpilot:sha256:aaa", "urn:testpilot:sha256:bbb"]}
    linked = link_attachment_urns(run_cases, urns)
    assert linked[2].artifact_urns == urns[ref]
    # 输入不被改、无附件用例保持空列表且为原对象
    assert run_cases[2].artifact_urns == []
    assert linked[0] is run_cases[0]
    # 未知 id 不发明用例
    assert link_attachment_urns(run_cases, {"ghost/id": ["urn:x"]})[0] is run_cases[0]


# ── TestRun 归一 ─────────────────────────────────────────────────────────────


def test_test_run_from_xcresult_identity_and_failed_outcome():
    cases = parse_test_nodes(_tree())
    run = build_run_from_xcresult(cases, _req(), env_id="18BC020D-UDID",
                                 capabilities=["ios.simulator"],
                                 uncovered_layers=["ios.device", "codesigning"])
    assert run.run_id == ""  # 解析层不发明身份
    assert run.idempotency_key == "idem-1"
    assert run.project == "ios-demo" and run.commit_sha == "9f8e7d6c"
    assert run.status is RunStatus.FAILED
    assert run.environment.env_id == "18BC020D-UDID"
    assert run.environment.provider == "ios-simulator"
    assert run.uncovered_layers == ["ios.device", "codesigning"]
    assert run.runner_capability_version == "xcresulttool-schema-0.4.0"
    assert len(run.case_results) == 3
    # 时长只统计有用期值的用例（skipped 为 None 不计）
    assert run.duration_ms == 430
    assert run.artifacts == [] and run.raw_report_ref is None


def test_test_run_from_xcresult_all_passed_and_artifacts_passthrough():
    from testpilot_core.models import ArtifactRef

    tree = {"testNodes": [
        {"nodeType": "Test Suite", "name": "S", "result": "Passed", "children": [
            _case_node("a()", "Passed", seconds=0.01),
            _case_node("b()", "Passed", seconds=0.02)]}]}
    run = build_run_from_xcresult(
        parse_test_nodes(tree), _req(), env_id="u",
        artifacts=[ArtifactRef(type=ArtifactType.XCRESULT,
                               uri="urn:testpilot:sha256:bundle")],
        raw_report_ref="urn:testpilot:sha256:raw",
    )
    assert run.status is RunStatus.PASSED and run.duration_ms == 30
    assert run.artifacts[0].type is ArtifactType.XCRESULT
    assert run.raw_report_ref == "urn:testpilot:sha256:raw"


def test_zero_cases_would_be_run_error_via_run_outcome():
    # 正常路径 parse_test_nodes 不允许零用例；这里直接验证聚合契约：
    # 空结论 = 基础设施 error（构建失败等场景由适配器显式构造）
    run = build_run_from_xcresult([], _req(), env_id="u")
    assert run.status is RunStatus.ERROR
