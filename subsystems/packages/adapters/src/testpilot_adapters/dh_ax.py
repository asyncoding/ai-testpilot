"""Device Hub AX 语义刺激适配器（docs/01 §3，H3）。

进程出口唯一走注入的 ProcessExecutor（argv 列表，永不 shell），调用入库的
``scripts/device_hub_ax_cli.swift`` 四动词：readiness/home/rotate-left/screenshot。
真机证据（docs/archive/04（EVD-20261113-02） 附录 B.4，E24–E31）：

- AXPress 语义通道在真机有效，HID/CGEvent 合成输入惰性——本适配器不暴露 HID；
- Mac 锁屏/工具条脱物化/元素缺失时 CLI 非零退出，本适配器 fail-loud，
  绝不把 AX err=0 当成功；设备侧动作效果由编排层 devicectl 截图对照判读。

退出码映射（与 CLI 头注释契约一致）：
  0 成功；2 DeviceHub 进程缺失、6 AX 未授权/锁屏 → AdapterUnavailableError（503 语义）；
  3 参数非法、4 元素缺失/窗口零尺寸、5 AXPress 失败 → DhAxActionError（fail-loud）。
"""

from __future__ import annotations

import json
from pathlib import Path

from testpilot_core.errors import AdapterUnavailableError

_UNAVAILABLE_EXITS = {2, 6}
_ACTION_FAILURE_EXITS = {3, 4, 5}


class DhAxActionError(RuntimeError):
    """CLI 动词执行失败（元素缺失/AXPress 失败/参数非法）；调用方按 blocked/fail 收口。"""


class DhAxAdapter:
    def __init__(
        self,
        executor,
        cli_path: str | Path,
        *,
        xcrun: str = "xcrun",
    ) -> None:
        self._executor = executor
        self._cli_path = str(cli_path)
        self._xcrun = xcrun

    def _argv(self, verb: str, *extra: str) -> list[str]:
        return [self._xcrun, "swift", self._cli_path, verb, *extra]

    async def _run(self, verb: str, *extra: str) -> dict:
        result = await self._executor.run(self._argv(verb, *extra))
        payload = self._parse_json_line(result.stdout, verb)
        if result.returncode == 0:
            if not payload.get("ok"):
                # CLI 契约：0 退出必须 ok=true；不一致就是契约破坏，响亮失败
                raise DhAxActionError(
                    f"DH AX {verb} 退出 0 但 ok=false：{payload}")
            return payload
        reason = payload.get("reason", "未知原因")
        if result.returncode in _UNAVAILABLE_EXITS:
            raise AdapterUnavailableError(
                f"DH AX {verb} 环境不可用（exit {result.returncode}）：{reason}")
        if result.returncode in _ACTION_FAILURE_EXITS:
            raise DhAxActionError(
                f"DH AX {verb} 失败（exit {result.returncode}）：{reason}")
        raise DhAxActionError(
            f"DH AX {verb} 未预期退出码 {result.returncode}：{reason}")

    @staticmethod
    def _parse_json_line(stdout: str, verb: str) -> dict:
        # CLI 契约：stdout 最后一行是单个 JSON 对象；前面不应有其他输出，
        # 解析失败按 fail-loud 处理，不猜测、不吞 stderr。
        last = stdout.strip().splitlines()[-1] if stdout.strip() else ""
        try:
            payload = json.loads(last)
        except (json.JSONDecodeError, IndexError) as exc:
            raise DhAxActionError(
                f"DH AX {verb} 输出无法解析为 JSON：{stdout[:200]!r}") from exc
        if not isinstance(payload, dict):
            raise DhAxActionError(f"DH AX {verb} 输出 JSON 顶层不是对象")
        return payload

    async def readiness(self) -> dict:
        """只读就绪探针：进程/授权/锁屏/窗口物化/工具条三按钮可达性。"""
        return await self._run("readiness")

    async def home(self) -> dict:
        return await self._run("home")

    async def rotate_left(self, times: int = 1) -> dict:
        if not isinstance(times, int) or times <= 0:
            raise DhAxActionError("rotate_left 的 times 必须是正整数")
        extra = ("--times", str(times)) if times != 1 else ()
        return await self._run("rotate-left", *extra)

    async def screenshot(self) -> dict:
        return await self._run("screenshot")
