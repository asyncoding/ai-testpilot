"""devicectl 设备控制面适配器（docs/01 §3/§4，H3）。

混合编排里 auto:devicectl 步骤与前置探针的唯一真机控制出口：截图（方向/
现场证据）、App 启动（段内 purge 透传）、按 PID 终止进程。argv 全部由
core.devicectl_commands 纯函数拼好，本层只做进程调用与 rc 收口，纪律同
DhAxAdapter：argv 列表永不 shell、工具链缺席由 executor 翻译成
AdapterUnavailableError、业务失败（rc != 0）响亮抛 DevicectlControlError，
绝不静默跳过——设备侧刺激没有"假装成功"这条路。

本机取证约束（2026-11-13/14，Xcode 27.0）：
- devicectl 词汇表没有进程清单动词，terminate 只能按显式 PID；主机侧无法
  可靠回答"App 是否在跑"，app_not_running 探针因此只判 unknown（docs/01 §4）；
- 截图产物是 PNG，方向结论由 core.png_probe 读像素宽高，不看任何按压计数。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

from testpilot_core.devicectl_commands import (
    build_process_launch_argv,
    build_process_terminate_argv,
    build_screenshot_argv,
)


class DevicectlControlError(RuntimeError):
    """devicectl 动词 rc != 0：设备侧动作未确认成功，调用方按 blocked/fail 收口。"""


class DevicectlControlAdapter:
    def __init__(self, executor, work_dir: str | Path) -> None:
        self._executor = executor
        self._work_dir = Path(work_dir)
        self._work_dir.mkdir(parents=True, exist_ok=True)

    async def _run(self, argv: list[str]) -> str:
        result = await self._executor.run(argv)
        if result.returncode != 0:
            raise DevicectlControlError(
                f"devicectl 失败 exit {result.returncode}：{' '.join(argv)}；"
                f"stderr={result.stderr.strip()[:300]}")
        return result.stdout

    async def capture_screenshot(self, udid: str) -> Path:
        """截图落 work_dir，文件名带 UTC 时间戳+随机段保证同会话多次不互覆。"""
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        destination = self._work_dir / f"shot-{stamp}-{uuid.uuid4().hex[:8]}.png"
        argv = build_screenshot_argv(udid, str(destination))
        await self._run(argv)
        if not destination.exists() or destination.stat().st_size == 0:
            # rc=0 却没有产物 = 工具契约破坏，fail-loud，不让上层拿空文件判方向
            raise DevicectlControlError(
                f"devicectl 截图退出 0 但产物缺失/为空：{destination}")
        return destination

    async def launch_app(
        self,
        udid: str,
        bundle_id: str,
        *,
        arguments: list[str] | None = None,
        terminate_existing: bool = False,
    ) -> str:
        argv = build_process_launch_argv(
            udid, bundle_id,
            arguments=list(arguments or []),
            terminate_existing=terminate_existing)
        return await self._run(argv)

    async def terminate_pid(
        self, udid: str, pid: int, *, kill: bool = False
    ) -> str:
        argv = build_process_terminate_argv(udid, pid, kill=kill)
        return await self._run(argv)
