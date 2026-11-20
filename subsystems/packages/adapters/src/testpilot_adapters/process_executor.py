"""子进程执行器（S3.2）：xcodebuild/simctl 的唯一进程出口，测试可替换为假件。

纪律：
- 永远 argv 列表 + ``create_subprocess_exec``，不走 shell，从根上杜绝命令注入；
- 命令不存在（xcrun/xcodebuild 未装）统一翻译成 AdapterUnavailableError，
  上层按"环境不可用"收口而不是崩溃；
- 业务失败（xcodebuild 测试失败返回非 0）不抛——由调用方结合产物判断，
  只有"工具链本身起不来"才是适配器不可用。
"""

from __future__ import annotations

import asyncio
import signal
from dataclasses import dataclass

from testpilot_core.errors import AdapterUnavailableError


@dataclass
class CommandResult:
    returncode: int
    stdout: str
    stderr: str
    argv: list[str]


class SubprocessProcess:
    """asyncio 子进程句柄的薄包装，对齐测试假件的 ProcessHandle 接口。"""

    def __init__(self, process: asyncio.subprocess.Process, argv: list[str]) -> None:
        self._process = process
        self.argv = argv
        self.pid = process.pid
        self._terminated = False
        self._returncode: int | None = None

    async def wait(self) -> int:
        if self._returncode is not None:
            return self._returncode
        # 必须排空 stdout/stderr：子进程输出超过内核管道缓冲（约 64KB）后会阻塞
        # 在写端永不退出——真实 xcodebuild 日志数 MB。2026-11-10 真机探针用 2MB
        # 吵闹进程复现挂死（红证 tests/test_process_executor.py）。当前接口不暴露
        # 日志内容，排空即弃；日志留存随 S3.5 证据链再做。
        await self._process.communicate()
        self._returncode = self._process.returncode or 0
        return self._returncode

    def terminate(self) -> None:
        self._terminated = True
        self._process.terminate()

    def interrupt(self) -> None:
        """优雅停止（SIGINT）。

        真机探针取证（2026-11-10）：``simctl io recordVideo`` 只在 SIGINT 下
        完成 mp4 moov 原子写并释放宿主侧录制会话；SIGTERM 杀掉 xcrun 外壳后
        SimRenderServer 仍持有录制句柄，产出 0 字节文件且后续录制全报
        NSPOSIXErrorDomain Code=16 "Resource busy"，只能 shutdown 设备回收。
        交互式/需要收尾的子进程走这里，强杀语义仍用 terminate()。
        """
        self._terminated = True
        self._process.send_signal(signal.SIGINT)

    @property
    def terminated(self) -> bool:
        return self._terminated


class SubprocessExecutor:
    """真实子进程执行器；hermetic 测试用同接口的 FakeExecutor 替换。"""

    async def run(self, argv: list[str], *, cwd: str | None = None) -> CommandResult:
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise AdapterUnavailableError(
                f"命令不存在（工具链未安装？）: {argv[0]}"
            ) from exc
        stdout, stderr = await process.communicate()
        return CommandResult(
            returncode=process.returncode or 0,
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
            argv=list(argv),
        )

    async def spawn(
        self, argv: list[str], *, cwd: str | None = None
    ) -> SubprocessProcess:
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise AdapterUnavailableError(
                f"命令不存在（工具链未安装？）: {argv[0]}"
            ) from exc
        return SubprocessProcess(process, list(argv))

    async def spawn_attached(
        self, argv: list[str], *, cwd: str | None = None
    ) -> SubprocessProcess:
        """交互式子进程出口：stdin=None 继承父进程 TTY，stdout/stderr 仍 PIPE。

        ws-run 的闸门驱动走这里：结构跑（--structural）零提问、全自动，是本入口
        的受支持形态；人工模式下 input() 可读继承来的终端 stdin，但提示语随
        stdout 入管，全 TTY 人工会话应直接在独立终端运行 testpilot-ws-gate-driver
        （docs/01 已写死这条边界）。out/err 保持 PIPE 是为沿用 communicate 排空
        纪律，防止驱动日志撑爆管道缓冲反压协议帧。
        """
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=cwd,
                stdin=None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise AdapterUnavailableError(
                f"命令不存在（工具链未安装？）: {argv[0]}"
            ) from exc
        return SubprocessProcess(process, list(argv))
