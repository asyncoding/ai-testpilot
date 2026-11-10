"""S3.3 FR-EX-005 xcresulttool JSON 纯解析测试（core 零 IO，hostless）。

覆盖：测试树展平与 external_id 路径、三态映射、秒→毫秒、failureText 首行
截断、附件 mime 分型与同用例聚合、URN 回链、畸形/未知形态 fail-closed、
TestRun 归一身份透传。全部使用自造合成 JSON，零语料仓内容。
"""

from __future__ import annotations

import pytest

from testpilot_core.enums import ArtifactType, CaseStatus, RunStatus
from testpilot_core.errors import SchemaValidationError
from testpilot_core.models import CaseResult, TestExecutionRequest
from testpilot_core.xcresult_parse import (
    link_attachment_urns,
    parse_attachments,
    parse_test_nodes,
    test_run_from_xcresult as build_run_from_xcresult,
)


def _req() -> TestExecutionRequest:
    return TestExecutionRequest(
        request_id="req-1", idempotency_key="idem-1", project="ios-demo",
        selector={"test_ids": ["AlarmKitSpikeTests"]},
        commit_sha="9f8e7d6c", build_id="xcode-1007",
    )


def _case_node(name: str, result: str, duration: float | None = 0.1,
               failures: list[dict] | None = None) -> dict:
    node: dict = {"nodeType": "Test case", "name": name, "result": result}
    if duration is not None:
        node["duration"] = duration
    if failures is not None:
        node["failures"] = failures
    return node


def _tree() -> dict:
    return {
        "testNodes": [
            {
                "nodeType": "Unit test bundle",
                "name": "AlarmKitSpikeTests.xctest",
                "result": "Mixed",
                "duration": 1.2,
                "children": [
                    {
                        "nodeType": "Test Suite",
                        "name": "AlarmKitSpikeTests",
                        "result": "Mixed",
                        "children": [
                            _case_node("testPass()", "Passed", 0.03),
                            _case_node("testSkip()", "Skipped", None),
                            _case_node(
                                "testFail()",
                                "Failed",
                                0.4,
                                [{"failureText": "XCTAssertEqual failed: (1) != (2)\n"
                                  "AlarmKitSpikeTests.swift:42"}],
                            ),
                        ],
                    }
                ],
            }
        ]
    }


def test_parse_tree_flattens_cases_with_path_ids():
    cases = parse_test_nodes(_tree())
    assert [c.name for c in cases] == ["testPass()", "testSkip()", "testFail()"]
    base = "AlarmKitSpikeTests.xctest/AlarmKitSpikeTests"
    assert cases[0].external_id == f"{base}/testPass()"
    # node_id 定位到所属套件路径（不含自身）
    assert cases[0].node_id == base
    assert cases[0].status is CaseStatus.PASSED
    assert cases[1].status is CaseStatus.SKIPPED and cases[1].duration_ms is None
    assert cases[2].status is CaseStatus.FAILED


def test_duration_seconds_to_ms_rounding():
    cases = parse_test_nodes(_tree())
    assert cases[0].duration_ms == 30  # 0.03s
    assert cases[2].duration_ms == 400  # 0.4s


def test_failure_text_keeps_first_line_only_and_truncates_500():
    cases = parse_test_nodes(_tree())
    failed = cases[2]
    assert failed.message == "XCTAssertEqual failed: (1) != (2)"
    assert failed.stack_top == "XCTAssertEqual failed: (1) != (2)"
    long_line = "x" * 800
    tree = {"testNodes": [
        {"nodeType": "Test Suite", "name": "S", "result": "Failed", "children": [
            _case_node("testLong()", "Failed", 0.1,
                       [{"failureText": f"{long_line}\nsecond line"}])]}]}
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
            "nodeType": "UI test bundle", "name": "UITests.xctest", "result": "Passed",
            "children": [{
                "nodeType": "Test Suite", "name": "ProductFlowUITests", "result": "Passed",
                "children": [{
                    "nodeType": "Test Suite", "name": "LoginGroup", "result": "Passed",
                    "children": [_case_node("test01_login()", "Passed", 12.5)],
                }],
            }],
        }]
    }
    case = parse_test_nodes(tree)[0]
    assert case.external_id == (
        "UITests.xctest/ProductFlowUITests/LoginGroup/test01_login()"
    )
    assert case.duration_ms == 12500


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
    tree["testNodes"][0]["children"][0]["children"][0]["result"] = "Flaky"
    with pytest.raises(SchemaValidationError, match="result 未知"):
        parse_test_nodes(tree)


def test_unknown_container_result_fails_closed():
    tree = _tree()
    tree["testNodes"][0]["result"] = "Banana"
    with pytest.raises(SchemaValidationError):
        parse_test_nodes(tree)


def test_missing_name_fails_closed():
    tree = _tree()
    del tree["testNodes"][0]["children"][0]["children"][0]["name"]
    with pytest.raises(SchemaValidationError, match="缺少 name"):
        parse_test_nodes(tree)


def test_container_without_children_fails_closed():
    tree = _tree()
    del tree["testNodes"][0]["children"][0]["children"]
    with pytest.raises(SchemaValidationError, match="缺少 children"):
        parse_test_nodes(tree)


