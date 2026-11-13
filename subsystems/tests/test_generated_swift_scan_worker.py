"""scan-generated-swift-worker 封装层测试（H5，tmp_path 全 hermetic）。

重点验证作用域：红线只在 AgentGenerated 目录生效，语料仓人工存量代码
（同名 sleep 写法）不被扫描、不产生假红；构建目录整体跳过。
"""

from __future__ import annotations

import json

import pytest

from testpilot_workers.scan_generated_swift import (
    iter_agent_generated_swift,
    main,
    scan_agent_generated_dir,
)

_CLEAN = """\
final class RelayTests: XCTestCase {
    func testRelay() throws {
        app.activate()
        XCTAssertTrue(app.buttons["ok"].waitForExistence(timeout: 10))
    }
}
"""

_BAD_SLEEP = """\
final class SmokeTests: XCTestCase {
    func testOldStyle() throws {
        sleep(2)
    }
}
"""

_BAD_LAUNCH = """\
final class RelayTests: XCTestCase {
    func testRelay() throws {
        app.activate()
        app.launch()
    }
}
"""


def _write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_scan_only_picks_agent_generated_swift(tmp_path):
    _write(tmp_path, "AlarmKitSpike/UITests/AgentGenerated/Relay.swift", _CLEAN)
    _write(tmp_path, "AlarmKitSpike/Tests/AgentGenerated/Smoke.swift", _CLEAN)
    # 圈外人工存量代码：即便含 sleep 也不在选盘集合内
    _write(tmp_path, "AlarmKitSpike/UITests/ManualTests/Legacy.swift", _BAD_SLEEP)
    # 构建产物目录整体跳过，哪怕里面有 AgentGenerated 段
    _write(tmp_path, "DerivedData/Build/AgentGenerated/Stale.swift", _BAD_SLEEP)
    _write(tmp_path, "AlarmKitSpike/UITests/Notes.txt", "sleep(1)\n")

    files = iter_agent_generated_swift(tmp_path)
    assert set(files) == {
        "AlarmKitSpike/UITests/AgentGenerated/Relay.swift",
        "AlarmKitSpike/Tests/AgentGenerated/Smoke.swift",
    }


def test_clean_repo_report_zero_violations(tmp_path):
    _write(tmp_path, "AlarmKitSpike/UITests/AgentGenerated/Relay.swift", _CLEAN)
    report = scan_agent_generated_dir(tmp_path)
    assert report["scanned_files"] == 1
    assert report["violation_count"] == 0
    assert report["rule_breakdown"] == {}
    assert report["violations"] == []


def test_red_proof_full_directory_red_then_green(tmp_path):
    bad_path = "AlarmKitSpike/Tests/AgentGenerated/Smoke.swift"
    _write(tmp_path, bad_path, _BAD_SLEEP)
    red = scan_agent_generated_dir(tmp_path)
    assert red["violation_count"] == 1
    assert red["rule_breakdown"] == {"GS-001": 1}
    row = red["violations"][0]
    assert row["rule_id"] == "GS-001"
    assert row["path"] == bad_path
    assert row["line"] == 3
    assert "sleep(2)" in row["snippet"]

    # 回退为轮询写法后整仓转绿
    _write(tmp_path, bad_path, _CLEAN)
    assert scan_agent_generated_dir(tmp_path)["violation_count"] == 0


def test_report_aggregates_multiple_rules(tmp_path):
    _write(tmp_path, "AlarmKitSpike/Tests/AgentGenerated/Smoke.swift", _BAD_SLEEP)
    _write(tmp_path, "AlarmKitSpike/UITests/AgentGenerated/Relay.swift", _BAD_LAUNCH)
    report = scan_agent_generated_dir(tmp_path)
    assert report["scanned_files"] == 2
    assert report["violation_count"] == 2
    assert report["rule_breakdown"] == {"GS-001": 1, "GS-002": 1}


def test_missing_repo_root_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        scan_agent_generated_dir(tmp_path / "does-not-exist")


def test_cli_exit_3_on_violation_and_emits_json(tmp_path, monkeypatch, capsys):
    _write(tmp_path, "AlarmKitSpike/Tests/AgentGenerated/Smoke.swift", _BAD_SLEEP)
    monkeypatch.setattr("sys.argv", ["worker", "--repo", str(tmp_path)])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["violation_count"] == 1


def test_cli_exit_0_when_clean(tmp_path, monkeypatch):
    _write(tmp_path, "AlarmKitSpike/UITests/AgentGenerated/Relay.swift", _CLEAN)
    monkeypatch.setattr("sys.argv", ["worker", "--repo", str(tmp_path)])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 0


def test_cli_exit_2_on_missing_repo(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        "sys.argv", ["worker", "--repo", str(tmp_path / "ghost")])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
    assert "扫描被拒" in capsys.readouterr().err
