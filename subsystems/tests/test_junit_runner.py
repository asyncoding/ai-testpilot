"""S3.1 FR-EX-001 JUnitXmlRunner 适配器契约测试（hostless，只读合成夹具）。

覆盖：真实 Surefire XML → 语言中立 TestRun 全字段、与 xcode 录制器同模型契约、
fixture_hint 点名/单文件自动发现/多文件 fail-closed、畸形与缺失的错误分型、
取消语义、factory 的 junit-xml 装配。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from testpilot_adapters.junit_runner import JUnitXmlRunner
from testpilot_adapters.recorded_runner import RecordedTestRunner
from testpilot_core.config_schema import AdapterRef
from testpilot_core.enums import ArtifactType, CaseStatus, RunStatus
from testpilot_core.errors import AdapterUnavailableError, SchemaValidationError
from testpilot_core.execution import run_id_for
from testpilot_core.models import CaseSelector, EnvironmentRequirements, TestExecutionRequest, TestRun
from testpilot_orchestrator.config_loader import load_profile
from testpilot_orchestrator.factory import build_container

FIXTURES = Path(__file__).parent / "fixtures"
SUBSYSTEMS = Path(__file__).resolve().parents[1]


def _req(hint: str | None = "TEST-com.example.CalcTest", **over) -> TestExecutionRequest:
    kwargs = dict(
        request_id="req-j1", idempotency_key="idem-j1", project="java-demo",
        selector=CaseSelector(test_ids=["com.example.CalcTest"], fixture_hint=hint),
        commit_sha="9f8e7d6c", build_id="jenkins-1007",
        environment=EnvironmentRequirements(capabilities=["jvm", "junit5"]),
    )
    kwargs.update(over)
    return TestExecutionRequest(**kwargs)


async def test_junit_runner_parses_synthetic_surefire():
    runner = JUnitXmlRunner(FIXTURES / "runs" / "surefire")
    run_id = await runner.submit(_req())
    assert run_id == run_id_for("idem-j1")
    run = await runner.get_run(run_id)
    assert isinstance(run, TestRun)
    assert run.status == RunStatus.FAILED
    assert run.environment.provider == "junit-local"
    assert run.environment.capabilities == []  # 默认构造不声称能力
    by_id = {c.external_case_id: c for c in run.case_results}
    assert len(by_id) == 5
    assert by_id["com.example.CalcTest#add"].status == CaseStatus.PASSED
    assert by_id["com.example.CalcTest#multiply"].status == CaseStatus.PASSED
    failed = by_id["com.example.CalcTest#subtract"]
    assert failed.status == CaseStatus.FAILED
    assert failed.failure_type == "AssertionError"
    assert "expected:<1>" in failed.message
    assert failed.stack_top.startswith("at com.example.CalcTest.subtract")
    assert by_id["com.example.CalcTest#divideByZero"].status == CaseStatus.SKIPPED
    assert by_id["com.example.CalcTest#readReport"].status == CaseStatus.ERROR
    # 制品：hash/类型/原始报告引用齐全（大文件只存 URI，不进结构化库）
    artifact = run.artifacts[0]
    assert artifact.type == ArtifactType.JUNIT_XML and artifact.content_hash
    assert run.raw_report_ref and run.raw_report_ref.endswith(".xml")
    assert all(c.node_id == "TEST-com.example.CalcTest.xml" for c in run.case_results)


async def test_same_contract_model_as_xcode_runner():
    """FR-EX-001 AC1：xcode 与 Surefire 两个适配器产出同一种语言中立模型。"""
    junit_runner = JUnitXmlRunner(FIXTURES / "runs" / "surefire")
    xcode_runner = RecordedTestRunner(FIXTURES / "runs")
    r1 = await junit_runner.submit(_req())
    r2 = await xcode_runner.submit(_req(hint="xcode_hostless"))
    run_junit = await junit_runner.get_run(r1)
    run_xcode = await xcode_runner.get_run(r2)
    for run in (run_junit, run_xcode):
        assert isinstance(run, TestRun)
        assert all(hasattr(c, "external_case_id") and hasattr(c, "status")
                   for c in run.case_results)
        assert isinstance(run.environment.capabilities, list)
    # 平台差异只存在于 environment/provider 与制品类型，模型骨架一致
    assert run_junit.environment.provider == "junit-local"
    assert run_xcode.environment.provider == "ios-simulator"
    assert run_junit.artifacts[0].type == ArtifactType.JUNIT_XML
    assert run_xcode.artifacts[0].type == ArtifactType.XCRESULT


async def test_hint_resolution_and_discovery_guards(tmp_path):
    # 无后缀提示自动补 .xml
    runner = JUnitXmlRunner(FIXTURES / "runs" / "surefire")
    run_id = await runner.submit(_req(hint="TEST-com.example.CalcTest"))
    assert (await runner.get_run(run_id)).status == RunStatus.FAILED

    # 目录恰好一份报告时允许无提示自动发现
    single = tmp_path / "single"
    single.mkdir()
    (single / "TEST-a.xml").write_text(
        '<?xml version="1.0"?><testsuite name="A" tests="0"/>', encoding="utf-8")
    auto = JUnitXmlRunner(single)
    rid = await auto.submit(_req(hint=None, idempotency_key="idem-auto"))
    assert (await auto.get_run(rid)).status == RunStatus.ERROR  # 零用例→run error

    # 多份报告不点名：fail-closed，不静默挑一份
    (single / "TEST-b.xml").write_text(
        '<?xml version="1.0"?><testsuite name="B" tests="0"/>', encoding="utf-8")
    with pytest.raises(SchemaValidationError):
        await auto.submit(_req(hint=None, idempotency_key="idem-multi"))

    # 点名的文件不存在 → 适配器不可用分型
    with pytest.raises(AdapterUnavailableError):
        await runner.submit(_req(hint="does-not-exist"))

    # 畸形 XML → 契约错误分型（不吞异常不编结果）
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "TEST-bad.xml").write_text("<<<not xml>>>", encoding="utf-8")
    bad_runner = JUnitXmlRunner(bad)
    with pytest.raises(SchemaValidationError):
        await bad_runner.submit(_req(hint="TEST-bad", idempotency_key="idem-bad"))


async def test_cancel_and_unknown_run():
    runner = JUnitXmlRunner(FIXTURES / "runs" / "surefire")
    with pytest.raises(AdapterUnavailableError):
        await runner.get_run("run-missing")
    run_id = await runner.submit(_req())
    await runner.cancel(run_id)
    assert (await runner.get_run(run_id)).status == RunStatus.CANCELED


def test_factory_wires_junit_xml_runner(tmp_path):
    config = load_profile(SUBSYSTEMS / "config" / "profiles" / "offline-demo.yaml")
    config.paths["data_dir"] = str(tmp_path / "data")
    config.paths["fixtures_dir"] = str(SUBSYSTEMS / "tests" / "fixtures")
    config.adapters.runner = AdapterRef(type="junit-xml", settings={})
    container = build_container(config, SUBSYSTEMS)
    try:
        assert isinstance(container.get("runner"), JUnitXmlRunner)
    finally:
        container.get("store").close()


def test_factory_rejects_unknown_runner_type(tmp_path):
    config = load_profile(SUBSYSTEMS / "config" / "profiles" / "offline-demo.yaml")
    config.paths["data_dir"] = str(tmp_path / "data")
    config.paths["fixtures_dir"] = str(SUBSYSTEMS / "tests" / "fixtures")
    config.adapters.runner = AdapterRef(type="ghost-runner")
    with pytest.raises(ValueError, match="未知 runner"):
        build_container(config, SUBSYSTEMS)
