"""S3.3 FR-EX-005 XcresultToolResultParser 适配器测试（hermetic，零真实 xcresulttool）。

假执行器按 argv 分发脚本化输出，export 动作在 --output-path 物化自造附件字节与
manifest.json（对齐 xcresulttool 25115 真实行为）；制品根走真实
LocalFsArtifactAdapter（tmp_path），验 URN 内容寻址与回链。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from testpilot_core.enums import ArtifactType, CaseStatus, RunStatus
from testpilot_core.errors import AdapterUnavailableError, SchemaValidationError
from testpilot_core.execution import run_id_for
from testpilot_core.models import TestExecutionRequest
from testpilot_adapters.localfs_artifact import LocalFsArtifactAdapter
from testpilot_adapters.process_executor import CommandResult
from testpilot_adapters.xcresult_tool import XcresultToolResultParser

_BUNDLE = "store/xcresult/run-1.xcresult"
_SUITE = "ReminderSchedulerTests"
_FAIL_REF = f"AlarmKitSpike/AlarmKitSpikeTests/{_SUITE}/testFail()"
_PASS_REF = f"AlarmKitSpike/AlarmKitSpikeTests/{_SUITE}/testPass()"


def _req() -> TestExecutionRequest:
    return TestExecutionRequest(
        request_id="req-1", idempotency_key="idem-1", project="ios-demo",
        selector={"test_ids": ["AlarmKitSpikeTests"]},
        commit_sha="9f8e7d6c", build_id="xcode-1007",
        environment={"capabilities": ["ios.simulator"],
                     "labels": {"udid": "18BC020D-UDID"}},
    )


def _tests_json() -> dict:
    return {"testNodes": [{
        "nodeType": "Test Plan", "name": "AlarmKitSpike", "result": "Failed",
        "children": [{
            "nodeType": "Unit test bundle", "name": "AlarmKitSpikeTests",
            "result": "Failed",
            "children": [{
                "nodeType": "Test Suite", "name": _SUITE, "result": "Failed",
                "children": [
                    {"nodeType": "Test Case", "name": "testPass()",
                     "result": "Passed", "duration": "0.03s",
                     "durationInSeconds": 0.03},
                    {"nodeType": "Test Case", "name": "testFail()",
                     "result": "Failed", "duration": "0.4s",
                     "durationInSeconds": 0.4,
                     "children": [
                         {"nodeType": "Failure Message",
                          "name": "assert failed: 1 != 2",
                          "details": "assert failed: 1 != 2\nfile: 42"}]},
                ],
            }],
        }],
    }]}


def _manifest_items() -> list:
    """export attachments 真实形态：顶层数组，testIdentifier 无 bundle 前缀。"""
    return [
        {
            "testIdentifier": f"{_SUITE}/testFail()",
            "testIdentifierURL": "test://x/testFail",
            "attachments": [
                {"exportedFileName": "Screenshot-1.png",
                 "suggestedHumanReadableName": "shot",
                 "isAssociatedWithFailure": True,
                 "configurationName": "Test Scheme Action",
                 "deviceName": "p", "deviceId": "18BC020D"},
                {"exportedFileName": "Recording.mp4",
                 "suggestedHumanReadableName": "rec",
                 "isAssociatedWithFailure": True,
                 "configurationName": "Test Scheme Action",
                 "deviceName": "p", "deviceId": "18BC020D"},
            ],
        }
    ]


class FakeExecutor:
    """按 argv 形态分发：get tests / export attachments（manifest 随导出落盘）。"""

    def __init__(
        self,
        tests_payload: dict,
        manifest_items: list,
        *,
        tests_rc: int = 0,
        export_rc: int = 0,
        skip_export_files: bool = False,
        skip_manifest: bool = False,
        broken_manifest: bool = False,
        raise_exc: Exception | None = None,
    ) -> None:
        self._tests = json.dumps(tests_payload)
        self._manifest = "{" if broken_manifest else json.dumps(manifest_items)
        self._tests_rc = tests_rc
        self._export_rc = export_rc
        self._skip_files = skip_export_files
        self._skip_manifest = skip_manifest
        self._raise_exc = raise_exc
        self.calls: list[list[str]] = []

    async def run(self, argv: list[str], *, cwd: str | None = None) -> CommandResult:
        assert isinstance(argv, list) and all(isinstance(p, str) for p in argv)
        if self._raise_exc is not None:
            raise self._raise_exc
        self.calls.append(argv)
        if argv[2:5] == ["get", "test-results", "tests"]:
            return CommandResult(self._tests_rc, self._tests, "", list(argv))
        if argv[2:4] == ["export", "attachments"]:
            if self._export_rc != 0:
                return CommandResult(self._export_rc, "", "export boom", list(argv))
            out = Path(argv[argv.index("--output-path") + 1])
            # 拟真 xcresulttool 25115：输出目录已含 manifest.json 即 rc=1 拒绝覆盖
            # （真实 stderr："Failed to generate manifest.json: file already exists."）
            if (out / "manifest.json").exists():
                return CommandResult(
                    1, "",
                    "Error: Failed to generate manifest.json: file already exists.",
                    list(argv),
                )
            if not self._skip_manifest:
                (out / "manifest.json").write_text(self._manifest)
            if not self._skip_files:
                for name, payload in (
                    ("Screenshot-1.png", b"\x89PNG fake"),
                    ("Recording.mp4", b"\x00mp4 fake"),
                ):
                    (out / name).write_bytes(payload)
            # 真实工具全绿 run 的 stderr 就是逐用例 Skipped 提示，rc=0
            return CommandResult(0, "", "Skipped export for testPass(): no matching attachments", list(argv))
        raise AssertionError(f"未预期的 xcresulttool 调用: {argv}")


def _parser(tmp_path, executor) -> XcresultToolResultParser:
    store = LocalFsArtifactAdapter(tmp_path / "artifacts")
    return XcresultToolResultParser(
        store, work_dir=tmp_path / "xcwork", executor=executor
    )


async def test_happy_path_failed_case_links_two_attachment_urns(tmp_path):
    executor = FakeExecutor(_tests_json(), _manifest_items())
    parser = _parser(tmp_path, executor)
    run = await parser(
        request=_req(), result_bundle_path=_BUNDLE,
        env_id="18BC020D-UDID", returncode=1,
    )

    # 两命令顺序：get tests → export attachments（25115 清单已并入 export）
    assert [c[2] for c in executor.calls] == ["get", "export"]
    assert executor.calls[0][2:5] == ["get", "test-results", "tests"]
    assert executor.calls[1][2:4] == ["export", "attachments"]
    assert executor.calls[0][-1] == _BUNDLE  # --path 指向 runner 产物包
    assert run.status is RunStatus.FAILED
    by_id = {c.external_case_id: c for c in run.case_results}
    failed = by_id[_FAIL_REF]
    passed = by_id[_PASS_REF]
    assert failed.status is CaseStatus.FAILED
    assert len(failed.artifact_urns) == 2
    assert passed.artifact_urns == []

    # URN 可经制品端口解析回真实文件
    store = LocalFsArtifactAdapter(tmp_path / "artifacts")
    for urn in failed.artifact_urns:
        resolved = await store.resolve_uri(urn)
        assert resolved.startswith("file:")
        assert Path(resolved.removeprefix("file:")).exists()

    # 制品清单：截图 + 录屏 + xcresult 原始 JSON，类型齐备
    types = sorted(a.type for a in run.artifacts)
    assert types == [
        ArtifactType.SCREENSHOT, ArtifactType.VIDEO, ArtifactType.XCRESULT
    ]
    assert all(a.content_hash and len(a.content_hash) == 12 for a in run.artifacts)
    # raw_report_ref 指向可解析 URN 而非裸路径
    assert run.raw_report_ref and run.raw_report_ref.startswith("urn:testpilot:")
    await store.resolve_uri(run.raw_report_ref)
    # 模拟器未覆盖层显式声明
    assert run.uncovered_layers == ["background_audio", "codesigning", "ios.device"]
    assert run.environment.env_id == "18BC020D-UDID"
    assert run.environment.capabilities == ["ios.simulator"]


async def test_empty_manifest_array_is_normal_all_green_run(tmp_path):
    # 真实全绿 run：export 仍执行，manifest.json 为 []，仅留 XCRESULT 原始制品
    executor = FakeExecutor(_tests_json(), [])
    parser = _parser(tmp_path, executor)
    run = await parser(
        request=_req(), result_bundle_path=_BUNDLE,
        env_id="u", returncode=0,
    )
    assert len(executor.calls) == 2
    assert all(c.artifact_urns == [] for c in run.case_results)
    assert [a.type for a in run.artifacts] == [ArtifactType.XCRESULT]
    assert run.run_id == ""  # 身份由 XcodeTestRunner.get_run 收口回填


async def test_repeated_parse_same_idempotency_key_cleans_export_dir(tmp_path):
    # 真机探针抓出（2026-11-10）：同 idempotency key → run_id 相同 → 复用
    # run_dir/attachments；真实 xcresulttool 25115 见 manifest.json 已存在即
    # rc=1 "file already exists"。适配器必须自清场，保证重试幂等。
    executor = FakeExecutor(_tests_json(), _manifest_items())
    parser = _parser(tmp_path, executor)
    first = await parser(
        request=_req(), result_bundle_path=_BUNDLE, env_id="u", returncode=1
    )
    assert first.status is RunStatus.FAILED
    second = await parser(
        request=_req(), result_bundle_path=_BUNDLE, env_id="u", returncode=1
    )
    assert len(second.case_results) == 2
    by_id = {c.external_case_id: c for c in second.case_results}
    assert len(by_id[_FAIL_REF].artifact_urns) == 2


async def test_tests_command_nonzero_rc_is_adapter_unavailable(tmp_path):
    executor = FakeExecutor(_tests_json(), [], tests_rc=64)
    parser = _parser(tmp_path, executor)
    with pytest.raises(AdapterUnavailableError, match="xcresulttool 失败"):
        await parser(request=_req(), result_bundle_path=_BUNDLE,
                     env_id="u", returncode=1)


async def test_export_nonzero_rc_is_adapter_unavailable(tmp_path):
    executor = FakeExecutor(_tests_json(), _manifest_items(), export_rc=66)
    parser = _parser(tmp_path, executor)
    with pytest.raises(AdapterUnavailableError, match="export boom"):
        await parser(request=_req(), result_bundle_path=_BUNDLE,
                     env_id="u", returncode=1)


async def test_missing_manifest_file_is_loud_failure(tmp_path):
    # 工具 rc=0 却不生成 manifest：证据链断裂，响亮失败不静默
    executor = FakeExecutor(_tests_json(), _manifest_items(), skip_manifest=True)
    parser = _parser(tmp_path, executor)
    with pytest.raises(AdapterUnavailableError, match="未生成 manifest.json"):
        await parser(request=_req(), result_bundle_path=_BUNDLE,
                     env_id="u", returncode=1)


async def test_manifest_claims_attachment_but_export_misses_file_loud(tmp_path):
    executor = FakeExecutor(
        _tests_json(), _manifest_items(), skip_export_files=True
    )
    parser = _parser(tmp_path, executor)
    with pytest.raises(AdapterUnavailableError, match="附件导出缺失"):
        await parser(request=_req(), result_bundle_path=_BUNDLE,
                     env_id="u", returncode=1)


async def test_malformed_tests_json_fails_closed(tmp_path):
    executor = FakeExecutor({"testNodes": []}, [])
    parser = _parser(tmp_path, executor)
    with pytest.raises(SchemaValidationError):
        await parser(request=_req(), result_bundle_path=_BUNDLE,
                     env_id="u", returncode=1)


async def test_malformed_manifest_json_fails_closed(tmp_path):
    executor = FakeExecutor(_tests_json(), [], broken_manifest=True)
    parser = _parser(tmp_path, executor)
    with pytest.raises(SchemaValidationError, match="manifest JSON 解析失败"):
        await parser(request=_req(), result_bundle_path=_BUNDLE,
                     env_id="u", returncode=0)


async def test_missing_xcresulttool_translates_to_unavailable(tmp_path):
    executor = FakeExecutor(
        _tests_json(), [],
        raise_exc=AdapterUnavailableError("命令不存在（工具链未安装？）: xcrun"),
    )
    parser = _parser(tmp_path, executor)
    with pytest.raises(AdapterUnavailableError, match="工具链未安装"):
        await parser(request=_req(), result_bundle_path=_BUNDLE,
                     env_id="u", returncode=1)


async def test_xcodebuild_failure_rc_still_parses_into_failed_run(tmp_path):
    # xcodebuild 测试失败 rc≠0 是正常业务信号，parser 不看 returncode gate
    executor = FakeExecutor(_tests_json(), [])
    parser = _parser(tmp_path, executor)
    run = await parser(
        request=_req(), result_bundle_path=_BUNDLE, env_id="u", returncode=65
    )
    assert run.status is RunStatus.FAILED
    assert run_id_for("idem-1")  # 身份函数可用，真正回填在 runner 层
