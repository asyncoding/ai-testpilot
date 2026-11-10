"""S3.2 XcodeTestRunner 适配器测试（假进程/假 parser，零 xcodebuild）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from testpilot_adapters.xcode_runner import XcodeTestRunner
from testpilot_core.enums import CaseStatus, RunStatus
from testpilot_core.errors import AdapterUnavailableError
from testpilot_core.execution import run_id_for
from testpilot_core.models import (
    CaseResult,
    CaseSelector,
    EnvironmentInfo,
    EnvironmentRequirements,
    TestExecutionRequest,
    TestRun,
)

_UDID = "5411787C-8AFF-4EBF-9000-EB5EFD1457D5"


class FakeProcess:
    def __init__(self, argv, cwd, *, rc, on_wait):
        self.argv = argv
        self.cwd = cwd
        self.pid = 4321
        self.terminated = False
        self._rc = rc
        self._on_wait = on_wait

    async def wait(self):
        if self._on_wait:
            self._on_wait()
        return self._rc

    def terminate(self):
        self.terminated = True


class FakeSpawner:
    """模拟 xcodebuild 生命周期：spawn 时刻在 resultBundlePath 产出 xcresult 目录。"""

    def __init__(self, *, rc=0, create_bundle=True):
        self.rc = rc
        self.create_bundle = create_bundle
        self.spawned: list[tuple[list[str], str]] = []
        self.processes: list[FakeProcess] = []

    async def spawn(self, argv, *, cwd=None):
        bundle = Path(argv[argv.index("-resultBundlePath") + 1])
        # 预清理证据：spawn 时旧包必须已不存在
        assert not bundle.exists(), "resultBundlePath 应在 spawn 前清理干净"
        process = FakeProcess(
            argv, cwd, rc=self.rc,
            on_wait=(lambda: bundle.mkdir(parents=True, exist_ok=True))
            if self.create_bundle else None,
        )
        self.spawned.append((argv, cwd or ""))
        self.processes.append(process)
        return process


def _request(*, test_ids=("AlarmKitSpikeTests",), udid=_UDID, request_id="req-1"):
    return TestExecutionRequest(
        request_id=request_id,
        idempotency_key=f"idem-{request_id}",
        project="demo",
        selector=CaseSelector(test_ids=list(test_ids)),
        commit_sha="deadbeef",
        environment=EnvironmentRequirements(
            capabilities=["ios.simulator"],
            labels={"udid": udid, "request_id": request_id},
        ),
    )


async def _fake_parser(**kwargs):
    _fake_parser.calls.append(kwargs)
    return TestRun(
        run_id="parser-internal",
        idempotency_key=kwargs["request"].idempotency_key,
        project=kwargs["request"].project,
        commit_sha=kwargs["request"].commit_sha,
        status=RunStatus.FAILED if kwargs["returncode"] != 0 else RunStatus.PASSED,
        environment=EnvironmentInfo(provider="ios-simulator", env_id=kwargs["env_id"]),
        case_results=[CaseResult(
            external_case_id="AlarmKitSpikeTests/t1",
            status=CaseStatus.FAILED if kwargs["returncode"] != 0 else CaseStatus.PASSED,
            duration_ms=12)],
    )


_fake_parser.calls = []


@pytest.fixture(autouse=True)
def _reset_parser_calls():
    _fake_parser.calls.clear()
    yield


def _runner(tmp_path, spawner):
    return XcodeTestRunner(
        workspace="AlarmKitSpike/AlarmKitSpike.xcworkspace",
        scheme="AlarmKitSpike",
        repo_path=tmp_path / "repo",
        result_bundle_dir=tmp_path / "bundles",
        result_parser=_fake_parser,
        executor=spawner,
    )


async def test_submit_builds_docs03_argv_with_cwd_and_precleans_bundle(tmp_path):
    spawner = FakeSpawner()
    runner = _runner(tmp_path, spawner)
    request = _request()
    # 预置一个陈旧 xcresult（含文件），验证 FR-EX-002 AC3 预清理
    run_id = run_id_for(request.idempotency_key)
    stale = tmp_path / "bundles" / f"{run_id}.xcresult"
    stale.mkdir(parents=True)
    (stale / "old.dat").write_text("stale")

    returned_id = await runner.submit(request)
    argv, cwd = spawner.spawned[0]
    assert returned_id == run_id
    assert cwd == str(tmp_path / "repo")
    assert argv == [
        "xcodebuild", "test",
        "-workspace", "AlarmKitSpike/AlarmKitSpike.xcworkspace",
        "-scheme", "AlarmKitSpike",
        "-destination", f"platform=iOS Simulator,id={_UDID}",
        "-only-testing", "AlarmKitSpikeTests",
        "-resultBundlePath", str(tmp_path / "bundles" / f"{run_id}.xcresult"),
    ]
    assert not (stale / "old.dat").exists()  # 旧内容被清掉
    assert spawner.processes[0].terminated is False


async def test_get_run_awaits_process_and_normalizes_identity(tmp_path):
    spawner = FakeSpawner(rc=1)  # 测试失败时 xcodebuild 非零退出是正常路径
    runner = _runner(tmp_path, spawner)
    request = _request()
    run_id = await runner.submit(request)
    run = await runner.get_run(run_id)

    assert run.run_id == run_id  # 覆盖 parser 内部 id
    assert run.status == RunStatus.FAILED
    assert run.environment.env_id == _UDID
    parser_kwargs = _fake_parser.calls[-1]
    assert parser_kwargs["returncode"] == 1
    assert parser_kwargs["env_id"] == _UDID
    assert parser_kwargs["result_bundle_path"].name == f"{run_id}.xcresult"


async def test_missing_udid_refuses_without_spawn(tmp_path):
    spawner = FakeSpawner()
    runner = _runner(tmp_path, spawner)
    with pytest.raises(AdapterUnavailableError, match="udid"):
        await runner.submit(_request(udid=""))
    assert spawner.spawned == []


async def test_empty_selector_fails_closed(tmp_path):
    spawner = FakeSpawner()
    runner = _runner(tmp_path, spawner)
    with pytest.raises(ValueError, match="选择执行"):
        await runner.submit(_request(test_ids=()))
    assert spawner.spawned == []


async def test_missing_bundle_after_exit_is_adapter_error(tmp_path):
    spawner = FakeSpawner(create_bundle=False)
    runner = _runner(tmp_path, spawner)
    run_id = await runner.submit(_request())
    with pytest.raises(AdapterUnavailableError, match="xcresult"):
        await runner.get_run(run_id)


async def test_cancel_terminates_process_and_unknown_id_is_noop(tmp_path):
    spawner = FakeSpawner()
    runner = _runner(tmp_path, spawner)
    run_id = await runner.submit(_request())
    await runner.cancel(run_id)
    assert spawner.processes[0].terminated is True
    await runner.cancel(run_id)  # 幂等，不重复 terminate 不抛
    await runner.cancel("run-ghost")  # 未知 id 静默
    with pytest.raises(AdapterUnavailableError):
        await runner.get_run("run-ghost")


async def test_single_xcuitest_identifier_flows_through_runner(tmp_path):
    spawner = FakeSpawner()
    runner = _runner(tmp_path, spawner)
    request = _request(test_ids=(
        "AlarmKitSpikeUITests/ProductFlowUITests/test10_WakeForm_SoundPickedThenSwipeStillBlocked",
    ))
    await runner.submit(request)
    argv = spawner.spawned[0][0]
    assert argv[argv.index("-only-testing") + 1].startswith("AlarmKitSpikeUITests/")