def test_bad_duration_fails_closed():
    tree = _tree()
    tree["testNodes"][0]["children"][0]["children"][0]["duration"] = "0.1s"
    with pytest.raises(SchemaValidationError, match="非法 duration"):
        parse_test_nodes(tree)
    tree2 = _tree()
    tree2["testNodes"][0]["children"][0]["children"][0]["duration"] = -1
    with pytest.raises(SchemaValidationError, match="不能为负"):
        parse_test_nodes(tree2)
    # bool 是 int 子类，明确拒绝防手滑
    tree3 = _tree()
    tree3["testNodes"][0]["children"][0]["children"][0]["duration"] = True
    with pytest.raises(SchemaValidationError):
        parse_test_nodes(tree3)


def test_non_failed_case_carrying_failures_is_contradiction():
    tree = _tree()
    tree["testNodes"][0]["children"][0]["children"][0]["failures"] = [
        {"failureText": "surprise"}
    ]
    with pytest.raises(SchemaValidationError, match="状态与证据矛盾"):
        parse_test_nodes(tree)


def test_failed_case_without_failure_text_allowed_evidence_in_raw_bundle():
    tree = _tree()
    failed = tree["testNodes"][0]["children"][0]["children"][2]
    failed["failures"] = []
    case = parse_test_nodes(tree)[2]
    assert case.status is CaseStatus.FAILED and case.message == ""


def test_malformed_failure_entry_fails_closed():
    tree = _tree()
    tree["testNodes"][0]["children"][0]["children"][2]["failures"] = [{"oops": True}]
    with pytest.raises(SchemaValidationError, match="failureText"):
        parse_test_nodes(tree)


# ── 附件清单 ────────────────────────────────────────────────────────────────


def _attachments_payload() -> dict:
    ref = "AlarmKitSpikeTests.xctest/AlarmKitSpikeTests/testFail()"
    return {
        "attachments": [
            {"case_ref": ref, "file_name": "Screenshot-1.png",
             "mime_type": "image/png"},
            {"case_ref": ref, "file_name": "Screenshot-2.jpg",
             "mime_type": "image/jpeg"},
            {"case_ref": ref, "file_name": "Recording.mp4",
             "mime_type": "video/mp4"},
            {"case_ref": f"{ref}x", "file_name": "note.txt",
             "mime_type": "text/plain"},
            {"case_ref": "other/case", "file_name": "blob.bin",
             "mime_type": "application/x-custom"},
        ]
    }


def test_parse_attachments_groups_by_case_and_maps_types():
    grouped = parse_attachments(_attachments_payload())
    ref = "AlarmKitSpikeTests.xctest/AlarmKitSpikeTests/testFail()"
    assert len(grouped[ref]) == 3
    assert [a.artifact_type for a in grouped[ref]] == [
        ArtifactType.SCREENSHOT, ArtifactType.SCREENSHOT, ArtifactType.VIDEO
    ]
    assert grouped[f"{ref}x"][0].artifact_type is ArtifactType.LOG_BUNDLE
    # 未登记 mime 落 other 但保留原名与原 mime，不丢证据
    unknown = grouped["other/case"][0]
    assert unknown.artifact_type is ArtifactType.OTHER
    assert unknown.mime_type == "application/x-custom"


def test_parse_attachments_mime_case_insensitive():
    grouped = parse_attachments(
        {"attachments": [
            {"case_ref": "c", "file_name": "a.PNG", "mime_type": "Image/PNG"}]}
    )
    assert grouped["c"][0].artifact_type is ArtifactType.SCREENSHOT


@pytest.mark.parametrize(
    "item",
    [
        {"file_name": "a.png", "mime_type": "image/png"},  # 缺 case_ref
        {"case_ref": "c", "mime_type": "image/png"},       # 缺 file_name
        {"case_ref": "c", "file_name": "a.png"},           # 缺 mime_type
        {"case_ref": "  ", "file_name": "a.png",
         "mime_type": "image/png"},                        # 空白 case_ref
    ],
)
def test_parse_attachments_malformed_entry_fails_closed(item):
    with pytest.raises(SchemaValidationError):
        parse_attachments({"attachments": [item]})


def test_parse_attachments_empty_fails_closed():
    with pytest.raises(SchemaValidationError):
        parse_attachments({"attachments": []})


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
    run = build_run_from_xcresult(cases, _req(), env_id="5411787C-UDID",
                                 capabilities=["ios.simulator"],
                                 uncovered_layers=["ios.device", "codesigning"])
    assert run.run_id == ""  # 解析层不发明身份
    assert run.idempotency_key == "idem-1"
    assert run.project == "ios-demo" and run.commit_sha == "9f8e7d6c"
    assert run.status is RunStatus.FAILED
    assert run.environment.env_id == "5411787C-UDID"
    assert run.environment.provider == "ios-simulator"
    assert run.uncovered_layers == ["ios.device", "codesigning"]
    assert run.runner_capability_version == "xcresulttool-16"
    assert len(run.case_results) == 3
    # 时长只统计有用期值的用例（skipped 为 None 不计）
    assert run.duration_ms == 430
    assert run.artifacts == [] and run.raw_report_ref is None


def test_test_run_from_xcresult_all_passed_and_artifacts_passthrough():
    from testpilot_core.models import ArtifactRef

    tree = {"testNodes": [
        {"nodeType": "Test Suite", "name": "S", "result": "Passed", "children": [
            _case_node("a()", "Passed", 0.01), _case_node("b()", "Passed", 0.02)]}]}
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
