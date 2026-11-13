"""真机 IosDeviceEnvironment 适配器测试（假执行器，零真机；devicectl JSON 形态照取证裁剪）。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from testpilot_adapters.ios_device_env import (
    IosDeviceEnvironment,
    PhysicalDevice,
)
from testpilot_adapters.process_executor import CommandResult
from testpilot_core.errors import (
    AdapterUnavailableError,
    NotFoundError,
    PolicyDeniedError,
    UnsupportedCapabilityError,
)
from testpilot_core.models import EnvironmentRequirements
from testpilot_core.ports import EnvironmentPort

_T0 = datetime(2026, 11, 13, 9, 0, tzinfo=timezone.utc)
_PHYSICAL_UDID = "00008140-001A39590162801C"


class FakeResult:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.argv = []


class FakeExecutor:
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
        PhysicalDevice(
            udid=_PHYSICAL_UDID, name="iPhone 16 Pro",
            capabilities=["ios.device", "ui.automation", "background_audio",
                          "codesigning"]),
        PhysicalDevice(
            udid="00008140-0000000000000000", name="iPhone 15",
            capabilities=["ios.device", "ui.automation"]),
    ]


def _env(devices=None, *, executor=None, lease_timeout_s=900, clock=None):
    clock = clock or [_T0]
    return IosDeviceEnvironment(
        devices or _devices(),
        executor=executor or FakeExecutor(),
        lease_timeout_s=lease_timeout_s,
        now=lambda: clock[0],
    ), clock


def _req(*caps, request_id="req-1"):
    return EnvironmentRequirements(
        capabilities=list(caps), labels={"request_id": request_id}
    )


def _devicectl_payload(*, udid: str, state: str, reality: str = "physical") -> str:
    return json.dumps({
        "info": {},
        "result": {"devices": [{
            "identifier": "9D3A93E6-73B7-5504-AA20-A4E6C5714D43",
            "visibilityClass": "default",
            "properties": {
                "connection": {"state": state, "transportType": "wired"},
                "hardware": {"platform": "iOS", "reality": reality, "udid": udid},
                "software": {},
                "state": {"bootState": "booted", "name": "iPhone"},
            },
        }]},
    })


def test_implements_environment_port_and_uses_device_provider():
    env, _ = _env()
    assert isinstance(env, EnvironmentPort)
    assert env.provider == "ios-device"


async def test_empty_inventory_rejected_at_construction():
    with pytest.raises(AdapterUnavailableError, match="真机设备清单为空"):
        IosDeviceEnvironment([], executor=FakeExecutor())


async def test_acquire_picks_idle_device_and_all_busy_is_adapter_error():
    env, _ = _env()
    lease_a = await env.acquire(_req("ios.device"))
    assert env.env_id_for_lease(lease_a) == _PHYSICAL_UDID
    lease_b = await env.acquire(_req("ios.device", request_id="req-2"))
    assert {l.env_id for l in env.active_leases()} == {
        _PHYSICAL_UDID, "00008140-0000000000000000"
    }
    assert lease_a != lease_b
    with pytest.raises(AdapterUnavailableError, match="空闲真机"):
        await env.acquire(_req("ios.device", request_id="req-3"))


async def test_device_only_capabilities_accepted_when_inventory_labels_them():
    env, _ = _env(devices=[_devices()[0]])
    lease = await env.acquire(_req("background_audio", "codesigning"))
    assert env.active_leases()[0].lease_id == lease


async def test_device_only_capability_still_denied_without_label():
    executor = FakeExecutor()
    env, _ = _env(devices=[_devices()[1]], executor=executor)  # 第二台无 background_audio
    with pytest.raises(UnsupportedCapabilityError, match="设备清单不提供"):
        await env.acquire(_req("background_audio"))
    assert env.active_leases() == []
    assert executor.calls == []  # 能力不过不碰 devicectl


async def test_unknown_capability_denied_not_retriable():
    env, _ = _env()
    with pytest.raises(UnsupportedCapabilityError):
        await env.acquire(_req("gpu.metal"))


async def test_reclaim_drops_bookkeeping_without_touching_device():
    clock = [_T0]
    executor = FakeExecutor()
    env, _ = _env(executor=executor, clock=clock, lease_timeout_s=900)
    lease = await env.acquire(_req("ios.device"))
    clock[0] = _T0 + timedelta(seconds=899)
    assert await env.reclaim_expired() == []
    clock[0] = _T0 + timedelta(seconds=900)
    reclaimed = await env.reclaim_expired()
    assert [l.lease_id for l in reclaimed] == [lease]
    # 真机不属本进程：回收只摘登记，没有 shutdown/任何子进程
    assert executor.calls == []
    new_lease = await env.acquire(_req("ios.device", request_id="req-2"))
    assert env.active_leases()[0].lease_id == new_lease


async def test_reset_is_always_denied_even_without_lease():
    executor = FakeExecutor()
    env, _ = _env(executor=executor)
    with pytest.raises(PolicyDeniedError, match="禁止自动重置"):
        await env.reset(_PHYSICAL_UDID)
    assert executor.calls == []  # 守卫挡在任何进程调用之前


async def test_health_check_connected_physical_device_is_true():
    executor = FakeExecutor([
        FakeResult(stdout=_devicectl_payload(udid=_PHYSICAL_UDID, state="connected"))
    ])
    env, _ = _env(executor=executor)
    assert await env.health_check(_PHYSICAL_UDID) is True
    # 盘点 argv 走 core 纯函数口径：JSON stdout、无隐式目标
    assert executor.calls[0] == [
        "xcrun", "devicectl", "list", "devices", "--json-output", "-"
    ]


async def test_health_check_disconnected_device_is_false():
    executor = FakeExecutor([
        FakeResult(stdout=_devicectl_payload(udid=_PHYSICAL_UDID, state="disconnected"))
    ])
    env, _ = _env(executor=executor)
    assert await env.health_check(_PHYSICAL_UDID) is False


async def test_health_check_simulator_udid_collision_is_false():
    # 即使 UDID 撞上模拟器条目，reality 守卫也不许把模拟器当真机健康
    executor = FakeExecutor([
        FakeResult(stdout=_devicectl_payload(
            udid=_PHYSICAL_UDID, state="connected", reality="simulated"))
    ])
    env, _ = _env(executor=executor)
    assert await env.health_check(_PHYSICAL_UDID) is False


async def test_health_check_unknown_udid_nonzero_rc_and_bad_json_all_false():
    env, _ = _env()
    # 清单外设备：不发起调用直接 False
    assert await env.health_check("00000000-0000000000000000") is False

    executor = FakeExecutor([FakeResult(returncode=1, stderr="boom")])
    env_bad_rc, _ = _env(executor=executor)
    assert await env_bad_rc.health_check(_PHYSICAL_UDID) is False

    executor_json = FakeExecutor([FakeResult(stdout="{not json")])
    env_bad_json, _ = _env(executor=executor_json)
    assert await env_bad_json.health_check(_PHYSICAL_UDID) is False


async def test_release_idempotent_and_unknown_lease_lookup_loud():
    env, _ = _env()
    lease = await env.acquire(_req("ios.device"))
    await env.release(lease)
    await env.release(lease)
    await env.release("envlease-ghost")
    assert env.active_leases() == []
    with pytest.raises(NotFoundError, match="租约"):
        env.env_id_for_lease(lease)


async def test_real_executor_commandresult_shape_compat():
    # 静态对账：假件与真实 CommandResult 同构（防假件接口漂移给出假绿）
    result = CommandResult(0, "{}", "", ["xcrun"])
    assert result.returncode == 0 and result.argv[0] == "xcrun"
