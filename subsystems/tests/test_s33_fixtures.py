"""S3.3 合成夹具守护测试：钉死 tests/fixtures/{xcresult,oslog} 与解析器契约。

夹具是 A21 探针与后续手动探针校准的基线，任何对夹具形态的改动都会在这里显化。
全部夹具为自造合成内容，零语料仓原件。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from testpilot_core.enums import ArtifactType, CaseStatus
from testpilot_core.errors import SchemaValidationError
from testpilot_core.oslog_commands import parse_window
from testpilot_core.xcresult_parse import (
    parse_attachment_manifest,
    parse_test_nodes,
)
from testpilot_adapters.os_log import OsLogQueryAdapter
from testpilot_adapters.process_executor import CommandResult

_FIX = Path(__file__).parent / "fixtures"
_XC = _FIX / "xcresult"
_FAIL_REF = (
    "AlarmKitSpike/AlarmKitSpikeTests/ReminderSchedulerTests/"
    "testKillBackupFallsBackTo30s()"
)
_WINDOW_START = "2026-11-10T10:00:00"
_WINDOW_END = "2026-11-10T10:20:00"


def test_xcresult_fixture_tree_covers_three_statuses():
    cases = parse_test_nodes((_XC / "test-results.json").read_text())
    assert [c.status for c in cases] == [
        CaseStatus.PASSED, CaseStatus.SKIPPED, CaseStatus.FAILED
    ]
    failed = cases[2]
    assert failed.external_id == _FAIL_REF
    assert failed.duration_ms == 410
    assert failed.message.startswith("XCTAssertEqual failed")
    assert "90" in failed.message and "30" in failed.message


def test_xcresult_fixture_attachments_link_to_failed_case():
    cases = parse_test_nodes((_XC / "test-results.json").read_text())
    grouped = parse_attachment_manifest(
        (_XC / "attachments.json").read_text(),
        [case.external_id for case in cases],
    )
    attachments = grouped[_FAIL_REF]
    assert [a.file_name for a in attachments] == [
        "Screenshot-Failure-1.png", "FailureRecording.mp4"
    ]
    assert [a.artifact_type for a in attachments] == [
        ArtifactType.SCREENSHOT, ArtifactType.VIDEO
    ]
    assert all(a.is_failure_associated for a in attachments)


def test_xcresult_malformed_fixture_is_rejected():
    with pytest.raises(SchemaValidationError, match="未知 nodeType"):
        parse_test_nodes((_XC / "malformed-test-results.json").read_text())


class _FixtureExecutor:
    def __init__(self, stdout: str) -> None:
        self._stdout = stdout

    async def run(self, argv, *, cwd=None) -> CommandResult:
        return CommandResult(0, self._stdout, "", list(argv))


async def test_oslog_fixture_window_filter_and_bad_line_count():
    ndjson = (_FIX / "oslog" / "window.ndjson").read_text()
    adapter = OsLogQueryAdapter("UDID-X", executor=_FixtureExecutor(ndjson))
    result = await adapter.query(
        run_id="run-fixture",
        predicate='subsystem == "com.thames.alarmkitspike"',
        start_time=_WINDOW_START,
        end_time=_WINDOW_END,
    )
    assert len(result["entries"]) == 3
    assert result["dropped_out_of_window"] == 2
    assert result["bad_lines"] == 1
    assert result["bad_line_samples"][0]["reason"] == "JSON 解析失败"
    # 回显查询可复现（AC1）
    assert result["query"]["start_time"] == _WINDOW_START
    assert result["query"]["end_time"] == _WINDOW_END
    assert result["query"]["udid"] == "UDID-X"
    # 夹具窗口本身合法
    start, end = parse_window(_WINDOW_START, _WINDOW_END)
    assert start < end
