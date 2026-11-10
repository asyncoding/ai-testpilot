"""os_log 查询命令拼装与时间窗过滤纯函数（docs/08 FR-EX-006，S3.3）。

对接 ``xcrun simctl spawn <udid> log show``（docs/03 §3.3–3.4）。本模块零 IO、
零三方：只做 argv 拼装、时间窗校验/过滤、可复现查询回显结构；进程调用与
JSON 行解析在适配器层（adapters/os_log.py）。

设计要点：

- docs/03 示例用 ``spawn booted``，实现改为显式 ``spawn <udid>``：租约本来就
  绑定具体模拟器，udid 进 argv 后查询条件可完整复现（FR-EX-006 AC1），不依赖
  调用时刻"谁是 booted"这个隐式状态；
- 时间窗显式 ``--start/--end`` 而非 ``--last 20m``：相对时间无法复现，事后
  归因必须能回放同一窗口（AC1）；窗外条目不进知识库（AC2）；
- **2026-11-10 真机探针校准（Xcode 27 模拟器 iOS 26.4）**：``log show`` 的
  ``--start/--end`` 只接受 ``%Y-%m-%d %H:%M:%S`` 空格格式，ISO8601 的 ``T``
  分隔直接 rc=64；故进程 argv 用 strftime 工具格式，而 echo 回显仍保留调用方
  ISO8601 原文（可复现描述与工具语法分层，不互相牺牲）；
- **style 必须是 ``ndjson`` 不是 ``json``**：真机取证 ``--style json`` 输出
  美化打印的 JSON 数组（多行一个对象），逐行解析会全量坏行；``ndjson`` 才是
  一行一条、自带 ``timestamp`` 字符串字段的形态；
- 谓词长度上限防手滑/防拼出巨型 NSPredicate，谓词原文原样进 query 回显。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Sequence

from pydantic import BaseModel, Field

from .errors import SchemaValidationError

# 谓词长度上限：NSPredicate 没有官方硬限，1024 足够覆盖 subsystem/process
# 组合查询，超长基本意味着拼接错误或注入尝试
MAX_PREDICATE_LEN = 1024
# os_log --style ndjson 每行一条记录，时间戳字段名固定
TIMESTAMP_KEY = "timestamp"
# log show --start/--end 的唯一受支持语法（真机 rc=64 取证，T 分隔/ISO 均被拒）
_TOOL_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
# 进程 argv 固定 ndjson：json style 是美化数组，无法逐行解析
_TOOL_STYLE = "ndjson"


class OsLogQuery(BaseModel):
    """一次 os_log 查询的可复现描述（AC1：查询条件本身是证据）。"""

    udid: str
    predicate: str
    start_time: datetime
    end_time: datetime
    argv: list[str] = Field(default_factory=list)

    def echo(self) -> dict[str, Any]:
        """适配器 query() 返回体的 query 节：逐字段可照着重跑。"""
        return {
            "udid": self.udid,
            "predicate": self.predicate,
            "start_time": self.start_time.isoformat(),
            "end_time": self.end_time.isoformat(),
            "argv": list(self.argv),
        }


def parse_window(start_text: str, end_text: str) -> tuple[datetime, datetime]:
    """ISO8601 时间窗字符串 → (start, end)，并校验 start ≤ end。"""
    if not isinstance(start_text, str) or not start_text.strip():
        raise SchemaValidationError("start_time 为空")
    if not isinstance(end_text, str) or not end_text.strip():
        raise SchemaValidationError("end_time 为空")
    try:
        start = datetime.fromisoformat(start_text.strip())
    except ValueError as exc:
        raise SchemaValidationError(f"非法 start_time: {start_text!r}") from exc
    try:
        end = datetime.fromisoformat(end_text.strip())
    except ValueError as exc:
        raise SchemaValidationError(f"非法 end_time: {end_text!r}") from exc
    if start > end:
        raise SchemaValidationError(
            f"时间窗倒挂: start={start.isoformat()} 晚于 end={end.isoformat()}"
        )
    return start, end


def _validate_predicate(predicate: str) -> str:
    if not isinstance(predicate, str) or not predicate.strip():
        raise SchemaValidationError("predicate 为空：本端口只做带谓词的定向查询")
    predicate = predicate.strip()
    if len(predicate) > MAX_PREDICATE_LEN:
        raise SchemaValidationError(
            f"predicate 超长: {len(predicate)} > {MAX_PREDICATE_LEN}"
        )
    return predicate


def build_log_show_argv(
    *,
    udid: str,
    predicate: str,
    start: datetime,
    end: datetime,
) -> list[str]:
    """拼装 ``simctl spawn <udid> log show`` argv（数组形态，无 shell 解析面）。"""
    if not isinstance(udid, str) or not udid.strip():
        raise SchemaValidationError("udid 为空：拒绝 booted 隐式目标，查询必须可复现")
    if not isinstance(start, datetime) or not isinstance(end, datetime):
        raise SchemaValidationError("start/end 必须是 datetime（先走 parse_window）")
    if start > end:
        raise SchemaValidationError("时间窗倒挂: start 晚于 end")
    predicate = _validate_predicate(predicate)
    return [
        "xcrun", "simctl", "spawn", udid.strip(),
        "log", "show",
        # 工具只吃 '%Y-%m-%d %H:%M:%S'（真机 rc=64 取证）；ISO 原文保留在 echo
        "--start", start.strftime(_TOOL_TIME_FORMAT),
        "--end", end.strftime(_TOOL_TIME_FORMAT),
        "--predicate", predicate,
        "--style", _TOOL_STYLE,
    ]


def build_os_log_query(
    *,
    udid: str,
    predicate: str,
    start_time: str,
    end_time: str,
) -> OsLogQuery:
    """端口入口的一站式纯函数：校验入参 → 时间窗 → argv + 回显模型。"""
    start, end = parse_window(start_time, end_time)
    argv = build_log_show_argv(
        udid=udid, predicate=predicate, start=start, end=end
    )
    return OsLogQuery(
        udid=udid.strip(),
        predicate=predicate.strip(),
        start_time=start,
        end_time=end,
        argv=argv,
    )


def _entry_timestamp(entry: dict[str, Any]) -> datetime:
    raw = entry.get(TIMESTAMP_KEY)
    if not isinstance(raw, str) or not raw.strip():
        raise SchemaValidationError(
            f"日志条目缺少 {TIMESTAMP_KEY} 字段: {str(entry)[:200]}"
        )
    try:
        return datetime.fromisoformat(raw.strip())
    except ValueError as exc:
        raise SchemaValidationError(f"非法日志时间戳: {raw!r}") from exc


def in_window(ts: datetime, start: datetime, end: datetime) -> bool:
    """闭区间 [start, end]：与 ``log show --start/--end`` 语义一致。

    真机条目时间戳带时区偏移（``2026-11-10 21:40:31.5+0800``），调用方窗口
    可能是 naive（语义=本地挂钟，log show 默认也按本地时区渲染）。aware/naive
    直接比较会 TypeError，这里统一按本地挂钟口径剥偏移再比；两侧同为 aware 或
    同为 naive 时原样比较，既有行为不变。
    """
    if start.tzinfo is None and ts.tzinfo is not None:
        ts = ts.replace(tzinfo=None)
    elif start.tzinfo is not None and ts.tzinfo is None:
        start = start.replace(tzinfo=None)
        end = end.replace(tzinfo=None)
    return start <= ts <= end


def filter_window(
    entries: Sequence[dict[str, Any]],
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    """丢弃窗外条目（AC2：不整量灌入知识库）；坏时间戳 fail-closed 不静默丢。"""
    if start > end:
        raise SchemaValidationError("时间窗倒挂: start 晚于 end")
    kept: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise SchemaValidationError(
                f"日志条目应为对象，实际: {type(entry).__name__}"
            )
        if in_window(_entry_timestamp(entry), start, end):
            kept.append(entry)
    return kept
