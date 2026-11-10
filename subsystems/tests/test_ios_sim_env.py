"""S3.2 SimctlEnvironment 适配器测试（假执行器，零真机/真模拟器）。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from testpilot_adapters.ios_simulator_env import SimctlEnvironment, SimulatorDevice
from testpilot_adapters.process_executor import SubprocessExecutor
from testpilot_core.errors import (
    AdapterUnavailableError,
    NotFoundError,
    PolicyDeniedError,
    UnsupportedCapabilityError,
)
from testpilot_core.models import EnvironmentRequirements

_T0 = datetime(2026, 11, 10, 9, 0, tzinfo=timezone.utc)


class FakeResult:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.argv = []


class FakeExecutor:
    """记录全部 argv，按入队顺序返回脚本化结果，默认 rc=0。"""

    def __init__(self, results: list[FakeResult] | None = None):
        self.calls: list[list[str]] = []
        self._results = list(results or [])

    async def run(self, argv, *, cwd=None):
        self.calls.append(list(argv))
        result = self._results.pop(0) if self._results else FakeResult()
        result.argv = list(argv)
        return result


def _devices():
    return [
        SimulatorDevice(
            udid="UDID-A", name="iPhone 15",
            capabilities=["ios.simulator", "ui.automation", "hostless"]),
        SimulatorDevice(
            udid="UDID-B", name="iPhone SE",
            capabilities=["ios.simulator", "hostless"]),
    ]


def _env(devices=None, *, executor=None, lease_timeout_s=300, clock=None):
    clock = clock or [_T0]
    return SimctlEnvironment(
        devices or _devices(),
        executor=executor or FakeExecutor(),
        lease_timeout_s=lease_timeout_s,
        now=lambda: clock[0],
    ), clock


def _req(*caps, request_id="req-1"):
    return EnvironmentRequirements(
        capabilities=list(caps), labels={"request_id": request_id}
    )


async def test_empty_inventory_rejected_at_construction():
    with pytest.raises(AdapterUnavailableError, match="设备清单为空"):
        SimctlEnvironment([], executor=FakeExecutor())


async def test_acquire_picks_idle_device_and_skips_held_one():
    env, _ = _env()
    lease_a = await env.acquire(_req("ios.simulator"))
    assert env.active_leases()[0].env_id == "UDID-A"
    lease_b = await env.acquire(_req("ios.simulator", request_id="req-2"))
    held = {l.env_id for l in env.active_leases()}
    assert held == {"UDID-A", "UDID-B"} and lease_a != lease_b
    # 两台都占满 → 适配器不可用（不是能力拒绝）
    with pytest.raises(AdapterUnavailableError, match="空闲模拟器"):
        await env.acquire(_req("ios.simulator", request_id="req-3"))


async def test_device_only_capability_rejected_with_uncovered_layer():
    executor = FakeExecutor()
    env, _ = _env(executor=executor)
    with pytest.raises(UnsupportedCapabilityError, match="未覆盖层"):
        await env.acquire(_req("background_audio"))
    assert env.active_leases() == []  # 拒绝不留租约幻影
    assert executor.calls == []  # 能力不过不碰 simctl


async def test_device_only_rejected_even_when_only_device_is_held():
    """真机探针抓出（2026-11-10）：唯一设备被占用时，真机独占请求仍须能力拒绝。

    held 跳过不能让 device_only 优先分支失效——重试解决不了 ios.device，
    模拟器永远不提供该能力，回"全忙请重试"会误导调用方（FR-EX-004 AC1）。
    """
    executor = FakeExecutor()
    env, _ = _env(devices=[_devices()[0]], executor=executor)
    await env.acquire(_req("ios.simulator"))  # 唯一设备先被占
    with pytest.raises(UnsupportedCapabilityError, match="未覆盖层"):
        await env.acquire(_req("ios.device"))
    assert executor.calls == []  # 能力不过不碰 simctl


async def test_not_offered_capability_routed_to_capable_device_else_rejected():
    env, _ = _env()
    # ui.automation 只有 UDID-A 提供 → 跳过 B 直接选中 A
    lease = await env.acquire(_req("ui.automation"))
    assert env.active_leases()[0].lease_id == lease
    # 释放后再要一台清单里谁都没有的能力
    await env.release(lease)
    with pytest.raises(UnsupportedCapabilityError, match="设备清单不提供"):
        await env.acquire(_req("gpu.metal"))


async def test_expired_lease_reclaimed_and_device_shutdown_then_reusable():
    clock = [_T0]
    executor = FakeExecutor()
    env, _ = _env(executor=executor, clock=clock, lease_timeout_s=300)
    lease = await env.acquire(_req("ios.simulator"))
    clock[0] = _T0 + timedelta(seconds=299)
    assert await env.reclaim_expired() == []  # 未到期不抢
    clock[0] = _T0 + timedelta(seconds=300)
    reclaimed = await env.reclaim_expired()
    assert [l.lease_id for l in reclaimed] == [lease]
    assert executor.calls == [["xcrun", "simctl", "shutdown", "UDID-A"]]
    # 设备回收后可再次分配
    new_lease = await env.acquire(_req("ios.simulator", request_id="req-2"))
    assert len(env.active_leases()) == 1 and new_lease != lease


async def test_reset_blocked_while_leased_then_shutdown_erase_in_order():
    executor = FakeExecutor()
    env, _ = _env(executor=executor)
    await env.acquire(_req("ios.simulator"))
    with pytest.raises(PolicyDeniedError, match="禁止 erase"):
        await env.reset("UDID-A")
    assert executor.calls == []  # 守卫挡在 simctl 之前
    await env.release(env.active_leases()[0].lease_id)
    await env.reset("UDID-A")
    assert executor.calls == [
        ["xcrun", "simctl", "shutdown", "UDID-A"],
        ["xcrun", "simctl", "erase", "UDID-A"],
    ]


async def test_reset_failure_surfaces_adapter_error():
    executor = FakeExecutor(results=[FakeResult(), FakeResult(returncode=1, stderr="boom")])
    env, _ = _env(executor=executor)
    with pytest.raises(AdapterUnavailableError, match="模拟器重置失败"):
        await env.reset("UDID-A")


async def test_health_check_unknown_env_is_false_without_command():
    executor = FakeExecutor()
    env, _ = _env(executor=executor)
    assert await env.health_check("UDID-GHOST") is False
    assert executor.calls == []
    assert await env.health_check("UDID-A") is True


async def test_health_check_nonzero_rc_is_false():
    executor = FakeExecutor(results=[FakeResult(returncode=1)])
    env, _ = _env(executor=executor)
    assert await env.health_check("UDID-A") is False


async def test_release_is_idempotent():
    env, _ = _env()
    lease = await env.acquire(_req("ios.simulator"))
    await env.release(lease)
    await env.release(lease)  # 至少一次投递下的重复释放，不抛
    await env.release("envlease-ghost")  # 未知租约同样幂等
    assert env.active_leases() == []


async def test_env_id_for_lease_resolves_udid_and_unknown_lease_raises():
    env, _ = _env()
    lease_id = await env.acquire(_req("ios.simulator"))
    assert env.env_id_for_lease(lease_id) == "UDID-A"
    # 已释放/未知租约必须响亮失败：静默返回空串会导致 runner 无 udid 裸跑
    await env.release(lease_id)
    with pytest.raises(NotFoundError, match="租约"):
        env.env_id_for_lease(lease_id)
    with pytest.raises(NotFoundError):
        env.env_id_for_lease("envlease-ghost")


async def test_real_executor_missing_binary_maps_to_adapter_error():
    with pytest.raises(AdapterUnavailableError, match="命令不存在"):
        await SubprocessExecutor().run(["/nonexistent/tp-fake-binary-xyz"])
