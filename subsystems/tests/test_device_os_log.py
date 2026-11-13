"""真机 DeviceOsLogQueryAdapter 两步采集测试（hermetic，假执行器 + tmp_path，零真机）。"""

from __future__ import annotations

import json

import pytest

from testpilot_adapters.device_os_log import DeviceOsLogQueryAdapter
from testpilot_adapters.process_executor import CommandResult
from testpilot_core.errors import AdapterUnavailableError, SchemaValidationError
from testpilot_core.ports import LogQueryPort

_UDID = "00008140-001A39590162801C"
_PREDICATE = 'subsystem == "com.thames.alarmkitspike"'
_START = "2026-11-13T10:00:00"
_END = "2026-11-13T10:20:00"
_RUN_ID = "run-abcd1234efgh"


def _line(ts: str, msg: str, subsystem: str = "com.thames.alarmkitspike") -> str:
    return json.dumps(
        {"timestamp": ts, "subsystem": subsystem, "message": msg,
         "process": "AlarmKitSpike"}
    )


class ScriptedExecutor:
    """按入队顺序返回结果，并可在每次调用后跑钩子（模拟 collect 落盘归档）。"""

    def __init__(self, results: list[CommandResult], *, hooks=None):
        self._results = list(results)
        self._hooks = list(hooks or [])
        self.calls: list[list[str]] = []

    async def run(self, argv: list[str], *, cwd=None) -> CommandResult:
        self.calls.append(list(argv))
        result = self._results.pop(0)
        hook = self._hooks.pop(0) if self._hooks else None
        if hook is not None:
            hook(argv)
        return result


def _adapter(tmp_path, **kwargs):
    return DeviceOsLogQueryAdapter(_UDID, tmp_path / "logarchive-work", **kwargs)


def _ndjson() -> str:
    return "\n".join([
        _line("2026-11-13T09:59:00", "out-before"),
        _line("2026-11-13T10:00:00", "left-bound"),
        _line("2026-11-13T10:10:00", "inside"),
        _line("2026-11-13T10:20:00", "right-bound"),
        _line("2026-11-13T10:21:00", "out-after"),
        json.dumps({"count": 4, "finished": 1}),  # 信封尾行
    ])


async def test_implements_log_query_port(tmp_path):
    adapter = _adapter(tmp_path)
    assert isinstance(adapter, LogQueryPort)
    assert adapter.provider == "ios-device-oslog"


def test_construction_rejects_blank_udid_and_bad_mode(tmp_path):
    with pytest.raises(SchemaValidationError, match="udid"):
        DeviceOsLogQueryAdapter(" ", tmp_path)
    with pytest.raises(SchemaValidationError, match="collect_mode"):
        DeviceOsLogQueryAdapter(_UDID, tmp_path, collect_mode="telepathic")


async def test_auto_mode_runs_collect_then_show_with_window_filter(tmp_path):
    adapter = _adapter(tmp_path)
    archive = tmp_path / "logarchive-work" / f"{_RUN_ID}.logarchive"

    def _materialize(argv):
        assert argv[1] == "collect"
        archive.mkdir(parents=True)

    executor = ScriptedExecutor(
        [
            CommandResult(0, "", "", []),
            CommandResult(0, _ndjson(), "", []),
        ],
        hooks=[_materialize, None],
    )
    adapter._executor = executor

    result = await adapter.query(
        run_id=_RUN_ID, predicate=_PREDICATE,
        start_time=_START, end_time=_END,
    )
    assert [argv[1] for argv in executor.calls] == ["collect", "show"]
    collect_argv, show_argv = executor.calls
    # collect 走设备 UDID、无 --end；sudo 缺省关闭
    assert collect_argv == [
        "/usr/bin/log", "collect", "--device-udid", _UDID,
        "--start", "2026-11-13 10:00:00", "--output", str(archive),
    ]
    # show 打本机归档、带完整窗口与谓词、固定 ndjson
    assert show_argv[:3] == ["/usr/bin/log", "show", str(archive)]
    assert show_argv[-2:] == ["--style", "ndjson"]
    assert [e["message"] for e in result["entries"]] == [
        "left-bound", "inside", "right-bound"
    ]
    assert result["dropped_out_of_window"] == 2
    assert result["bad_lines"] == 0
    assert result["archive_path"] == str(archive)
    # 回显同时含两条 argv，照单可人工重放
    echo = result["query"]
    assert echo["collect_argv"] == collect_argv and echo["show_argv"] == show_argv
    assert echo["collect_mode"] == "auto"


