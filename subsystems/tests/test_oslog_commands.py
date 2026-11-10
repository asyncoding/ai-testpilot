"""S3.3 FR-EX-006 os_log 命令拼装与时间窗过滤纯函数测试（core 零 IO）。

覆盖：显式 udid（拒绝 booted 隐式目标）、ISO 窗校验/倒挂拒绝、谓词空值与
超长守卫、argv 精确对账、query 回显可复现、闭区间窗内/窗外过滤、坏时间戳
fail-closed。零真实 simctl 调用。
"""

from __future__ import annotations

import pytest

from testpilot_core.errors import SchemaValidationError
from testpilot_core.oslog_commands import (
    MAX_PREDICATE_LEN,
    build_log_show_argv,
    build_os_log_query,
    filter_window,
    in_window,
    parse_window,
)

_UDID = "5411787C-8AFF-4EBF-9000-EB5EFD1457D5"
_PREDICATE = 'subsystem == "com.thames.alarmkitspike"'
_START = "2026-11-10T10:00:00"
_END = "2026-11-10T10:20:00"


def test_argv_exact_reconciliation_udid_explicit_not_booted():
    start, end = parse_window(_START, _END)
    argv = build_log_show_argv(udid=_UDID, predicate=_PREDICATE,
                               start=start, end=end)
    assert argv == [
        "xcrun", "simctl", "spawn", _UDID,
        "log", "show",
        "--start", "2026-11-10T10:00:00",
        "--end", "2026-11-10T10:20:00",
        "--predicate", _PREDICATE,
        "--style", "json",
    ]
    assert "booted" not in argv


def test_build_os_log_query_echo_is_reproducible():
    query = build_os_log_query(
        udid=_UDID, predicate=_PREDICATE,
        start_time=_START, end_time=_END,
    )
    echo = query.echo()
    assert echo == {
        "udid": _UDID,
        "predicate": _PREDICATE,
        "start_time": _START,
        "end_time": _END,
        "argv": query.argv,
    }
    # 回显 argv 可直接照跑，且与 echo 字段同源
    assert echo["argv"][3] == echo["udid"]
    assert echo["argv"][11] == echo["predicate"]


def test_equal_start_end_is_valid_zero_width_window():
    start, end = parse_window(_START, _START)
    assert start == end
    assert in_window(start, start, end)


@pytest.mark.parametrize(
    "start_text,end_text,match",
    [
        ("", _END, "start_time 为空"),
        ("  ", _END, "start_time 为空"),
        (_START, "", "end_time 为空"),
        ("2026-11-10T25:00:00", _END, "非法 start_time"),
        ("not-a-date", _END, "非法 start_time"),
        (_START, "2026-11-10T09:00:00", "时间窗倒挂"),
    ],
)
def test_parse_window_guardrails(start_text, end_text, match):
    with pytest.raises(SchemaValidationError, match=match):
        parse_window(start_text, end_text)


def test_empty_or_blank_udid_rejected():
    start, end = parse_window(_START, _END)
    for bad in ("", "   "):
        with pytest.raises(SchemaValidationError, match="udid 为空"):
            build_log_show_argv(udid=bad, predicate=_PREDICATE,
                                start=start, end=end)


def test_empty_predicate_rejected_directional_query_only():
    start, end = parse_window(_START, _END)
    with pytest.raises(SchemaValidationError, match="predicate 为空"):
        build_log_show_argv(udid=_UDID, predicate="  ", start=start, end=end)


def test_oversized_predicate_rejected():
    start, end = parse_window(_START, _END)
    with pytest.raises(SchemaValidationError, match="predicate 超长"):
        build_log_show_argv(udid=_UDID, predicate="x" * (MAX_PREDICATE_LEN + 1),
                            start=start, end=end)


def test_build_log_show_argv_rejects_datetime_inversion():
    start, end = parse_window(_START, _END)
    with pytest.raises(SchemaValidationError, match="时间窗倒挂"):
        build_log_show_argv(udid=_UDID, predicate=_PREDICATE,
                            start=end, end=start)


def _entry(ts: str, msg: str = "x") -> dict:
    return {"timestamp": ts, "message": msg}


def test_filter_window_keeps_inclusive_bounds_and_drops_outside():
    start, end = parse_window(_START, _END)
    entries = [
        _entry("2026-11-10T09:59:59", "before"),      # 窗外
        _entry("2026-11-10T10:00:00", "left-bound"),  # 左闭
        _entry("2026-11-10T10:10:00", "inside"),
        _entry("2026-11-10T10:20:00", "right-bound"), # 右闭
        _entry("2026-11-10T10:20:01", "after"),       # 窗外
    ]
    kept = filter_window(entries, start, end)
    assert [e["message"] for e in kept] == [
        "left-bound", "inside", "right-bound"
    ]
    # 输入不被修改
    assert len(entries) == 5


def test_filter_window_bad_timestamp_fails_closed_not_silently_dropped():
    start, end = parse_window(_START, _END)
    with pytest.raises(SchemaValidationError, match="非法日志时间戳"):
        filter_window([_entry("banana")], start, end)
    with pytest.raises(SchemaValidationError, match="缺少 timestamp"):
        filter_window([{"message": "no-ts"}], start, end)
    with pytest.raises(SchemaValidationError, match="应为对象"):
        filter_window(["not-a-dict"], start, end)  # type: ignore[list-item]


def test_filter_window_empty_entries_returns_empty():
    start, end = parse_window(_START, _END)
    assert filter_window([], start, end) == []


def test_predicate_is_stripped_but_preserved_verbatim_in_argv():
    query = build_os_log_query(
        udid=f"  {_UDID}  ", predicate=f"  {_PREDICATE}  ",
        start_time=_START, end_time=_END,
    )
    assert query.udid == _UDID and query.predicate == _PREDICATE
    assert query.argv[11] == _PREDICATE
    assert all(not piece.startswith("  ") for piece in query.argv)
