"""Surefire/JUnit XML 纯解析（docs/08 FR-EX-001 AC2，S3.1）。

语言中立执行契约的另一半证据：同一份 :class:`TestRun` 模型既能装 xcode 结果，
也能装 JVM 技术栈的 Surefire 报告。本模块零 IO、零三方——只用 stdlib
``xml.etree.ElementTree`` 把 XML 文本解析成核心模型，文件读取在适配器层
（adapters/junit_runner.py）。

Surefire 方言要点（实测口径，S5 接真实 Jenkins 产物时再校准）：

- 根元素可以是 ``<testsuite>``（单类一个文件，Surefire 默认）或
  ``<testsuites>``（聚合报告，包多个 suite）；
- ``<testcase>`` 无子元素=通过；``<skipped>/<failure>/<error>`` 三种失败子元素
  互斥，出现未知子元素按畸形处理（fail-closed，不静默猜状态）；
- 时间属性 ``time`` 单位是秒（小数），核心模型统一存毫秒。
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Sequence

from pydantic import BaseModel, Field

from .enums import CaseStatus
from .errors import SchemaValidationError
from .execution import run_outcome
from .models import (
    CaseResult,
    EnvironmentInfo,
    TestExecutionRequest,
    TestRun,
)

# testcase 合法的状态子元素（system-out 等只允许挂在 suite 级）
_STATUS_CHILDREN = frozenset({"skipped", "failure", "error"})
_CHILD_TO_STATUS = {
    "skipped": CaseStatus.SKIPPED,
    "failure": CaseStatus.FAILED,
    "error": CaseStatus.ERROR,
}


class JUnitCase(BaseModel):
    """单个 testcase 的解析中间态（适配器/测试可直接断言结构）。"""

    external_id: str
    classname: str
    name: str
    status: CaseStatus
    duration_ms: int | None = None
    failure_type: str | None = None
    message: str = ""
    stack_top: str = ""


class JUnitSuite(BaseModel):
    """一个 <testsuite> 的解析结果。"""

    name: str
    duration_ms: int | None = None
    cases: list[JUnitCase] = Field(default_factory=list)


def _to_ms(time_text: str | None) -> int | None:
    if time_text is None or time_text == "":
        return None
    try:
        return round(float(time_text) * 1000)
    except ValueError as exc:
        raise SchemaValidationError(f"非法 time 属性: {time_text!r}") from exc


def _first_trace_line(text: str | None) -> str:
    if not text:
        return ""
    for line in text.splitlines():
        line = line.strip()
        if line:
            # 栈顶只留首行，完整 trace 属于原始报告制品，不进结构化字段
            return line[:500]
    return ""


def _parse_testcase(element: ET.Element) -> JUnitCase:
    name = element.get("name", "").strip()
    classname = element.get("classname", "").strip()
    if not name:
        raise SchemaValidationError("testcase 缺少 name 属性")
    status_children = [c for c in element if c.tag in _STATUS_CHILDREN]
    unknown = [c for c in element if c.tag not in _STATUS_CHILDREN]
    if unknown:
        raise SchemaValidationError(
            f"testcase {classname}#{name} 含未知子元素: {[c.tag for c in unknown]}"
        )
    if len(status_children) > 1:
        raise SchemaValidationError(
            f"testcase {classname}#{name} 的状态子元素互斥，实际: "
            f"{[c.tag for c in status_children]}"
        )

    if not status_children:
        return JUnitCase(
            external_id=f"{classname}#{name}" if classname else name,
            classname=classname,
            name=name,
            status=CaseStatus.PASSED,
            duration_ms=_to_ms(element.get("time")),
        )

    child = status_children[0]
    status = _CHILD_TO_STATUS[child.tag]
    message = (child.get("message") or "").strip()
    trace = (child.text or "").strip()
    if not message and trace:
        # 部分方言只写 trace 不写 message 属性：用首行兜底，保证证据不丢
        message = trace.splitlines()[0].strip()[:500]
    return JUnitCase(
        external_id=f"{classname}#{name}" if classname else name,
        classname=classname,
        name=name,
        status=status,
        duration_ms=_to_ms(element.get("time")),
        failure_type=child.get("type"),
        message=message,
        stack_top=_first_trace_line(trace),
    )


def _parse_suite(element: ET.Element) -> JUnitSuite:
    cases = [_parse_testcase(tc) for tc in element.findall("testcase")]
    return JUnitSuite(
        name=element.get("name", ""),
        duration_ms=_to_ms(element.get("time")),
        cases=cases,
    )


def parse_surefire_xml(xml_text: str) -> list[JUnitSuite]:
    """XML 文本 → suite 列表（纯函数；畸形输入统一抛 SchemaValidationError）。"""
    if not xml_text or not xml_text.strip():
        raise SchemaValidationError("Surefire XML 为空")
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise SchemaValidationError(f"Surefire XML 解析失败: {exc}") from exc
    if root.tag == "testsuite":
        suite_elements = [root]
    elif root.tag == "testsuites":
        suite_elements = root.findall("testsuite")
    else:
        raise SchemaValidationError(
            f"根元素应为 testsuite/testsuites，实际: {root.tag!r}"
        )
    if not suite_elements:
        raise SchemaValidationError("报告中没有任何 testsuite")
    return [_parse_suite(e) for e in suite_elements]


def test_run_from_junit(
    suites: Sequence[JUnitSuite],
    request: TestExecutionRequest,
    *,
    env_id: str,
    provider: str = "junit-local",
    capabilities: list[str] | None = None,
    version: str = "",
    source_name: str = "",
    runner_capability_version: str = "junit-xml",
) -> TestRun:
    """解析中间态 → 语言中立 :class:`TestRun`（身份字段取自本次请求）。

    ``source_name`` 是 XML 文件名（适配器层知道），写入 node_id 与栈定位同源。
    """
    case_results: list[CaseResult] = []
    for suite in suites:
        for case in suite.cases:
            case_results.append(
                CaseResult(
                    external_case_id=case.external_id,
                    node_id=source_name,
                    status=case.status,
                    duration_ms=case.duration_ms,
                    failure_type=case.failure_type,
                    message=case.message,
                    stack_top=case.stack_top,
                )
            )
    durations = [s.duration_ms for s in suites if s.duration_ms is not None]
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
        runner_capability_version=runner_capability_version,
        # JVM 本地执行不声称任何 iOS 层覆盖；未覆盖层由调用方按场景补充
        uncovered_layers=[],
    )
