"""S3.1 FR-EX-001 AC2 Surefire XML 纯解析测试（core 零 IO，hostless）。

覆盖：四态映射（pass/failure/skipped/error）、testsuites 聚合、秒→毫秒、
失败证据字段、空报告→run error、六类畸形输入 fail-closed、请求身份透传。
"""

from __future__ import annotations

import pytest

from testpilot_core.enums import CaseStatus, RunStatus
from testpilot_core.errors import SchemaValidationError
from testpilot_core.junit_report import (
    parse_surefire_xml,
    test_run_from_junit as build_run_from_junit,
)
from testpilot_core.models import CaseSelector, TestExecutionRequest


def _req() -> TestExecutionRequest:
    return TestExecutionRequest(
        request_id="req-1", idempotency_key="idem-1", project="java-demo",
        selector=CaseSelector(test_ids=["com.example.CalcTest"]),
        commit_sha="9f8e7d6c", build_id="jenkins-1007",
    )


_SUITE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="com.example.CalcTest" tests="4" failures="1" errors="1" skipped="1" time="0.123">
  <testcase name="add" classname="com.example.CalcTest" time="0.031"/>
  <testcase name="divideByZero" classname="com.example.CalcTest" time="0.002">
    <skipped message="not on JDK 21"/>
  </testcase>
  <testcase name="subtract" classname="com.example.CalcTest" time="0.05">
    <failure message="expected:&lt;1&gt; but was:&lt;2&gt;" type="AssertionError">at com.example.CalcTest.subtract(CalcTest.java:42)
at sun.reflect.NativeMethodAccessorImpl.invoke0(Native Method)</failure>
  </testcase>
  <testcase name="ioBoom" classname="com.example.CalcTest" time="0.04">
    <error type="java.io.IOException" message="disk full"/>
  </testcase>
</testsuite>"""


def test_parse_full_suite_four_statuses():
    suites = parse_surefire_xml(_SUITE_XML)
    assert len(suites) == 1
    suite = suites[0]
    assert suite.name == "com.example.CalcTest"
    assert suite.duration_ms == 123
    by_id = {c.external_id: c for c in suite.cases}
    assert by_id["com.example.CalcTest#add"].status == CaseStatus.PASSED
    assert by_id["com.example.CalcTest#add"].duration_ms == 31
    skipped = by_id["com.example.CalcTest#divideByZero"]
    assert skipped.status == CaseStatus.SKIPPED and skipped.message == "not on JDK 21"
    failed = by_id["com.example.CalcTest#subtract"]
    assert failed.status == CaseStatus.FAILED
    assert failed.failure_type == "AssertionError"
    assert failed.message == "expected:<1> but was:<2>"
    assert failed.stack_top.startswith("at com.example.CalcTest.subtract")
    errored = by_id["com.example.CalcTest#ioBoom"]
    assert errored.status == CaseStatus.ERROR and errored.failure_type == "java.io.IOException"


def test_build_test_run_is_language_neutral_contract():
    run = build_run_from_junit(
        parse_surefire_xml(_SUITE_XML), _req(),
        env_id="openjdk-21", version="JUnit 5.11 / Surefire 3.5",
        source_name="TEST-com.example.CalcTest.xml",
    )
    assert run.status == RunStatus.FAILED  # 含 failure/error 用例
    assert run.idempotency_key == "idem-1"
    assert run.project == "java-demo" and run.commit_sha == "9f8e7d6c"
    assert run.build_id == "jenkins-1007"
    assert run.environment.provider == "junit-local"
    assert run.environment.env_id == "openjdk-21"
    assert run.duration_ms == 123
    assert all(c.node_id == "TEST-com.example.CalcTest.xml" for c in run.case_results)
    assert run.run_id == ""  # 身份由适配器填，解析层不发明


def test_testsuites_wrapper_aggregates():
    xml = """<testsuites>
      <testsuite name="A" time="0.1"><testcase name="a1" classname="A" time="0.1"/></testsuite>
      <testsuite name="B" time="0.2">
        <testcase name="b1" classname="B" time="0.2">
          <failure type="AssertionError" message="boom"/>
        </testcase>
      </testsuite>
    </testsuites>"""
    suites = parse_surefire_xml(xml)
    assert [s.name for s in suites] == ["A", "B"]
    run = build_run_from_junit(suites, _req(), env_id="jdk")
    assert run.status == RunStatus.FAILED
    assert run.duration_ms == 300
    assert len(run.case_results) == 2


def test_zero_cases_maps_to_run_error():
    suites = parse_surefire_xml('<testsuite name="Empty" tests="0" time="0"/>')
    run = build_run_from_junit(suites, _req(), env_id="jdk")
    assert run.status == RunStatus.ERROR
    assert run.case_results == []


def test_trace_only_failure_uses_first_line_as_message():
    xml = """<testsuite name="X">
      <testcase name="x1" classname="X" time="0.01">
        <failure type="AssertionError">line one
line two</failure>
      </testcase>
    </testsuite>"""
    case = parse_surefire_xml(xml)[0].cases[0]
    assert case.message == "line one" and case.stack_top == "line one"


@pytest.mark.parametrize(
    "xml",
    [
        "",
        "   \n  ",
        "<not-a-suite/>",
        "<testsuites></testsuites>",
        "<testsuite><testcase classname='X'/></testsuite>",  # 缺 name
        # 未知子元素 fail-closed
        "<testsuite><testcase name='a' classname='X'><weird/></testcase></testsuite>",
        # 状态子元素互斥
        "<testsuite><testcase name='a' classname='X'>"
        "<failure type='E'/><error type='E'/></testcase></testsuite>",
        "<testsuite><testcase name='a' classname='X' time='not-a-number'/></testsuite>",
        "<<<not xml>>>",
    ],
)
def test_malformed_inputs_fail_closed(xml):
    with pytest.raises(SchemaValidationError):
        parse_surefire_xml(xml)
