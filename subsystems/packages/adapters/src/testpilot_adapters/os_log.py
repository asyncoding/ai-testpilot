"""os_log 日志查询适配器（LogQueryPort 的 iOS 模拟器实现，FR-EX-006，S3.3）。

封装 ``xcrun simctl spawn <udid> log show --style ndjson``（真机校准：
``--style json`` 输出美化打印的 JSON 数组，无法逐行解析；ndjson 才是一行一条）：

- argv 拼装、时间窗校验全部复用 core 纯函数（oslog_commands.py），本类只做
  进程调用与 NDJSON 行解析；
- 返回体 ``{"entries", "query", "bad_lines", ...}``：query 节原样回显
  udid/predicate/时间窗/argv，任何时候都能照着重跑（AC1）；
- 服务端窗口之外再用 core 纯函数过滤一遍（纵深防御，AC2：不整量灌入知识库）；
- 坏行策略：单行 JSON 损坏/时间戳非法不计入 entries，但总数+留首条样片，
  既不因单行坏数据废掉整窗证据，也不静默丢弃。

真实 ``log show --style ndjson`` 的字段形态已经 2026-11-10 专用模拟器探针
校准（timestamp 形如 ``2026-11-10 21:40:31.506619+0800``，见 handoff）。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from testpilot_core.errors import AdapterUnavailableError, SchemaValidationError
from testpilot_core.oslog_commands import build_os_log_query, filter_window

from .process_executor import SubprocessExecutor

# 坏行样片最多留几条进返回体，防止畸形输出撑爆响应；总数不受此限
_BAD_LINE_SAMPLE_LIMIT = 3
_SNIPPET_LEN = 200


def _is_envelope_trailer(entry: dict[str, Any]) -> bool:
    """识别 ``log show --style ndjson`` 流末尾的工具汇总尾行。

    2026-11-10 专用模拟器探针取证：每次查询最后一行固定为
    ``{"count":N,"finished":1}``（N 恰为真实条目数），无 timestamp，
    是工具信封而非坏数据——当坏行会让每次真查凭空多 1 条 bad_lines 污染
    质量门禁。只放行 count+finished 双键完整形态，缺键的疑似尾行不识别，
    仍按坏行 fail-closed（未观测形态不擅自放行）。
    """
    return "timestamp" not in entry and "count" in entry and "finished" in entry


def parse_ndjson(
    stdout: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    """拆行解析 NDJSON；坏行计数留片（样片上限 3），不中断整窗查询。

    模拟器 ``simctl spawn log show`` 与真机 ``log show <archive>`` 的 ndjson
    形态同构（2026-11-13 两步法复用），故提为模块函数供两个适配器共用。
    """
    entries: list[dict[str, Any]] = []
    samples: list[dict[str, Any]] = []
    total_bad = 0
    for line_no, raw in enumerate(stdout.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        reason = ""
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            reason = "JSON 解析失败"
        else:
            if not isinstance(entry, dict):
                reason = "行顶层不是对象"
            elif _is_envelope_trailer(entry):
                continue
            else:
                ts = entry.get("timestamp")
                if not isinstance(ts, str):
                    reason = "缺少 timestamp 字符串"
                else:
                    try:
                        datetime.fromisoformat(ts.strip())
                    except ValueError:
                        reason = f"非法时间戳: {ts!r}"
        if reason:
            total_bad += 1
            if len(samples) < _BAD_LINE_SAMPLE_LIMIT:
                samples.append(
                    {"line_no": line_no, "reason": reason,
                     "snippet": line[:_SNIPPET_LEN]}
                )
            continue
        entries.append(entry)
    return entries, samples, total_bad


class OsLogQueryAdapter:
    provider = "ios-simulator-oslog"

    def __init__(self, udid: str, *, executor=None) -> None:
        if not isinstance(udid, str) or not udid.strip():
            raise SchemaValidationError(
                "OsLogQueryAdapter 缺少 udid：拒绝 booted 隐式目标"
            )
        self.udid = udid.strip()
        self._executor = executor or SubprocessExecutor()

    @staticmethod
    def _parse_ndjson(
        stdout: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
        # 保留静态方法入口（既有测试/调用方在用），实现统一走模块函数
        return parse_ndjson(stdout)

    async def query(
        self,
        *,
        run_id: str,
        predicate: str,
        start_time: str,
        end_time: str,
    ) -> dict[str, Any]:
        # core 纯函数完成入参校验/argv 拼装，非法谓词/倒挂窗口在此响亮失败
        log_query = build_os_log_query(
            udid=self.udid,
            predicate=predicate,
            start_time=start_time,
            end_time=end_time,
        )
        result = await self._executor.run(log_query.argv)
        if result.returncode != 0:
            raise AdapterUnavailableError(
                f"log show 失败 rc={result.returncode} run_id={run_id} "
                f"stderr={result.stderr[:300]}"
            )
        entries, samples, total_bad = self._parse_ndjson(result.stdout)
        # 纵深防御：服务端窗外条目再过滤一遍（AC2）
        kept = filter_window(entries, log_query.start_time, log_query.end_time)
        return {
            "run_id": run_id,
            "entries": kept,
            "dropped_out_of_window": len(entries) - len(kept),
            "bad_lines": total_bad,
            "bad_line_samples": samples,
            "query": log_query.echo(),
        }
