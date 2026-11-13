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
    # 真机校准（2026-11-10）：工具只吃 '%Y-%m-%d %H:%M:%S' 空格格式（T 分隔
    # rc=64），且必须 ndjson（json style 是美化数组，无法逐行解析）
    assert argv == [
        "xcrun", "simctl", "spawn", _UDID,
        "log", "show",
        "--start", "2026-11-10 10:00:00",
        "--end", "2026-11-10 10:20:00",
        "--predicate", _PREDICATE,
        "--style", "ndjson",
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


def test_filter_window_mixed_aware_entries_and_naive_window_uses_wall_clock():
    # 真机形态：条目带 +0800 偏移，naive 窗口语义为本地挂钟（log show 默认
    # 本地时区渲染）。混比不得 TypeError，按挂钟剥偏移后正常判窗。
    start, end = parse_window(_START, _END)  # naive
    entries = [
        _entry("2026-11-10 09:59:59.123+0800", "before-aware"),
        _entry("2026-11-10 10:00:00.000+0800", "left-bound-aware"),
        _entry("2026-11-10 10:10:00.999+0800", "inside-aware"),
        _entry("2026-11-10 10:20:00.000+0800", "right-bound-aware"),
        _entry("2026-11-10 10:20:00.001+0800", "after-aware"),
    ]
    kept = filter_window(entries, start, end)
    assert [e["message"] for e in kept] == [
        "left-bound-aware", "inside-aware", "right-bound-aware"
    ]


def test_aware_window_with_naive_entries_also_comparable():
    # 反向混搭：aware 窗口 + naive 条目（老夹具/老库历史数据），同样不得崩
    start, end = parse_window(
        "2026-11-10T10:00:00+08:00", "2026-11-10T10:20:00+08:00"
    )
    kept = filter_window(
        [_entry("2026-11-10T10:10:00", "naive-inside")], start, end
    )
    assert len(kept) == 1


def test_predicate_is_stripped_but_preserved_verbatim_in_argv():
    query = build_os_log_query(
        udid=f"  {_UDID}  ", predicate=f"  {_PREDICATE}  ",
        start_time=_START, end_time=_END,
    )
    assert query.udid == _UDID and query.predicate == _PREDICATE
    assert query.argv[11] == _PREDICATE
    assert all(not piece.startswith("  ") for piece in query.argv)

# ── 真机两步法（切片一，2026-11-13）──────────────────────────────────────────

_PHYSICAL_UDID = "00008140-001A39590162801C"
_ARCHIVE = "/tmp/tp/run-x.logarchive"


def test_device_collect_argv_exact_shape_without_sudo():
    from testpilot_core.oslog_commands import build_device_log_collect_argv

    start, _ = parse_window(_START, _END)
    argv = build_device_log_collect_argv(
        udid=_PHYSICAL_UDID, start=start, output_path=_ARCHIVE
    )
    assert argv == [
        "/usr/bin/log", "collect",
        "--device-udid", _PHYSICAL_UDID,
        "--start", "2026-11-10 10:00:00",
        "--output", _ARCHIVE,
    ]
    # collect 没有 --end：窗口收口只在 show 侧，防止误以为双端裁剪
    assert "--end" not in argv and "--last" not in argv


def test_device_collect_argv_sudo_non_interactive_prefix():
    from testpilot_core.oslog_commands import build_device_log_collect_argv

    start, _ = parse_window(_START, _END)
    argv = build_device_log_collect_argv(
        udid=_PHYSICAL_UDID, start=start, output_path=_ARCHIVE, use_sudo=True
    )
    # sudo -n：无 TTY 凭据未缓存时立即失败，绝不挂死等密码；只包住 collect
    assert argv[:3] == ["sudo", "-n", "/usr/bin/log"]
    assert argv[3] == "collect"


def test_archive_show_argv_exact_shape():
    from testpilot_core.oslog_commands import build_archive_log_show_argv

    start, end = parse_window(_START, _END)
    argv = build_archive_log_show_argv(
        archive_path=_ARCHIVE, predicate=_PREDICATE, start=start, end=end
    )
    assert argv == [
        "/usr/bin/log", "show", _ARCHIVE,
        "--start", "2026-11-10 10:00:00",
        "--end", "2026-11-10 10:20:00",
        "--predicate", _PREDICATE,
        "--style", "ndjson",
    ]


def test_device_query_echo_carries_both_argv_and_mode():
    from testpilot_core.oslog_commands import build_device_os_log_query

    query = build_device_os_log_query(
        udid=_PHYSICAL_UDID, predicate=_PREDICATE,
        start_time=_START, end_time=_END, archive_path=_ARCHIVE,
    )
    echo = query.echo()
    assert echo["collect_argv"][1] == "collect"
    assert echo["show_argv"][2] == _ARCHIVE
    # 兼容模拟器 query 回显的 argv 键：归因/探针代码可统一读 argv
    assert echo["argv"] == echo["show_argv"]
    assert echo["collect_mode"] == "auto" and echo["use_sudo"] is False
    assert echo["udid"] == _PHYSICAL_UDID


def test_manual_mode_still_emits_collect_command_for_evidence():
    from testpilot_core.oslog_commands import (
        COLLECT_MODE_MANUAL,
        build_device_os_log_query,
    )

    query = build_device_os_log_query(
        udid=_PHYSICAL_UDID, predicate=_PREDICATE,
        start_time=_START, end_time=_END, archive_path=_ARCHIVE,
        collect_mode=COLLECT_MODE_MANUAL, use_sudo=True,
    )
    # manual 不自动采集，但等效命令必须留在回显里供人工照跑/复盘
    assert query.collect_argv[:2] == ["sudo", "-n"]
    assert query.collect_mode == "manual"


def test_device_query_rejects_bad_mode_blank_udid_and_inverted_window():
    from testpilot_core.oslog_commands import build_device_os_log_query

    with pytest.raises(SchemaValidationError, match="collect_mode"):
        build_device_os_log_query(
            udid=_PHYSICAL_UDID, predicate=_PREDICATE,
            start_time=_START, end_time=_END, archive_path=_ARCHIVE,
            collect_mode="telepathic",
        )
    with pytest.raises(SchemaValidationError, match="udid 为空"):
        build_device_os_log_query(
            udid="  ", predicate=_PREDICATE,
            start_time=_START, end_time=_END, archive_path=_ARCHIVE,
        )
    with pytest.raises(SchemaValidationError, match="archive_path 为空"):
        build_device_os_log_query(
            udid=_PHYSICAL_UDID, predicate=_PREDICATE,
            start_time=_START, end_time=_END, archive_path=" ",
        )
    with pytest.raises(SchemaValidationError, match="倒挂"):
        build_device_os_log_query(
            udid=_PHYSICAL_UDID, predicate=_PREDICATE,
            start_time=_END, end_time=_START, archive_path=_ARCHIVE,
        )
