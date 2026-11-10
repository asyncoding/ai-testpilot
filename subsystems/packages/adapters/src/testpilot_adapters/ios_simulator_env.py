"""iOS 模拟器环境适配器（EnvironmentPort，S3.2 FR-EX-004）。

职责边界：只管"选哪台模拟器、租约、健康检查、重置"，不碰 xcodebuild
（那是 XcodeTestRunner 的事）。所有 simctl 调用走可注入执行器，
hermetic 测试用假执行器逐字断言 argv，真机/真模拟器只在手动探针跑。

POC 范围：单 worker 进程内租约登记（内存）。多进程设备云是 P2
（profile java-enterprise/ios-device-lab），届时租约落库或走设备实验室服务；
进程重启后的兜底是 ``reclaim_expired`` + simctl shutdown（过期登记随进程消失，
模拟器 shutdown 是幂等操作，不会留下被锁死的设备）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Callable

from pydantic import BaseModel, Field

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
    simctl_reset_plan,
)

from .process_executor import SubprocessExecutor


class SimulatorDevice(BaseModel):
    udid: str
    name: str
    capabilities: list[str] = Field(default_factory=list)


class SimctlEnvironment:
    provider = "ios-simulator"

    def __init__(
        self,
        devices: list[SimulatorDevice],
        *,
        executor=None,
        lease_timeout_s: int = 300,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not devices:
            raise AdapterUnavailableError("模拟器设备清单为空，无法装配环境适配器")
        self._devices = list(devices)
        self._executor = executor or SubprocessExecutor()
        self._lease_timeout_s = lease_timeout_s
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._leases: dict[str, EnvLease] = {}
        self._held: dict[str, str] = {}  # udid -> lease_id

    # ── 租约回收（先自愈再分配，FR-EX-004 AC2）──────────────────────────────

    async def reclaim_expired(self) -> list[EnvLease]:
        """回收过期租约并尽力 shutdown 对应模拟器；未到期绝不抢。"""
        now = self._now()
        stale = reclaimable_env_leases(list(self._leases.values()), now=now)
        for lease in stale:
            self._leases.pop(lease.lease_id, None)
            self._held.pop(lease.env_id, None)
            # shutdown 幂等：设备可能已关，失败不掩盖回收结论
            try:
                await self._executor.run(
                    ["xcrun", "simctl", "shutdown", lease.env_id]
                )
            except AdapterUnavailableError:
                pass
        return stale

    def active_leases(self) -> list[EnvLease]:
        return list(self._leases.values())

    def env_id_for_lease(self, lease_id: str) -> str:
        lease = self._leases.get(lease_id)
        if lease is None:
            # 不能静默返回空 udid——那会让 runner 在无设备身份下裸跑
            raise NotFoundError(f"未知或已释放的环境租约: {lease_id}")
        return lease.env_id

    # ── EnvironmentPort ─────────────────────────────────────────────────────

    async def acquire(self, requirements: EnvironmentRequirements) -> str:
        await self.reclaim_expired()
        # 先对整份清单的能力并集判定（与占用状态无关）：真机独占/清单永不提供的
        # 能力重试也等不到，即使设备全被占也必须回能力拒绝，而非"全忙请重试"。
        # 真机探针 2026-11-10 抓出：held 设备在下方循环被跳过，曾让单设备被占时
        # decisions 为空、ios.device 请求误报 AdapterUnavailable（FR-EX-004 AC1）。
        inventory_caps = sorted(
            {cap for device in self._devices for cap in device.capabilities}
        )
        inventory_decision = evaluate_capabilities(
            requirements.capabilities, inventory_caps, provider=self.provider
        )
        if not inventory_decision.supported:
            raise UnsupportedCapabilityError(inventory_decision.message)
        decisions = []
        for device in self._devices:
            if device.udid in self._held:
                continue  # 已被其他执行占用，跳过而非排队抢锁
            decision = evaluate_capabilities(
                requirements.capabilities,
                device.capabilities,
                provider=self.provider,
            )
            decisions.append((device, decision))
            if decision.supported:
                now = self._now()
                # request_id 由 worker 经 labels 透传（端口签名只收 requirements）
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

        # 能走到这里说明清单并集满足能力要求：空 decisions 只能是设备全被占，
        # 属可重试的暂时不可用；非空则是唯一能满足的设备被占等逐机不匹配。
        if not decisions:
            raise AdapterUnavailableError("没有空闲模拟器（全部被占用），请稍后重试或扩容")
        raise UnsupportedCapabilityError(decisions[0][1].message)

    async def health_check(self, env_id: str) -> bool:
        if env_id not in {d.udid for d in self._devices}:
            return False
        result = await self._executor.run(
            ["xcrun", "simctl", "list", "devices"]
        )
        return result.returncode == 0

    async def reset(self, env_id: str) -> None:
        """shutdown→erase 重置（docs/03 §3）；持有活跃租约的设备禁止重置。"""
        active = self._held.get(env_id)
        if active is not None:
            raise PolicyDeniedError(
                f"模拟器 {env_id} 仍被租约 {active} 占用，禁止 erase（防误删在用环境）"
            )
        for argv in simctl_reset_plan(env_id):
            result = await self._executor.run(argv)
            if result.returncode != 0:
                raise AdapterUnavailableError(
                    f"模拟器重置失败 argv={argv} rc={result.returncode} "
                    f"stderr={result.stderr[:300]}"
                )

    async def release(self, lease_id: str) -> None:
        """释放租约；幂等——队列至少一次投递可能让同租约走两遍释放。"""
        lease = self._leases.pop(lease_id, None)
        if lease is not None:
            self._held.pop(lease.env_id, None)
