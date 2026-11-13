"""DhAxAdapter 适配器 hermetic 测试（docs/15 §3/§9，H3）。

零真机：FakeExecutor 脚本化返回，钉死四件事——
① argv 列表契约（xcrun swift <cli> <verb>，永不 shell）；
② CLI 退出码到异常的映射（2/6 环境不可用，3/4/5 fail-loud）；
③ 输出必须是末行 JSON，0 退出却 ok=false 按契约破坏处理；
④ rotate-left 的 --times 仅在非 1 时出现，非法 times 不起进程直接拒。
"""

from __future__ import annotations

import json

import pytest

from testpilot_adapters.dh_ax import DhAxActionError, DhAxAdapter
from testpilot_adapters.process_executor import CommandResult
from testpilot_core.errors import AdapterUnavailableError

_CLI = "/repo/subsystems/scripts/device_hub_ax_cli.swift"


class FakeExecutor:
    def __init__(self, results: list[CommandResult]):
        self._results = list(results)
        self.calls: list[list[str]] = []

    async def run(self, argv: list[str], *, cwd=None) -> CommandResult:
        self.calls.append(list(argv))
        return self._results.pop(0)


def _ok(verb: str, **extra) -> CommandResult:
    payload = {"ok": True, "verb": verb, "pid": 83446, **extra}
    return CommandResult(0, json.dumps(payload), "", ["xcrun"])


def _fail(returncode: int, reason: str, **extra) -> CommandResult:
    payload = {"ok": False, "reason": reason, **extra}
    return CommandResult(returncode, json.dumps(payload), "stderr line", ["xcrun"])


async def test_readiness_passthrough_and_argv_contract():
    executor = FakeExecutor([_ok("readiness", screen_locked=False, buttons={
        "home": True, "screenshot": True, "rotate_left": True})])
    adapter = DhAxAdapter(executor, _CLI)

    report = await adapter.readiness()
    assert report["buttons"]["home"] is True
    argv = executor.calls[0]
    assert argv == ["xcrun", "swift", _CLI, "readiness"]
    assert all(isinstance(part, str) for part in argv)  # 列表 argv，无 shell 拼接


async def test_home_and_screenshot_argv():
    executor = FakeExecutor([_ok("home"), _ok("screenshot")])
    adapter = DhAxAdapter(executor, _CLI)
    await adapter.home()
    await adapter.screenshot()
    assert executor.calls[0][-1] == "home"
    assert executor.calls[1][-1] == "screenshot"


async def test_rotate_left_times_argv_rules():
    executor = FakeExecutor([_ok("rotate-left", times=1),
                             _ok("rotate-left", times=3)])
    adapter = DhAxAdapter(executor, _CLI)

    await adapter.rotate_left()
    assert executor.calls[0] == ["xcrun", "swift", _CLI, "rotate-left"]

    await adapter.rotate_left(3)
    assert executor.calls[1] == [
        "xcrun", "swift", _CLI, "rotate-left", "--times", "3"]

    with pytest.raises(DhAxActionError, match="正整数"):
        await adapter.rotate_left(0)
    with pytest.raises(DhAxActionError, match="正整数"):
        await adapter.rotate_left(-2)
    assert len(executor.calls) == 2  # 非法参数不起进程


@pytest.mark.parametrize("code", [2, 6])
async def test_environment_unavailable_mapped(code):
    executor = FakeExecutor([
        _fail(code, "DeviceHub 进程未找到" if code == 2 else "Mac 会话锁屏")])
    adapter = DhAxAdapter(executor, _CLI)
    with pytest.raises(AdapterUnavailableError, match=f"exit {code}"):
        await adapter.readiness()


@pytest.mark.parametrize("code", [3, 4, 5])
async def test_action_failures_are_loud(code):
    executor = FakeExecutor([_fail(code, "工具条按钮不可达")])
    adapter = DhAxAdapter(executor, _CLI)
    with pytest.raises(DhAxActionError, match=f"exit {code}"):
        await adapter.home()


async def test_rc0_with_ok_false_is_contract_breach():
    executor = FakeExecutor([
        CommandResult(0, json.dumps({"ok": False, "verb": "home"}), "", [])])
    adapter = DhAxAdapter(executor, _CLI)
    with pytest.raises(DhAxActionError, match="ok=false"):
        await adapter.home()


async def test_non_json_output_is_loud():
    executor = FakeExecutor([
        CommandResult(0, "some random log without json\n", "", [])])
    adapter = DhAxAdapter(executor, _CLI)
    with pytest.raises(DhAxActionError, match="无法解析为 JSON"):
        await adapter.home()


async def test_unexpected_exit_code_is_loud():
    executor = FakeExecutor([_fail(137, "被外部杀掉")])
    adapter = DhAxAdapter(executor, _CLI)
    with pytest.raises(DhAxActionError, match="未预期退出码 137"):
        await adapter.home()
