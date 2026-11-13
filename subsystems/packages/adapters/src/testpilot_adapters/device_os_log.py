"""真机 os_log 两步采集适配器（LogQueryPort 真机实现，切片一，2026-11-13）。

真机没有 ``simctl spawn`` 通道，日志链路分两步（core/oslog_commands.py 纯函数拼 argv）：

1. ``/usr/bin/log collect --device-udid <UDID> --start ... --output <run>.logarchive``
   把设备日志采集成本机归档（collect 不支持 --end，窗口收口在第二步）；
2. ``/usr/bin/log show <archive> --start/--end/--predicate --style ndjson``
   对本机归档做与模拟器同口径的窗口/谓词过滤，NDJSON 解析复用同一模块函数。

提权与人工模式（用户现场约束：部分环境 collect 需 sudo，worker 进程没有 TTY）：

- ``collect_mode=auto``（默认）：平台直接起 collect；``use_sudo=True`` 时 argv 前置
  ``sudo -n``，凭据未缓存会**立即失败**而不是挂死等密码；
- ``collect_mode=manual``：平台永不发起 collect，要求人工按 query 回显里的命令预采集
  到指定归档路径；归档缺失即 AdapterUnavailableError 并在报错里给出照跑命令——
  fail-closed，绝不用空证据冒充查询成功。

归档按 run_id 命名落在 profile 配置的 work_dir（gitignored store/ 内），是事后归因的
原始证据，不自动删除；保留/清理策略随 store 制品生命周期人工管理。
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from testpilot_core.errors import AdapterUnavailableError, SchemaValidationError
from testpilot_core.oslog_commands import (
    COLLECT_MODE_AUTO,
    COLLECT_MODE_MANUAL,
    COLLECT_MODES,
    build_device_os_log_query,
    filter_window,
)

from .os_log import parse_ndjson
from .process_executor import SubprocessExecutor

# run_id 进归档文件名，白名单防路径穿越（实际 run_id 形如 run-<sha1>，这里是防线）
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_ARCHIVE_SUFFIX = ".logarchive"


class DeviceOsLogQueryAdapter:
    provider = "ios-device-oslog"

    def __init__(
        self,
        udid: str,
        work_dir: str | Path,
        *,
        collect_mode: str = COLLECT_MODE_AUTO,
        use_sudo: bool = False,
        executor=None,
    ) -> None:
        if not isinstance(udid, str) or not udid.strip():
            raise SchemaValidationError(
                "DeviceOsLogQueryAdapter 缺少 udid：拒绝隐式设备目标"
            )
        if collect_mode not in COLLECT_MODES:
            raise SchemaValidationError(
                f"非法 collect_mode（白名单 {sorted(COLLECT_MODES)}）: {collect_mode!r}"
            )
        self.udid = udid.strip()
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.collect_mode = collect_mode
        self.use_sudo = use_sudo
        self._executor = executor or SubprocessExecutor()

    def _archive_path(self, run_id: str) -> Path:
        if not _RUN_ID_RE.match(run_id):
            raise SchemaValidationError(
                f"非法 run_id（只允许字母数字 . _ -）: {run_id!r}"
            )
        return self.work_dir / f"{run_id}{_ARCHIVE_SUFFIX}"

    async def query(
        self,
        *,
        run_id: str,
        predicate: str,
        start_time: str,
        end_time: str,
    ) -> dict:
        archive = self._archive_path(run_id)
        # 入参校验/双 argv 拼装全部在 core 纯函数内，非法窗口/谓词先于进程响亮失败
        log_query = build_device_os_log_query(
            udid=self.udid,
            predicate=predicate,
            start_time=start_time,
            end_time=end_time,
            archive_path=str(archive),
            collect_mode=self.collect_mode,
            use_sudo=self.use_sudo,
        )

        if self.collect_mode == COLLECT_MODE_MANUAL:
            if not archive.exists():
                human_cmd = " ".join(log_query.collect_argv)
                raise AdapterUnavailableError(
                    "manual 采集模式：真机日志归档不存在，需人工先执行 collect："
                    f"{human_cmd}（完成后重跑本查询；平台不代提权、不静默跳过）"
                )
        else:
            # 与 xcresult bundle 同口径：先删后采，防陈旧归档掩盖本次采集失败
            if archive.exists():
                shutil.rmtree(archive)
            collect = await self._executor.run(log_query.collect_argv)
            if collect.returncode != 0:
                hint = ""
                if self.use_sudo:
                    hint = "（sudo -n 失败：请先人工 sudo -v 缓存凭据，或改用 manual 模式）"
                raise AdapterUnavailableError(
                    f"log collect 失败 rc={collect.returncode} run_id={run_id}{hint} "
                    f"stderr={collect.stderr[:300]}"
                )
            if not archive.exists():
                # rc=0 但没有归档：工具链/设备会话异常，按适配器不可用收口
                raise AdapterUnavailableError(
                    f"log collect 退出 0 但归档缺失: {archive}（设备断连或磁盘异常？）"
                )

        show = await self._executor.run(log_query.show_argv)
        if show.returncode != 0:
            raise AdapterUnavailableError(
                f"log show 归档失败 rc={show.returncode} run_id={run_id} "
                f"stderr={show.stderr[:300]}"
            )
        entries, samples, total_bad = parse_ndjson(show.stdout)
        kept = filter_window(entries, log_query.start_time, log_query.end_time)
        return {
            "run_id": run_id,
            "entries": kept,
            "dropped_out_of_window": len(entries) - len(kept),
            "bad_lines": total_bad,
            "bad_line_samples": samples,
            "archive_path": str(archive),
            "query": log_query.echo(),
        }
