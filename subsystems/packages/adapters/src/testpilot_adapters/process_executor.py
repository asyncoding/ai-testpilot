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

    async def wait(self) -> int:
        return await self._process.wait()

    def terminate(self) -> None:
        self._terminated = True
        self._process.terminate()

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
