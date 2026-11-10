"""S3.3 FR-EX-005 XcresultToolResultParser 适配器测试（hermetic，零真实 xcresulttool）。

假执行器按 argv 分发脚本化输出，export 动作在 --output-path 物化自造附件字节；
制品根走真实 LocalFsArtifactAdapter（tmp_path），验 URN 内容寻址与回链。
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
_FAIL_REF = "AlarmKitSpikeTests.xctest/AlarmKitSpikeTests/testFail()"
_PASS_REF = "AlarmKitSpikeTests.xctest/AlarmKitSpikeTests/testPass()"


def _req() -> TestExecutionRequest:
    return TestExecutionRequest(
        request_id="req-1", idempotency_key="idem-1", project="ios-demo",
        selector={"test_ids": ["AlarmKitSpikeTests"]},
        commit_sha="9f8e7d6c", build_id="xcode-1007",
        environment={"capabilities": ["ios.simulator"],
                     "labels": {"udid": "5411787C-UDID"}},
    )


def _tests_json() -> dict:
    return {"testNodes": [{
        "nodeType": "Unit test bundle",
        "name": "AlarmKitSpikeTests.xctest", "result": "Mixed",
        "children": [{
            "nodeType": "Test Suite", "name": "AlarmKitSpikeTests",
            "result": "Mixed",
            "children": [
                {"nodeType": "Test case", "name": "testPass()",
                 "result": "Passed", "duration": 0.03},
                {"nodeType": "Test case", "name": "testFail()",
                 "result": "Failed", "duration": 0.4,
                 "failures": [{"failureText": "assert failed: 1 != 2\nfile: 42"}]},
            ],
        }],
    }]}


def _attachments_json() -> dict:
    return {"attachments": [
        {"case_ref": _FAIL_REF, "file_name": "Screenshot-1.png",
         "mime_type": "image/png"},
        {"case_ref": _FAIL_REF, "file_name": "Recording.mp4",
         "mime_type": "video/mp4"},
    ]}


class FakeExecutor:
    """按 argv 形态分发：get tests / get attachments / export attachments。"""

    def __init__(
        self,
        tests_payload: dict,
        attachments_payload: dict | None,
        *,
        tests_rc: int = 0,
        manifest_rc: int = 0,
        export_rc: int = 0,
        skip_export_files: bool = False,
        raise_exc: Exception | None = None,
    ) -> None:
        self._tests = json.dumps(tests_payload)
        self._attachments = (
            json.dumps(attachments_payload) if attachments_payload is not None else ""
        )
        self._rc = {"tests": tests_rc, "manifest": manifest_rc, "export": export_rc}
        self._skip_files = skip_export_files
        self._raise_exc = raise_exc
        self.calls: list[list[str]] = []

    async def run(self, argv: list[str], *, cwd: str | None = None) -> CommandResult:
        assert isinstance(argv, list) and all(isinstance(p, str) for p in argv)
        if self._raise_exc is not None:
            raise self._raise_exc
        self.calls.append(argv)
        kind = argv[2:5]
        if kind == ["get", "test-results", "tests"]:
            return CommandResult(self._rc["tests"], self._tests, "", list(argv))
        if kind == ["get", "test-results", "attachments"]:
            return CommandResult(self._rc["manifest"], self._attachments, "", list(argv))
        if argv[2:4] == ["export", "attachments"]:
            rc = self._rc["export"]
            if rc == 0 and not self._skip_files:
                out = argv[argv.index("--output-path") + 1]
                for name, payload in (
                    ("Screenshot-1.png", b"\x89PNG fake"),
                    ("Recording.mp4", b"\x00mp4 fake"),
                ):
                    (Path(out) / name).write_bytes(payload)
            return CommandResult(rc, "", "" if rc == 0 else "export boom", list(argv))
        raise AssertionError(f"未预期的 xcresulttool 调用: {argv}")


def _parser(tmp_path, executor) -> XcresultToolResultParser:
    store = LocalFsArtifactAdapter(tmp_path / "artifacts")
    return XcresultToolResultParser(
        store, work_dir=tmp_path / "xcwork", executor=executor
    )


async def test_happy_path_failed_case_links_two_attachment_urns(tmp_path):
    executor = FakeExecutor(_tests_json(), _attachments_json())
    parser = _parser(tmp_path, executor)
    run = await parser(
        request=_req(), result_bundle_path=_BUNDLE,
        env_id="5411787C-UDID", returncode=1,
    )

    # 三命令顺序：get tests → get attachments → export
    assert [c[2] for c in executor.calls] == ["get", "get", "export"]
    assert executor.calls[0][2:5] == ["get", "test-results", "tests"]
    assert executor.calls[1][2:5] == ["get", "test-results", "attachments"]
    assert executor.calls[2][2:4] == ["export", "attachments"]
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
    assert run.environment.env_id == "5411787C-UDID"
    assert run.environment.capabilities == ["ios.simulator"]


async def test_no_attachments_skips_export_and_keeps_raw_ref(tmp_path):
    executor = FakeExecutor(_tests_json(), {"attachments": []})
    parser = _parser(tmp_path, executor)
    run = await parser(
        request=_req(), result_bundle_path=_BUNDLE,
        env_id="u", returncode=0,
    )
    assert len(executor.calls) == 2  # 无 export
    assert [c[2:5] for c in executor.calls][1] == [
        "get", "test-results", "attachments"
    ]
    assert all(c.artifact_urns == [] for c in run.case_results)
    assert [a.type for a in run.artifacts] == [ArtifactType.XCRESULT]
    assert run.run_id == ""  # 身份由 XcodeTestRunner.get_run 收口回填


async def test_blank_manifest_stdout_treated_as_no_attachments(tmp_path):
    executor = FakeExecutor(_tests_json(), None)
    parser = _parser(tmp_path, executor)
    run = await parser(
        request=_req(), result_bundle_path=_BUNDLE, env_id="u", returncode=0
    )
    assert len(executor.calls) == 2
    assert [a.type for a in run.artifacts] == [ArtifactType.XCRESULT]


async def test_tests_command_nonzero_rc_is_adapter_unavailable(tmp_path):
    executor = FakeExecutor(_tests_json(), None, tests_rc=64)
    parser = _parser(tmp_path, executor)
    with pytest.raises(AdapterUnavailableError, match="xcresulttool 失败"):
        await parser(request=_req(), result_bundle_path=_BUNDLE,
                     env_id="u", returncode=1)


async def test_manifest_nonzero_rc_is_adapter_unavailable(tmp_path):
    executor = FakeExecutor(_tests_json(), _attachments_json(), manifest_rc=65)
    parser = _parser(tmp_path, executor)
    with pytest.raises(AdapterUnavailableError):
        await parser(request=_req(), result_bundle_path=_BUNDLE,
                     env_id="u", returncode=1)


async def test_export_nonzero_rc_is_adapter_unavailable(tmp_path):
    executor = FakeExecutor(
        _tests_json(), _attachments_json(), export_rc=66
    )
    parser = _parser(tmp_path, executor)
    with pytest.raises(AdapterUnavailableError, match="export boom"):
        await parser(request=_req(), result_bundle_path=_BUNDLE,
                     env_id="u", returncode=1)


async def test_manifest_claims_attachment_but_export_misses_file_loud(tmp_path):
    executor = FakeExecutor(
        _tests_json(), _attachments_json(), skip_export_files=True
    )
    parser = _parser(tmp_path, executor)
    with pytest.raises(AdapterUnavailableError, match="附件导出缺失"):
        await parser(request=_req(), result_bundle_path=_BUNDLE,
                     env_id="u", returncode=1)


async def test_malformed_tests_json_fails_closed(tmp_path):
    executor = FakeExecutor({"testNodes": []}, None)
    parser = _parser(tmp_path, executor)
    with pytest.raises(SchemaValidationError):
        await parser(request=_req(), result_bundle_path=_BUNDLE,
                     env_id="u", returncode=1)


async def test_malformed_manifest_json_fails_closed(tmp_path):
    class BadJsonExecutor(FakeExecutor):
        def __init__(self, tmp):
            super().__init__(_tests_json(), None)
            self._bad = True

        async def run(self, argv, *, cwd=None):
            if argv[2:5] == ["get", "test-results", "attachments"]:
                self.calls.append(argv)
                return CommandResult(0, "{broken", "", list(argv))
            return await super().run(argv, cwd=cwd)

    parser = _parser(tmp_path, BadJsonExecutor(tmp_path))
    with pytest.raises(SchemaValidationError, match="附件清单 JSON 解析失败"):
        await parser(request=_req(), result_bundle_path=_BUNDLE,
                     env_id="u", returncode=0)


async def test_missing_xcresulttool_translates_to_unavailable(tmp_path):
    executor = FakeExecutor(
        _tests_json(), None,
        raise_exc=AdapterUnavailableError("命令不存在（工具链未安装？）: xcrun"),
    )
    parser = _parser(tmp_path, executor)
    with pytest.raises(AdapterUnavailableError, match="工具链未安装"):
        await parser(request=_req(), result_bundle_path=_BUNDLE,
                     env_id="u", returncode=1)


async def test_xcodebuild_failure_rc_still_parses_into_failed_run(tmp_path):
    # xcodebuild 测试失败 rc≠0 是正常业务信号，parser 不看 returncode  gate
    executor = FakeExecutor(_tests_json(), {"attachments": []})
    parser = _parser(tmp_path, executor)
    run = await parser(
        request=_req(), result_bundle_path=_BUNDLE, env_id="u", returncode=65
    )
    assert run.status is RunStatus.FAILED
    assert run_id_for("idem-1")  # 身份函数可用，真正回填在 runner 层
