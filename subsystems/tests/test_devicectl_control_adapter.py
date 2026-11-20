"""DevicectlControlAdapter hermetic 测试（docs/01 §3/§4，H3）。

零真机：FakeExecutor 脚本化 rc，并在截图调用时模拟 devicectl 落盘 PNG，
钉死三件事——① argv 全显式（设备/路径/PID，无隐式目标）；② rc!=0 一律
DevicectlControlError 响亮失败；③ rc=0 却缺截图产物按契约破坏处理，
绝不让上层拿空文件判方向（假绿防线）。
"""

from __future__ import annotations

import json

import pytest

from testpilot_adapters.devicectl_control import (
    DevicectlControlAdapter,
    DevicectlControlError,
)
from testpilot_adapters.process_executor import CommandResult

_UDID = "00008140-001A39590162801C"
_BUNDLE = "com.example.AlarmKitSpike"


class FakeExecutor:
    def __init__(self, *, results: list[CommandResult] | None = None,
                 write_screenshot: bool = True) -> None:
        self._results = list(results or [])
        self._write_screenshot = write_screenshot
        self.calls: list[list[str]] = []

    async def run(self, argv: list[str], *, cwd=None) -> CommandResult:
        self.calls.append(list(argv))
        result = (
            self._results.pop(0) if self._results
            else CommandResult(0, json.dumps({"ok": True}), "", list(argv))
        )
        # 真实 devicectl 仅在成功时落盘；失败 rc 不产文件诱饵
        if "screenshot" in argv and self._write_screenshot \
                and result.returncode == 0:
            # argv 里 --destination 后一个参数就是落盘路径
            dest = argv[argv.index("--destination") + 1]
            with open(dest, "wb") as fh:
                fh.write(b"\x89PNG\r\n\x1a\nFAKE-BYTES")
        return result


async def test_capture_screenshot_argv_and_returned_path(tmp_path):
    executor = FakeExecutor()
    adapter = DevicectlControlAdapter(executor, tmp_path / "shots")
    path = await adapter.capture_screenshot(_UDID)

    argv = executor.calls[0]
    assert argv[:5] == [
        "xcrun", "devicectl", "device", "capture", "screenshot"]
    assert argv[argv.index("--device") + 1] == _UDID
    assert path.exists() and path.stat().st_size > 0
    assert path.parent == tmp_path / "shots"


async def test_screenshot_rc_zero_but_missing_artifact_is_loud(tmp_path):
    executor = FakeExecutor(write_screenshot=False)
    adapter = DevicectlControlAdapter(executor, tmp_path / "shots")
    with pytest.raises(DevicectlControlError, match="产物缺失/为空"):
        await adapter.capture_screenshot(_UDID)


async def test_screenshot_nonzero_rc_is_loud(tmp_path):
    executor = FakeExecutor(results=[
        CommandResult(1, "", "device unavailable", [])])
    adapter = DevicectlControlAdapter(executor, tmp_path / "shots")
    with pytest.raises(DevicectlControlError, match="exit 1"):
        await adapter.capture_screenshot(_UDID)
    # 失败不留空文件诱饵
    assert list((tmp_path / "shots").glob("*.png")) == []


async def test_launch_app_argv_with_purge_args(tmp_path):
    executor = FakeExecutor()
    adapter = DevicectlControlAdapter(executor, tmp_path / "work")
    await adapter.launch_app(
        _UDID, _BUNDLE, arguments=["--ak-purge-dates", "2026-11-14"])
    argv = executor.calls[0]
    assert argv[-3:] == [_BUNDLE, "--ak-purge-dates", "2026-11-14"]
    assert argv[argv.index("--device") + 1] == _UDID
    assert "--terminate-existing" not in argv

    await adapter.launch_app(_UDID, _BUNDLE, terminate_existing=True)
    assert "--terminate-existing" in executor.calls[1]


async def test_terminate_pid_argv_and_kill_flag(tmp_path):
    executor = FakeExecutor()
    adapter = DevicectlControlAdapter(executor, tmp_path / "work")
    await adapter.terminate_pid(_UDID, 4321)
    assert executor.calls[0] == [
        "xcrun", "devicectl", "device", "process", "terminate",
        "--device", _UDID, "--pid", "4321",
    ]
    await adapter.terminate_pid(_UDID, 4321, kill=True)
    assert executor.calls[1][-1] == "--kill"


async def test_terminate_nonzero_rc_is_loud(tmp_path):
    executor = FakeExecutor(results=[
        CommandResult(1, "", "no such process", [])])
    adapter = DevicectlControlAdapter(executor, tmp_path / "work")
    with pytest.raises(DevicectlControlError, match="exit 1"):
        await adapter.terminate_pid(_UDID, 999)
