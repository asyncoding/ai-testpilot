"""S3.3 FR-EX-006 OsLogQueryAdapter 测试（hermetic，假执行器 + 自造 NDJSON）。

覆盖：窗内/窗外/坏行三类处置、query 回显可复现（AC1）、窗外不灌入（AC2）、
坏行计数与样片上限、非零退出/非法入参 fail-closed、LogQueryPort 协议符合性。
"""

from __future__ import annotations

import json

import pytest

from testpilot_core.errors import AdapterUnavailableError, SchemaValidationError
from testpilot_core.ports import LogQueryPort
from testpilot_adapters.os_log import OsLogQueryAdapter
from testpilot_adapters.process_executor import CommandResult

_UDID = "5411787C-8AFF-4EBF-9000-EB5EFD1457D5"
_PREDICATE = 'subsystem == "com.thames.alarmkitspike"'
_START = "2026-11-10T10:00:00"
_END = "2026-11-10T10:20:00"


def _line(ts: str, msg: str, subsystem: str = "com.thames.alarmkitspike") -> str:
    return json.dumps(
        {"timestamp": ts, "subsystem": subsystem, "message": msg,
         "process": "AlarmKitSpike"}
    )


class FakeExecutor:
    def __init__(self, stdout: str = "", *, rc: int = 0,
                 stderr: str = "") -> None:
        self._stdout = stdout
        self._rc = rc
        self._stderr = stderr
        self.calls: list[list[str]] = []

    async def run(self, argv: list[str], *, cwd: str | None = None) -> CommandResult:
        self.calls.append(argv)
        return CommandResult(self._rc, self._stdout, self._stderr, list(argv))


def _ndjson() -> str:
    return "\n".join([
        _line("2026-11-10T09:59:00", "out-before"),       # 窗外
        _line("2026-11-10T10:00:00", "left-bound"),        # 窗内（左闭）
        _line("2026-11-10T10:10:00", "inside"),            # 窗内
        _line("2026-11-10T10:20:00", "right-bound"),       # 窗内（右闭）
        _line("2026-11-10T10:21:00", "out-after"),         # 窗外
        "{broken json line",                                # 坏行
        "",                                                 # 空行忽略
    ])


async def test_query_keeps_window_drops_outside_counts_bad_lines():
    executor = FakeExecutor(_ndjson())
    adapter = OsLogQueryAdapter(_UDID, executor=executor)
    assert isinstance(adapter, LogQueryPort)

    result = await adapter.query(
        run_id="run-1", predicate=_PREDICATE,
        start_time=_START, end_time=_END,
    )
    assert [e["message"] for e in result["entries"]] == [
        "left-bound", "inside", "right-bound"
    ]
    assert result["dropped_out_of_window"] == 2
    assert result["bad_lines"] == 1
    assert result["bad_line_samples"][0]["reason"] == "JSON 解析失败"
    assert result["run_id"] == "run-1"


async def test_query_echo_is_reproducible_with_explicit_udid():
    executor = FakeExecutor(_ndjson())
    adapter = OsLogQueryAdapter(_UDID, executor=executor)
    result = await adapter.query(
        run_id="run-1", predicate=_PREDICATE,
        start_time=_START, end_time=_END,
    )
    query = result["query"]
    assert query["udid"] == _UDID
    assert query["predicate"] == _PREDICATE
    assert query["start_time"] == _START and query["end_time"] == _END
    # 实际跑的 argv 与回显同源，可直接照着重跑（AC1）
    assert query["argv"] == executor.calls[0]
    assert "booted" not in query["argv"]
    assert query["argv"][-2:] == ["--style", "json"]
    assert query["argv"][query["argv"].index("--predicate") + 1] == _PREDICATE


async def test_nonzero_rc_is_adapter_unavailable_with_stderr_evidence():
    executor = FakeExecutor("", rc=1, stderr="log: failed to archive")
    adapter = OsLogQueryAdapter(_UDID, executor=executor)
    with pytest.raises(AdapterUnavailableError, match="log show 失败"):
        await adapter.query(run_id="r", predicate=_PREDICATE,
                            start_time=_START, end_time=_END)


async def test_blank_udid_construction_rejected():
    with pytest.raises(SchemaValidationError, match="udid"):
        OsLogQueryAdapter("   ")


async def test_inverted_window_fails_before_any_process_call():
    executor = FakeExecutor(_ndjson())
    adapter = OsLogQueryAdapter(_UDID, executor=executor)
    with pytest.raises(SchemaValidationError, match="时间窗倒挂"):
        await adapter.query(
            run_id="r", predicate=_PREDICATE,
            start_time=_END, end_time=_START,
        )
    assert executor.calls == []


async def test_empty_predicate_fails_before_any_process_call():
    executor = FakeExecutor(_ndjson())
    adapter = OsLogQueryAdapter(_UDID, executor=executor)
    with pytest.raises(SchemaValidationError, match="predicate 为空"):
        await adapter.query(
            run_id="r", predicate="  ",
            start_time=_START, end_time=_END,
        )
    assert executor.calls == []


def _four_bad_lines() -> str:
    return "\n".join([
        "{broken",
        "123",                                   # 顶层非对象
        json.dumps({"no_timestamp": True}),     # 缺 timestamp
        json.dumps({"timestamp": "banana"}),    # 非法时间戳
    ])


async def test_bad_line_total_count_exceeds_sample_cap():
    executor = FakeExecutor(_four_bad_lines())
    adapter = OsLogQueryAdapter(_UDID, executor=executor)
    result = await adapter.query(
        run_id="r", predicate=_PREDICATE,
        start_time=_START, end_time=_END,
    )
    assert result["bad_lines"] == 4
    assert len(result["bad_line_samples"]) == 3  # 样片封顶，总数不封顶
    assert result["entries"] == []
    reasons = {s["reason"] for s in result["bad_line_samples"]}
    # 前三条坏行的原因留片；第四条（banana）超样片上限但计入总数
    assert reasons == {
        "JSON 解析失败", "行顶层不是对象", "缺少 timestamp 字符串"
    }
    assert [s["line_no"] for s in result["bad_line_samples"]] == [1, 2, 3]


async def test_blank_lines_are_ignored_not_counted():
    executor = FakeExecutor("\n\n   \n" + _line("2026-11-10T10:05:00", "x") + "\n\n")
    adapter = OsLogQueryAdapter(_UDID, executor=executor)
    result = await adapter.query(
        run_id="r", predicate=_PREDICATE,
        start_time=_START, end_time=_END,
    )
    assert result["bad_lines"] == 0 and len(result["entries"]) == 1


async def test_empty_stdout_returns_empty_entries_not_error():
    executor = FakeExecutor("")
    adapter = OsLogQueryAdapter(_UDID, executor=executor)
    result = await adapter.query(
        run_id="r", predicate=_PREDICATE,
        start_time=_START, end_time=_END,
    )
    assert result["entries"] == [] and result["bad_lines"] == 0
    assert result["dropped_out_of_window"] == 0
