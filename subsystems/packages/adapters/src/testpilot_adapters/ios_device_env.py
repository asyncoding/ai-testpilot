"""iOS 真机环境适配器（EnvironmentPort，真机切片一，2026-11-13）。

与模拟器适配器（ios_simulator_env.py）同构、职责边界相同：只管"选哪台真机、
租约、健康检查"，不碰 xcodebuild（XcodeTestRunner 按 destination_platform=iOS
拼真机目的地）。差异点都是真机物理约束决定的：

- 盘点走 ``devicectl list devices --json-output -``（core 纯函数解析），连接态白名单
  只认 ``connected``，未知态保守不可用，不猜测未来枚举；
- 能力判定 ``device_capable=True``：ios.device/background_audio/codesigning 等
  真机独占能力不再天然拒绝，但必须由 profile 清单**显式贴标签**才视为提供；
- **没有自动重置**：真机 erase 只能人工经系统设置/MDM，reset() 恒抛 PolicyDeniedError，
  平台不自动化任何破坏性设备操作；
- 过期回收只摘内存登记，不 shutdown 真机（设备不属于本进程，SimctlEnvironment 的
  simctl shutdown 语义在这里不成立）；
- 单 worker 进程内租约登记，多进程设备云仍是后续 P2。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Callable

from pydantic import BaseModel, Field

from testpilot_core.devicectl_commands import (
    build_devicectl_list_argv,
    find_device,
    parse_devicectl_devices,
)
from testpilot_core.errors import (
    AdapterUnavailableError,
    NotFoundError,
    PolicyDeniedError,
    UnsupportedCapabilityError,
)
from testpilot_core.models import EnvironmentRequirements
from testpilot_core.xcode_commands import (
    EnvLease,
    env_lease_id,
    evaluate_capabilities,
    reclaimable_env_leases,
)

from .process_executor import SubprocessExecutor


class PhysicalDevice(BaseModel):
    udid: str  # 硬件 UDID（properties.hardware.udid），不是 CoreDevice UUID
    name: str
    capabilities: list[str] = Field(default_factory=list)


class IosDeviceEnvironment:
    provider = "ios-device"

    def __init__(
        self,
        devices: list[PhysicalDevice],
        *,
        executor=None,
        lease_timeout_s: int = 900,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not devices:
            raise AdapterUnavailableError("真机设备清单为空，无法装配环境适配器")
        self._devices = list(devices)
        self._executor = executor or SubprocessExecutor()
        # 真机用例分钟级，默认租约比模拟器（300s）宽，profile 可覆盖
        self._lease_timeout_s = lease_timeout_s
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._leases: dict[str, EnvLease] = {}
        self._held: dict[str, str] = {}  # udid -> lease_id

    # ── 租约回收（只摘登记，不碰设备状态）────────────────────────────────────

    async def reclaim_expired(self) -> list[EnvLease]:
        now = self._now()
        stale = reclaimable_env_leases(list(self._leases.values()), now=now)
        for lease in stale:
            self._leases.pop(lease.lease_id, None)
            self._held.pop(lease.env_id, None)
        return stale

    def active_leases(self) -> list[EnvLease]:
        return list(self._leases.values())

    def env_id_for_lease(self, lease_id: str) -> str:
        lease = self._leases.get(lease_id)
        if lease is None:
            raise NotFoundError(f"未知或已释放的环境租约: {lease_id}")
        return lease.env_id

    # ── EnvironmentPort ─────────────────────────────────────────────────────

    async def acquire(self, requirements: EnvironmentRequirements) -> str:
        await self.reclaim_expired()
        # 与模拟器同口径：先对清单并集判定，设备独占能力在真机环境也要显式标签。
        inventory_caps = sorted(
            {cap for device in self._devices for cap in device.capabilities}
        )
        inventory_decision = evaluate_capabilities(
            requirements.capabilities,
            inventory_caps,
            provider=self.provider,
            device_capable=True,
        )
        if not inventory_decision.supported:
            raise UnsupportedCapabilityError(inventory_decision.message)
        decisions = []
        for device in self._devices:
            if device.udid in self._held:
                continue
            decision = evaluate_capabilities(
                requirements.capabilities,
                device.capabilities,
                provider=self.provider,
                device_capable=True,
            )
            decisions.append((device, decision))
            if decision.supported:
                now = self._now()
                request_id = requirements.labels.get("request_id", "")
                lease = EnvLease(
                    lease_id=env_lease_id(
                        device.udid, request_id or f"lease-{now.timestamp()}", now
                    ),
                    env_id=device.udid,
                    request_id=request_id,
                    acquired_at=now,
                    expires_at=now + timedelta(seconds=self._lease_timeout_s),
                )
                self._leases[lease.lease_id] = lease
                self._held[device.udid] = lease.lease_id
                return lease.lease_id

        if not decisions:
            raise AdapterUnavailableError("没有空闲真机（全部被占用），请稍后重试或扩容")
        raise UnsupportedCapabilityError(decisions[0][1].message)

    async def health_check(self, env_id: str) -> bool:
        """真机存活判定：devicectl 清单中存在该硬件 UDID 且 connection.state=connected。"""
        if env_id not in {d.udid for d in self._devices}:
            return False
        result = await self._executor.run(build_devicectl_list_argv())
        if result.returncode != 0:
            return False
        try:
            entries = parse_devicectl_devices(json.loads(result.stdout))
        except Exception:
            # 工具输出形态漂移/坏 JSON：保守判不可用，绝不基于猜测返回 True
            return False
        entry = find_device(entries, env_id)
        return entry is not None and entry.is_physical and entry.is_connected

    async def reset(self, env_id: str) -> None:
        """真机不支持平台侧自动重置（无 devicectl erase 动词；抹除只能人工/MDM）。

        恒拒绝而不是静默 no-op：调用方若依赖 reset 获得干净环境，必须显式感知这条
        边界并改走用例内启动参数/人工流程，防止脏环境假绿。
        """
        raise PolicyDeniedError(
            f"真机 {env_id} 禁止自动重置：抹除设备只能人工经系统设置或 MDM 执行，"
            "平台不自动化该破坏性操作；干净状态请用 App 启动参数或人工前置流程保证"
        )

    async def release(self, lease_id: str) -> None:
        """释放租约；幂等——队列至少一次投递可能让同租约走两遍。"""
        lease = self._leases.pop(lease_id, None)
        if lease is not None:
            self._held.pop(lease.env_id, None)
