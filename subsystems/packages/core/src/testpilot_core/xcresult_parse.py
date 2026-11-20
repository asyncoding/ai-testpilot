"""xcresulttool 测试结果 JSON 纯解析（docs/08 FR-EX-005，S3.3）。

对接 ``xcrun xcresulttool get test-results tests --format json --path X.xcresult``
与 ``export attachments`` 产出的 manifest.json。本模块零 IO、零三方——只吃
JSON 字符串/字典，进程调用与文件导出在适配器层（adapters/xcresult_tool.py）。

字段口径以 **2026-11-10 真实探针校准**为准（Xcode 27 / xcresulttool 25115，
官方 ``--schema`` 0.4.0 + 真跑 AlarmKitSpike 6 用例全绿 run，见
.agent-handoff/2026-11-10-s3-real-device-probe.md）：

.. code-block:: json

    {
      "testNodes": [
        {
          "nodeType": "Test Plan",
          "name": "AlarmKitSpike",
          "result": "Passed",
          "children": [
            {
              "nodeType": "Unit test bundle",
              "name": "AlarmKitSpikeTests",
              "result": "Passed",
              "children": [
                {
                  "nodeType": "Test Suite",
                  "name": "HomeListBuilderTests",
                  "result": "Passed",
                  "children": [
                    {
                      "nodeType": "Test Case",
                      "name": "testExample()",
                      "result": "Failed",
                      "duration": "0.401s",
                      "durationInSeconds": 0.401,
                      "children": [
                        {"nodeType": "Failure Message",
                         "name": "XCTAssert…", "details": "XCTAssert…\\nfile: …:12"}
                      ]
                    }
                  ]
                }
              ]
            }
          ]
        }
      ]
    }

附件 manifest 形态（``export attachments`` 输出目录内 manifest.json，**顶层
数组**；官方 schema 无 mime 字段，类型按导出文件名扩展名推断）：

.. code-block:: json

    [
      {"testIdentifier": "HomeListBuilderTests/testExample()",
       "testIdentifierURL": "test://…",
       "attachments": [
         {"exportedFileName": "Screenshot-1.png",
          "suggestedHumanReadableName": "…", "isAssociatedWithFailure": true,
          "configurationName": "…", "deviceName": "…", "deviceId": "…"}
       ]}
    ]

纪律：未知 nodeType/result、缺键、非法时长一律 :class:`SchemaValidationError`
fail-closed，绝不静默猜状态（docs/01 证据驱动）。官方 18 种 nodeType 中本模块
只放行真实 run 已观测/有 schema 证据的五类，其余（Repetition/Attachment/…）
等真跑观测后再校准，不提前实现。
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel

from .enums import ArtifactType, CaseStatus
from .errors import SchemaValidationError
from .execution import run_outcome
from .models import (
    ArtifactRef,
    CaseResult,
    EnvironmentInfo,
    TestExecutionRequest,
    TestRun,
)

# 真实 run 已观测的容器节点（官方 TestNodeType 18 枚举的子集，余者 fail-closed）
_CONTAINER_NODE_TYPES = frozenset(
    {"Test Plan", "Unit test bundle", "UI test bundle", "Test Suite"}
)
_CASE_NODE_TYPE = "Test Case"
_FAILURE_MESSAGE_NODE_TYPE = "Failure Message"
_CASE_RESULT_MAP = {
    "Passed": CaseStatus.PASSED,
    "Failed": CaseStatus.FAILED,
    "Skipped": CaseStatus.SKIPPED,
    # XCTExpectFailure：XCTest 语义上不红 bundle，映射 failed 会制造假缺陷信号
    "Expected Failure": CaseStatus.SKIPPED,
}
# 官方 schema 0.4.0 TestResult 全集；"unknown" 不在映射表，走 fail-closed
_KNOWN_RESULTS = frozenset(
    {"Passed", "Failed", "Skipped", "Expected Failure", "unknown"}
)
# 导出文件扩展名 → 制品类型；未登记扩展名落 other 但原样保留，不丢证据
_EXT_TO_ARTIFACT = {
    ".png": ArtifactType.SCREENSHOT,
    ".jpg": ArtifactType.SCREENSHOT,
    ".jpeg": ArtifactType.SCREENSHOT,
    ".mp4": ArtifactType.VIDEO,
    ".mov": ArtifactType.VIDEO,
    ".txt": ArtifactType.LOG_BUNDLE,
    ".log": ArtifactType.LOG_BUNDLE,
    ".json": ArtifactType.LOG_BUNDLE,
}
# human-readable duration 分量（schema：days/hours/minutes/seconds 可选分量）
_DURATION_PART_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([dhms])")
_DURATION_FULL_RE = re.compile(r"(?:\s*\d+(?:\.\d+)?[dhms]\s*)+")
_DURATION_MULTIPLIER = {"d": 86400, "h": 3600, "m": 60, "s": 1}


class XcresultCase(BaseModel):
    """单个 Test case 节点的解析中间态（附件 URN 由适配器回填）。"""

    external_id: str
    node_id: str
    name: str
    status: CaseStatus
    duration_ms: int | None = None
    message: str = ""
    stack_top: str = ""


class XcresultAttachment(BaseModel):
    """单条附件记录的解析中间态（URN 在适配器 put 之后才有）。

    对齐 xcresulttool 25115 ``export attachments`` 的 manifest.json 形态：
    无 mime 字段，类型按导出文件名扩展名推断；归属用我方 external_case_id
    （manifest 的 testIdentifier 无 bundle 前缀，已在解析时做后缀匹配）。
    """

    external_case_id: str
    file_name: str
    artifact_type: ArtifactType
    is_failure_associated: bool = False


def _load_json(payload: str | dict[str, Any]) -> dict[str, Any]:
    if isinstance(payload, str):
        if not payload.strip():
            raise SchemaValidationError("xcresulttool JSON 为空")
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise SchemaValidationError(f"xcresulttool JSON 解析失败: {exc}") from exc
    else:
        data = payload
    if not isinstance(data, dict):
        raise SchemaValidationError(
            f"xcresulttool 顶层应为对象，实际: {type(data).__name__}"
        )
    return data


def _parse_human_duration(text: str) -> float:
    """解析 schema 0.4.0 的 human-readable duration（实测形如 ``0.0013s``，
    文档允许 d/h/m/s 分量组合，如 ``1m 2.3s``）。"""
    text = text.strip()
    if not _DURATION_FULL_RE.fullmatch(text):
        raise SchemaValidationError(f"非法 duration 字符串: {text!r}")
    total = 0.0
    for value, unit in _DURATION_PART_RE.findall(text):
        total += float(value) * _DURATION_MULTIPLIER[unit]
    return total


def _node_duration_ms(node: dict[str, Any]) -> int | None:
    """时长双源：优先数值 ``durationInSeconds``（权威），缺失时回退解析
    ``duration`` 字符串（真实节点两者并存，回退路径保 schema 兼容）。"""
    numeric = node.get("durationInSeconds")
    if numeric is None:
        raw = node.get("duration")
        if raw is None:
            return None
        if isinstance(raw, bool) or not isinstance(raw, str):
            raise SchemaValidationError(f"非法 duration: {raw!r}")
        numeric = _parse_human_duration(raw)
    if isinstance(numeric, bool) or not isinstance(numeric, (int, float)):
        raise SchemaValidationError(f"非法 durationInSeconds: {numeric!r}")
    if numeric < 0:
        raise SchemaValidationError(f"duration 不能为负: {numeric!r}")
    return round(float(numeric) * 1000)


def _first_text_line(text: str) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line:
            # 栈顶/消息只留首行 500 字，完整 failureText 属于 xcresult 原始制品
            return line[:500]
    return ""


def _name_of(node: dict[str, Any]) -> str:
    name = node.get("name")
    if not isinstance(name, str) or not name.strip():
        raise SchemaValidationError(
            f"节点缺少 name: nodeType={node.get('nodeType')!r}"
        )
    return name.strip()


def _failure_messages(
    node: dict[str, Any], name: str, result: str
) -> list[str]:
    """从用例节点的 Failure Message 子节点提取失败文本（schema 0.4.0 形态）。

    官方 TestNode 没有 failures 数组，失败证据是 nodeType="Failure Message"
    的子节点（文本在 details，回退 name）。用例叶子下任何其他子节点类型都未
    经真实 run 校准，fail-closed 等观测后再放行。
    """
    children = node.get("children", []) or []
    messages: list[str] = []
    for child in children:
        if not isinstance(child, dict):
            raise SchemaValidationError(
                f"用例 {name} 的子节点应为对象，实际: {type(child).__name__}"
            )
        child_type = child.get("nodeType")
        if child_type != _FAILURE_MESSAGE_NODE_TYPE:
            raise SchemaValidationError(
                f"用例 {name} 下出现未校准的子节点类型: {child_type!r}"
                "（需真实 run 观测后再放行）"
            )
        text = child.get("details") or child.get("name")
        if not isinstance(text, str) or not text.strip():
            raise SchemaValidationError(
                f"用例 {name} 的 Failure Message 缺少 details/name 文本"
            )
        messages.append(text)
    # 非失败用例挂失败证据是矛盾信号：静默丢弃会丢证据，fail-closed
    # （Expected Failure 按 XCTest 语义本就携带失败消息，属合法）
    if messages and result not in ("Failed", "Expected Failure"):
        raise SchemaValidationError(
            f"用例 {name} result={result} 却携带 Failure Message，状态与证据矛盾"
        )
    return messages


def _parse_case_node(node: dict[str, Any], path: tuple[str, ...]) -> XcresultCase:
    name = _name_of(node)
    result = node.get("result")
    if result not in _CASE_RESULT_MAP:
        if result in _KNOWN_RESULTS:
            raise SchemaValidationError(
                f"用例节点 {name} 的 result={result!r} 属官方枚举但语义未校准，"
                "拒绝静默映射（需人工确认真实 run 含义）"
            )
        raise SchemaValidationError(
            f"用例节点 {name} 的 result 未知: {result!r}（合法: "
            f"{sorted(_KNOWN_RESULTS)}）"
        )
    messages = _failure_messages(node, name, result)
    message = stack_top = ""
    if messages and result == "Failed":
        # Expected Failure 映射 skipped，不携带失败消息（不是缺陷信号）
        message = _first_text_line(messages[0])
        stack_top = message
    full_path = (*path, name)
    return XcresultCase(
        external_id="/".join(full_path),
        node_id="/".join(path),
        name=name,
        status=_CASE_RESULT_MAP[result],
        duration_ms=_node_duration_ms(node),
        message=message,
        stack_top=stack_top,
    )


def _walk(node: dict[str, Any], path: tuple[str, ...], out: list[XcresultCase]) -> None:
    if not isinstance(node, dict):
        raise SchemaValidationError(f"测试节点应为对象，实际: {type(node).__name__}")
    node_type = node.get("nodeType")
    name = _name_of(node)
    if node_type == _CASE_NODE_TYPE:
        out.append(_parse_case_node(node, path))
        return
    if node_type not in _CONTAINER_NODE_TYPES:
        raise SchemaValidationError(
            f"未知 nodeType: {node_type!r}（节点 {name}）；真实新形态需先校准再放行"
        )
    result = node.get("result")
    if result not in _KNOWN_RESULTS or result == "unknown":
        raise SchemaValidationError(
            f"容器节点 {name} 的 result 非法或未校准: {result!r}"
        )
    children = node.get("children", [])
    if not isinstance(children, list) or not children:
        raise SchemaValidationError(f"容器节点 {name} 缺少 children")
    child_path = (*path, name)
    for child in children:
        _walk(child, child_path, out)


def parse_test_nodes(payload: str | dict[str, Any]) -> list[XcresultCase]:
    """xcresulttool tests JSON → 展平的用例中间态列表（纯函数）。"""
    data = _load_json(payload)
    roots = data.get("testNodes")
    if not isinstance(roots, list) or not roots:
        raise SchemaValidationError("报告中没有任何 testNodes")
    cases: list[XcresultCase] = []
    for root in roots:
        _walk(root, (), cases)
    if not cases:
        raise SchemaValidationError("testNodes 存在但未解析出任何 Test case 叶子")
    return cases


def _artifact_from_filename(file_name: str) -> ArtifactType:
    """官方 manifest 无 mime 字段，按导出文件名扩展名推断制品类型。"""
    suffix = file_name[file_name.rfind(".") :].lower() if "." in file_name else ""
    # 未登记扩展名落 other，原件仍导出留证，不静默丢弃
    return _EXT_TO_ARTIFACT.get(suffix, ArtifactType.OTHER)


def parse_attachment_manifest(
    payload: str | list[Any],
    case_external_ids: Sequence[str],
) -> dict[str, list[XcresultAttachment]]:
    """解析 ``export attachments`` 的 manifest.json（顶层数组）→
    ``external_case_id -> 附件列表``（同用例多附件保序聚合）。

    manifest 的 ``testIdentifier`` 无 bundle 前缀
    （``HomeListBuilderTests/testX()``），我方 external_case_id 带 bundle
    （``AlarmKitSpikeTests/HomeListBuilderTests/testX()``），用等值或后缀
    匹配归属；零匹配/多匹配都 fail-closed——归属猜不得。
    """
    if isinstance(payload, str):
        if not payload.strip():
            raise SchemaValidationError("attachment manifest 为空")
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise SchemaValidationError(
                f"attachment manifest JSON 解析失败: {exc}"
            ) from exc
    else:
        data = payload
    if not isinstance(data, list):
        raise SchemaValidationError(
            f"attachment manifest 顶层应为数组，实际: {type(data).__name__}"
        )
    grouped: dict[str, list[XcresultAttachment]] = {}
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            raise SchemaValidationError(f"manifest 条目 #{index} 应为对象")
        test_identifier = item.get("testIdentifier")
        if not isinstance(test_identifier, str) or not test_identifier.strip():
            raise SchemaValidationError(
                f"manifest 条目 #{index} 缺少 testIdentifier"
            )
        test_identifier = test_identifier.strip()
        matches = [
            case_id
            for case_id in case_external_ids
            if case_id == test_identifier
            or case_id.endswith("/" + test_identifier)
        ]
        if len(matches) != 1:
            raise SchemaValidationError(
                f"manifest testIdentifier={test_identifier!r} 归属匹配 "
                f"{len(matches)} 条（须恰好 1 条），候选={matches}"
            )
        raw_attachments = item.get("attachments")
        if not isinstance(raw_attachments, list) or not raw_attachments:
            raise SchemaValidationError(
                f"manifest 条目 #{index} 缺少非空 attachments 数组"
            )
        for att_index, att in enumerate(raw_attachments):
            if not isinstance(att, dict):
                raise SchemaValidationError(
                    f"manifest 条目 #{index} 附件 #{att_index} 应为对象"
                )
            file_name = att.get("exportedFileName")
            if not isinstance(file_name, str) or not file_name.strip():
                raise SchemaValidationError(
                    f"manifest 条目 #{index} 附件 #{att_index} 缺少 exportedFileName"
                )
            grouped.setdefault(matches[0], []).append(
                XcresultAttachment(
                    external_case_id=matches[0],
                    file_name=file_name.strip(),
                    artifact_type=_artifact_from_filename(file_name.strip()),
                    is_failure_associated=bool(
                        att.get("isAssociatedWithFailure", False)
                    ),
                )
            )
    return grouped


def link_attachment_urns(
    case_results: Sequence[CaseResult],
    urns_by_external_id: Mapping[str, Sequence[str]],
) -> list[CaseResult]:
    """把适配器导出后的 URN 挂回对应用例（纯函数；未知 id 不发明用例）。

    返回新列表（``model_copy``），不改输入；URN 顺序即附件导出顺序。
    """
    linked: list[CaseResult] = []
    for case in case_results:
        urns = urns_by_external_id.get(case.external_case_id, [])
        linked.append(
            case.model_copy(update={"artifact_urns": list(urns)}) if urns else case
        )
    return linked


def test_run_from_xcresult(
    cases: Sequence[XcresultCase],
    request: TestExecutionRequest,
    *,
    env_id: str,
    provider: str = "ios-simulator",
    capabilities: list[str] | None = None,
    version: str = "",
    artifacts: list[ArtifactRef] | None = None,
    raw_report_ref: str | None = None,
    uncovered_layers: list[str] | None = None,
    runner_capability_version: str = "xcresulttool-schema-0.4.0",
) -> TestRun:
    """解析中间态 → 语言中立 :class:`TestRun`（身份字段取自本次请求）。"""
    case_results = [
        CaseResult(
            external_case_id=case.external_id,
            node_id=case.node_id,
            status=case.status,
            duration_ms=case.duration_ms,
            message=case.message,
            stack_top=case.stack_top,
        )
        for case in cases
    ]
    durations = [c.duration_ms for c in case_results if c.duration_ms is not None]
    return TestRun(
        run_id="",  # 由适配器按 run_id_for(idempotency_key) 填，解析层不发明身份
        idempotency_key=request.idempotency_key,
        project=request.project,
        commit_sha=request.commit_sha,
        build_id=request.build_id,
        status=run_outcome(case_results),
        environment=EnvironmentInfo(
            provider=provider,
            env_id=env_id,
            capabilities=capabilities or [],
            version=version,
        ),
        duration_ms=sum(durations) if durations else None,
        case_results=case_results,
        artifacts=artifacts or [],
        raw_report_ref=raw_report_ref,
        runner_capability_version=runner_capability_version,
        # 模拟器跑不到的层（真机/签名/后台音频…）由适配器按请求能力标签显式声明
        uncovered_layers=uncovered_layers or [],
    )