async def test_auto_mode_pre_cleans_stale_archive(tmp_path):
    adapter = _adapter(tmp_path)
    archive = tmp_path / "logarchive-work" / f"{_RUN_ID}.logarchive"
    archive.mkdir(parents=True)
    (archive / "stale.txt").write_text("old", encoding="utf-8")

    def _materialize(argv):
        archive.mkdir(parents=True, exist_ok=True)

    executor = ScriptedExecutor(
        [CommandResult(0, "", "", []), CommandResult(0, _ndjson(), "", [])],
        hooks=[_materialize, None],
    )
    adapter._executor = executor
    result = await adapter.query(
        run_id=_RUN_ID, predicate=_PREDICATE,
        start_time=_START, end_time=_END,
    )
    # 旧文件已被先删后采清掉，只剩本次归档目录
    assert not (archive / "stale.txt").exists()
    assert result["archive_path"] == str(archive)


async def test_collect_failure_with_sudo_hint_and_no_show_call(tmp_path):
    adapter = _adapter(tmp_path, use_sudo=True)
    executor = ScriptedExecutor([
        CommandResult(1, "", "sudo: a password is required", []),
    ])
    adapter._executor = executor
    with pytest.raises(AdapterUnavailableError, match="sudo -n 失败"):
        await adapter.query(
            run_id=_RUN_ID, predicate=_PREDICATE,
            start_time=_START, end_time=_END,
        )
    assert len(executor.calls) == 1  # collect 失败不继续 show
    assert executor.calls[0][:2] == ["sudo", "-n"]


async def test_collect_rc0_without_archive_is_adapter_error(tmp_path):
    adapter = _adapter(tmp_path)
    adapter._executor = ScriptedExecutor([CommandResult(0, "", "", [])])
    with pytest.raises(AdapterUnavailableError, match="归档缺失"):
        await adapter.query(
            run_id=_RUN_ID, predicate=_PREDICATE,
            start_time=_START, end_time=_END,
        )


async def test_show_failure_surfaces_stderr(tmp_path):
    adapter = _adapter(tmp_path)
    archive = tmp_path / "logarchive-work" / f"{_RUN_ID}.logarchive"

    def _materialize(argv):
        archive.mkdir(parents=True)

    adapter._executor = ScriptedExecutor(
        [
            CommandResult(0, "", "", []),
            CommandResult(1, "", "log: invalid archive", []),
        ],
        hooks=[_materialize, None],
    )
    with pytest.raises(AdapterUnavailableError, match="log show 归档失败"):
        await adapter.query(
            run_id=_RUN_ID, predicate=_PREDICATE,
            start_time=_START, end_time=_END,
        )


async def test_manual_mode_requires_prestaged_archive_with_human_command(tmp_path):
    adapter = _adapter(tmp_path, collect_mode="manual")
    executor = ScriptedExecutor([])
    adapter._executor = executor
    with pytest.raises(AdapterUnavailableError, match="manual 采集模式") as exc:
        await adapter.query(
            run_id=_RUN_ID, predicate=_PREDICATE,
            start_time=_START, end_time=_END,
        )
    # 报错里带可照跑的 collect 命令与目标归档路径
    assert "/usr/bin/log collect --device-udid" in str(exc.value)
    assert executor.calls == []  # 平台绝不代跑 collect


async def test_manual_mode_consumes_existing_archive_via_show_only(tmp_path):
    adapter = _adapter(tmp_path, collect_mode="manual", use_sudo=True)
    archive = tmp_path / "logarchive-work" / f"{_RUN_ID}.logarchive"
    archive.mkdir(parents=True)
    executor = ScriptedExecutor([CommandResult(0, _ndjson(), "", [])])
    adapter._executor = executor

    result = await adapter.query(
        run_id=_RUN_ID, predicate=_PREDICATE,
        start_time=_START, end_time=_END,
    )
    assert len(executor.calls) == 1 and executor.calls[0][1] == "show"
    # 回显保留人工本应执行的 sudo collect 命令，证据链不断
    assert result["query"]["collect_argv"][:2] == ["sudo", "-n"]
    assert len(result["entries"]) == 3


async def test_bad_run_id_rejected_before_any_process(tmp_path):
    adapter = _adapter(tmp_path)
    adapter._executor = ScriptedExecutor([])
    with pytest.raises(SchemaValidationError, match="非法 run_id"):
        await adapter.query(
            run_id="../escape", predicate=_PREDICATE,
            start_time=_START, end_time=_END,
        )
    assert adapter._executor.calls == []
