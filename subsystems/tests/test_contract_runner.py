"""录制执行器契约 + TestRun 语言中立性（docs/08 FR-EX-001）。"""

from pathlib import Path

import pytest

from testpilot_adapters.recorded_runner import RecordedTestRunner
from testpilot_core.enums import CaseStatus, RunStatus
from testpilot_core.errors import AdapterUnavailableError
from testpilot_core.models import CaseSelector, EnvironmentRequirements, TestExecutionRequest

FIXTURES = Path(__file__).parent / "fixtures"


def _req(name: str) -> TestExecutionRequest:
    return TestExecutionRequest(
        request_id="req-1",
        idempotency_key="idem-1",
        project="demo",
        selector=CaseSelector(test_ids=["t"], fixture_hint=name),
        commit_sha="deadbeef",
        environment=EnvironmentRequirements(capabilities=["ios.simulator"]),
    )


async def test_xcode_recorded_run_normalizes():
    runner = RecordedTestRunner(FIXTURES / "runs")
    run_id = await runner.submit(_req("xcode_hostless"))
    run = await runner.get_run(run_id)
    assert run.status == RunStatus.FAILED
    assert run.commit_sha == "deadbeef"  # 请求覆盖录制身份
    statuses = {c.external_case_id: c.status for c in run.case_results}
    assert any(s == CaseStatus.FAILED for s in statuses.values())
    failed = next(c for c in run.case_results if c.status == CaseStatus.FAILED)
    assert "90" in failed.message and "30" in failed.message
    assert "ios.device" in run.uncovered_layers  # 未覆盖层显式声明


async def test_same_contract_serves_junit_fixture():
    """同一份 TestRun 模型可承载 JUnit/Surefire 结果——语言中立性证据。"""
    runner = RecordedTestRunner(FIXTURES / "runs")
    run_id = await runner.submit(_req("junit_surefire"))
    run = await runner.get_run(run_id)
    assert run.environment.provider == "junit-local"
    assert run.status == RunStatus.PASSED
    assert run.artifacts[0].type == "junit_xml"


async def test_unknown_run_and_cancel():
    runner = RecordedTestRunner(FIXTURES / "runs")
    with pytest.raises(AdapterUnavailableError):
        await runner.get_run("nope")
    run_id = await runner.submit(_req("xcode_hostless"))
    await runner.cancel(run_id)
    assert (await runner.get_run(run_id)).status == RunStatus.CANCELED


async def test_missing_fixture():
    runner = RecordedTestRunner(FIXTURES / "runs")
    with pytest.raises(AdapterUnavailableError):
        await runner.submit(_req("does-not-exist"))
