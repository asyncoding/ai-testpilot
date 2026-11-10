"""SubprocessExecutor 真实进程层测试（hostless：只用本机 python，无模拟器/端口）。

FakeExecutor/FakeSpawner 覆盖不到内核管道行为，这类问题只能用真进程复现。
"""

from __future__ import annotations

import asyncio
import sys

from testpilot_adapters.process_executor import SubprocessExecutor


async def test_spawn_drains_pipes_so_chatty_process_does_not_deadlock():
    """红证（2026-11-10 真机探针预演）：spawn 接 PIPE 但 wait 不排空，
    子进程输出超过内核管道缓冲（约 64KB）即阻塞在写端永不退出。

    真 xcodebuild 日志数 MB，不修此问题任何真实测试运行都会挂死。
    """
    executor = SubprocessExecutor()
    proc = await executor.spawn(
        [
            sys.executable,
            "-c",
            "import sys; "
            "sys.stdout.write('x' * (2 * 1024 * 1024)); "
            "sys.stderr.write('y' * 1024)",
        ]
    )
    # 挂死时让测试快速红，而不是等默认超时
    rc = await asyncio.wait_for(proc.wait(), timeout=15)
    assert rc == 0


async def test_spawn_terminate_marks_and_signals_sleeping_process():
    executor = SubprocessExecutor()
    proc = await executor.spawn([sys.executable, "-c", "import time; time.sleep(30)"])
    assert proc.terminated is False
    proc.terminate()
    assert proc.terminated is True
    # 被 SIGTERM 的进程 wait 必须能收口（负返回码即被信号杀死），不留僵尸
    rc = await asyncio.wait_for(proc.wait(), timeout=10)
    assert rc != 0


async def test_spawn_interrupt_sends_sigint_and_reaps():
    """红证背景：simctl io recordVideo 只认 SIGINT 优雅收尾，SIGTERM 会泄漏
    宿主侧录制句柄（Resource busy，2026-11-10 真机探针取证）。interrupt()
    必须发 SIGINT 且 wait 能收口，terminate() 的 SIGTERM 语义保持不变。
    """
    executor = SubprocessExecutor()
    proc = await executor.spawn([sys.executable, "-c", "import time; time.sleep(30)"])
    proc.interrupt()
    assert proc.terminated is True
    rc = await asyncio.wait_for(proc.wait(), timeout=10)
    # POSIX：被信号 N 杀死返回 -N；SIGINT=2
    assert rc == -2
