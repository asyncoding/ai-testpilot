"""xcresulttool 测试结果 JSON 纯解析（docs/08 FR-EX-005，S3.3）。

对接 ``xcrun xcresulttool get test-results tests --format json --path X.xcresult``
（Xcode 16 形态）与 ``get test-results attachments --format json`` 的附件清单。
本模块零 IO、零三方——只吃 JSON 字符串/字典，进程调用与文件导出在适配器层
（adapters/xcresult_tool.py）。

夹具口径（自造合成形态，真实 xcresulttool 字段以手动探针实测校准，见
.agent-handoff/2026-11-10-s3.3-xcresult-oslog.md）：

.. code-block:: json

    {
      "testNodes": [
        {
          "nodeType": "Unit test bundle",
          "name": "AlarmKitSpikeTests.xctest",
          "result": "Mixed",
          "duration": 1.23,
          "children": [
            {
              "nodeType": "Test Suite",
              "name": "AlarmKitSpikeTests",
              "result": "Failed",
              "children": [
                {
                  "nodeType": "Test case",
                  "name": "testExample()",
                  "result": "Failed",
                  "duration": 0.4,
                  "failures": [{"failureText": "XCTAssert…\\nfile: …:12"}]
                }
              ]
            }
          ]
        }
      ]
    }

附件清单形态（适配器导出后回填 URN，解析层只归一类型/归属）：

.. code-block:: json

    {
      "attachments": [
        {"case_ref": "AlarmKitSpikeTests.xctest/AlarmKitSpikeTests/testExample()",
         "file_name": "Screenshot-1.png", "mime_type": "image/png"}
      ]
    }

纪律：未知 nodeType/result、缺键、非法时长一律 :class:`SchemaValidationError`
fail-closed，绝不静默猜状态（docs/07 证据驱动）。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel, Field

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

_CONTAINER_NODE_TYPES = frozenset(
    {"Unit test bundle", "UI test bundle", "Test Suite", "Test plan"}
)
_CASE_NODE_TYPE = "Test case"
_CASE_RESULT_MAP = {
    "Passed": CaseStatus.PASSED,
    "Failed": CaseStatus.FAILED,
    "Skipped": CaseStatus.SKIPPED,
}
# 容器节点允许 Mixed（子树有失败），用例节点不允许
_CONTAINER_RESULTS = frozenset({"Passed", "Failed", "Skipped", "Mixed"})
# mime → 制品类型；未登记的 mime 落 other 但原样保留，不丢证据
_MIME_TO_ARTIFACT = {
    "image/png": ArtifactType.SCREENSHOT,
    "image/jpeg": ArtifactType.SCREENSHOT,
    "video/mp4": ArtifactType.VIDEO,
    "video/quicktime": ArtifactType.VIDEO,
    "text/plain": ArtifactType.LOG_BUNDLE,
    "application/json": ArtifactType.LOG_BUNDLE,
}


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
    """单条附件记录的解析中间态（URN 在适配器 put 之后才有）。"""

    case_ref: str
    file_name: str
    mime_type: str
    artifact_type: ArtifactType


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


def _to_ms(duration: Any) -> int | None:
    if duration is None:
        return None
    if isinstance(duration, bool) or not isinstance(duration, (int, float)):
        raise SchemaValidationError(f"非法 duration: {duration!r}")
    if duration < 0:
        raise SchemaValidationError(f"duration 不能为负: {duration!r}")
    return round(float(duration) * 1000)


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


def _parse_case_node(node: dict[str, Any], path: tuple[str, ...]) -> XcresultCase:
    name = _name_of(node)
    result = node.get("result")
    if result not in _CASE_RESULT_MAP:
        raise SchemaValidationError(
            f"用例节点 {name} 的 result 未知: {result!r}（合法: "
            f"{sorted(_CASE_RESULT_MAP)}）"
        )
    failures = node.get("failures", [])
    if not isinstance(failures, list):
        raise SchemaValidationError(f"用例 {name} 的 failures 应为数组")
    if result != "Failed" and failures:
        # 非失败用例挂 failureText 是矛盾信号：静默丢弃会丢证据，fail-closed
        raise SchemaValidationError(
            f"用例 {name} result={result} 却携带 failures，状态与证据矛盾"
        )
    message = stack_top = ""
    if failures:
        first = failures[0]
        if not isinstance(first, dict) or not isinstance(
            first.get("failureText"), str
        ):
            raise SchemaValidationError(
                f"用例 {name} 的首条 failure 缺少 failureText 字符串"
            )
        message = _first_text_line(first["failureText"])
        stack_top = _first_text_line(first["failureText"])
    full_path = (*path, name)
    return XcresultCase(
        external_id="/".join(full_path),
        node_id="/".join(path),
        name=name,
        status=_CASE_RESULT_MAP[result],
        duration_ms=_to_ms(node.get("duration")),
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
    if result not in _CONTAINER_RESULTS:
        raise SchemaValidationError(
            f"容器节点 {name} 的 result 未知: {result!r}"
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


def parse_attachments(payload: str | dict[str, Any]) -> dict[str, list[XcresultAttachment]]:
    """附件清单 JSON → ``case_ref -> 附件列表``（同用例多附件保序聚合）。"""
    data = _load_json(payload)
    raw = data.get("attachments")
    if not isinstance(raw, list) or not raw:
        raise SchemaValidationError("附件清单为空或缺少 attachments 数组")
    grouped: dict[str, list[XcresultAttachment]] = {}
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise SchemaValidationError(f"附件 #{index} 应为对象")
        case_ref = item.get("case_ref")
        file_name = item.get("file_name")
        mime_type = item.get("mime_type")
        if not isinstance(case_ref, str) or not case_ref.strip():
            raise SchemaValidationError(f"附件 #{index} 缺少 case_ref")
        if not isinstance(file_name, str) or not file_name.strip():
            raise SchemaValidationError(f"附件 #{index} 缺少 file_name")
        if not isinstance(mime_type, str) or not mime_type.strip():
            raise SchemaValidationError(f"附件 #{index} 缺少 mime_type")
        mime_type = mime_type.strip().lower()
        grouped.setdefault(case_ref.strip(), []).append(
            XcresultAttachment(
                case_ref=case_ref.strip(),
                file_name=file_name.strip(),
                mime_type=mime_type,
                # 未登记 mime 落 other，原件仍导出留证，不静默丢弃
                artifact_type=_MIME_TO_ARTIFACT.get(mime_type, ArtifactType.OTHER),
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
    runner_capability_version: str = "xcresulttool-16",
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
