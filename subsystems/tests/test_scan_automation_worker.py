"""scan-automation-worker 集成测试（docs/01 E3，hermetic）。

tmp 语料仓（Swift 文件）+ tmp SQLite store（活跃/探索性用例）+ 假容器：
钉死 map 落盘形状、raw_json.automation 投影（零 schema 变更）、重扫人审
保留/去重、悬空 manual 绑定 fail-closed 且不覆盖旧 map、CLI 退出码
0/2/3 三档。不碰真语料仓、不起 git 进程、不占端口。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import pytest

from testpilot_adapters.store import bootstrap_store
from testpilot_core.automation_mapping import AutomationMapError
from testpilot_core.models import TestCase
from testpilot_workers import scan_automation as sa
from testpilot_workers.scan_automation import make_fake_container, run_scan

_T0 = datetime(2026, 11, 13, 8, 0, tzinfo=timezone.utc)
_DOC_URN = "urn:testpilot:fs-cases:document:cases/x.md"

_AGENT_SWIFT = """
import XCTest

final class AgentRoutineAlarmTests: XCTestCase {

    // testpilot-case: TC-A1
    // testpilot-case: TC-A2
    func test_TCA1_ringSnapshot() { }

    // testpilot-case: TC-A1
    func test_TCA1_moreCoverage() { }
}
"""

_LEGACY_SWIFT = """
import XCTest

final class HolidayCalendarTests: XCTestCase {
    func testPlainDays() { }
}
"""

_UI_SWIFT = """
import XCTest

final class ProductFlowUITests: XCTestCase {
    // testpilot-case: TC-U1
    func test_TCU1_flow() { }
}
"""


def _case(case_id: str, *, exploratory: bool = False) -> TestCase:
    return TestCase(
        urn=f"urn:case:{case_id}", external_id=case_id, title=f"用例 {case_id}",
        module="RoutineAlarm", priority="P0", steps=["s"],
        exploratory=exploratory)


def _seed_store(tmp_path):
    store = bootstrap_store(tmp_path / "store")
    for cid in ("TC-A1", "TC-A2", "TC-U1", "TC-N2"):
        store.upsert_test_case(_DOC_URN, _case(cid), content_hash="h")
    store.upsert_test_case(
        _DOC_URN, _case("TC-EXP", exploratory=True), content_hash="h")
    return store


def _seed_repo(tmp_path):
    repo = tmp_path / "app-calendar-alarm"
    tests = repo / "AlarmKitSpike" / "Tests"
    uitests = repo / "AlarmKitSpike" / "UITests"
    tests.mkdir(parents=True)
    uitests.mkdir(parents=True)
    (tests / "AgentGenerated").mkdir()
    (tests / "AgentGenerated" / "RoutineAlarmAgentTests.swift").write_text(
        _AGENT_SWIFT, encoding="utf-8")
    (tests / "HolidayCalendarTests.swift").write_text(_LEGACY_SWIFT, encoding="utf-8")
    (uitests / "ProductFlowUITests.swift").write_text(_UI_SWIFT, encoding="utf-8")
    # 应被跳过的构建产物目录
    (repo / "AlarmKitSpike" / "build").mkdir()
    (repo / "AlarmKitSpike" / "build" / "IgnoreTests.swift").write_text(
        "// testpilot-case: TC-A1\nfinal class IgnoreTests {}\n", encoding="utf-8")
    return repo


def _container(store, repo, tmp_path):
    return make_fake_container(
        store, repo, head_commit="c" * 40,
        data_dir=tmp_path / "work")


# ── 通用化（2026-11-22）：profile ios 三键驱动工程根/target，CLI 可覆盖 ────────

_CUSTOM_SWIFT = """
final class FeatureTests: XCTestCase {
    // testpilot-case: TC-A1
    func test_feature() { }
}
"""

_CUSTOM_UI_SWIFT = """
final class FlowUITests: XCTestCase {
    // testpilot-case: TC-U1
    func test_flow() { }
}
"""

# 旧工程根段下的标记测试：自定义 engineering_root 后必须被 containment 挡住，
# 且其 TC-A1 标记不能因文件被跳过而报「未知 TC」硬错误（压根不扫描该文件）。
_LEGACY_ROOT_SWIFT = """
final class LegacyTests: XCTestCase {
    // testpilot-case: TC-A1
    func test_legacy() { }
}
"""


def _seed_custom_repo(tmp_path):
    repo = tmp_path / "demo-app"
    unit = repo / "MyApp" / "Tests" / "FeatureTests"
    ui = repo / "MyApp" / "UITests" / "Flow"
    unit.mkdir(parents=True)
    ui.mkdir(parents=True)
    (unit / "FeatureTests.swift").write_text(_CUSTOM_SWIFT, encoding="utf-8")
    (ui / "FlowUITests.swift").write_text(_CUSTOM_UI_SWIFT, encoding="utf-8")
    legacy = repo / "AlarmKitSpike" / "Tests" / "LegacyTests"
    legacy.mkdir(parents=True)
    (legacy / "LegacyTests.swift").write_text(_LEGACY_ROOT_SWIFT, encoding="utf-8")
    return repo


_CUSTOM_IOS = {
    "engineering_root": "MyApp",
    "unit_test_target": "MyAppTests",
    "ui_test_target": "MyAppUITests",
}


def _read_map(tmp_path):
    return json.loads(
        (tmp_path / "work" / "automation-map.json").read_text(encoding="utf-8"))


def test_scan_uses_profile_ios_config_for_root_and_targets(tmp_path):
    """profile 层：ios 三键驱动 containment 与 OID target；旧工程根被挡住。"""
    store = _seed_store(tmp_path)
    repo = _seed_custom_repo(tmp_path)
    container = make_fake_container(
        store, repo, head_commit="d" * 40, data_dir=tmp_path / "work",
        ios_config=_CUSTOM_IOS)
    summary = asyncio.run(run_scan(container, now=_T0))

    payload = _read_map(tmp_path)
    assert {b["only_testing_id"] for b in payload["bindings"]} == {
        "MyAppTests/FeatureTests/test_feature",
        "MyAppUITests/FlowUITests/test_flow",
    }
    unlinked = {u["only_testing_id"] for u in payload["unlinked_swift_tests"]}
    assert unlinked == set()  # 旧工程根文件未进扫描，不留幻影台账
    assert summary["scanned_files"] == 2


def test_run_scan_cli_kwargs_override_profile_ios_config(tmp_path):
    """优先级第一层：显式参数（CLI 接缝）压过 profile ios 配置。"""
    store = _seed_store(tmp_path)
    repo = _seed_custom_repo(tmp_path)
    container = make_fake_container(
        store, repo, head_commit="d" * 40, data_dir=tmp_path / "work",
        ios_config=_CUSTOM_IOS)
    asyncio.run(run_scan(
        container, now=_T0,
        engineering_root="MyApp",
        unit_target="CliTests", ui_target="CliUITests"))
    payload = _read_map(tmp_path)
    assert {b["only_testing_id"] for b in payload["bindings"]} == {
        "CliTests/FeatureTests/test_feature",
        "CliUITests/FlowUITests/test_flow",
    }


def test_scan_falls_back_to_core_constants_without_profile_ios(tmp_path):
    """优先级第三层：裸 config（无 project.ios，测试假容器/老 profile 同构）
    时回退 core legacy 常量，行为与通用化前逐字一致。"""
    store = _seed_store(tmp_path)
    repo = _seed_repo(tmp_path)
    asyncio.run(run_scan(_container(store, repo, tmp_path), now=_T0))
    payload = _read_map(tmp_path)
    assert any(
        b["only_testing_id"].startswith("AlarmKitSpikeTests/")
        for b in payload["bindings"])
    assert any(
        b["only_testing_id"].startswith("AlarmKitSpikeUITests/")
        for b in payload["bindings"])


def test_cli_flags_threaded_into_run_scan(tmp_path, monkeypatch):
    """argv 层：三个新 CLI flag 必须真正接到 run_scan（防只加参数不接线）。"""
    store = _seed_store(tmp_path)
    repo = _seed_custom_repo(tmp_path)
    container = make_fake_container(
        store, repo, head_commit="d" * 40, data_dir=tmp_path / "work",
        ios_config=_CUSTOM_IOS)
    monkeypatch.setattr(sa, "bootstrap", lambda name: container)
    monkeypatch.setattr("sys.argv", [
        "testpilot-scan-automation",
        "--engineering-root", "MyApp",
        "--unit-test-target", "CliTests",
        "--ui-test-target", "CliUITests"])
    with pytest.raises(SystemExit) as exc:
        sa.main()
    assert exc.value.code == 0  # 自定义库内无重复标记/探索性绑定
    payload = _read_map(tmp_path)
    assert {b["only_testing_id"] for b in payload["bindings"]} == {
        "CliTests/FeatureTests/test_feature",
        "CliUITests/FlowUITests/test_flow",
    }


def test_scan_writes_map_and_projects_raw_json(tmp_path):
    store = _seed_store(tmp_path)
    repo = _seed_repo(tmp_path)
    summary = asyncio.run(run_scan(_container(store, repo, tmp_path), now=_T0))

    map_path = tmp_path / "work" / "automation-map.json"
    payload = json.loads(map_path.read_text(encoding="utf-8"))
    assert payload["map_schema_version"] == "automation-map-v1"
    assert payload["head_commit"] == "c" * 40
    assert payload["branch"] == "demo/agent-generated-tests"
    bound = {(b["case_external_id"], b["origin"]) for b in payload["bindings"]}
    assert ("TC-A1", "marker") in bound and ("TC-A2", "marker") in bound
    assert ("TC-U1", "marker") in bound
    # 存量无标记测试进 unlinked
    unlinked = {u["only_testing_id"] for u in payload["unlinked_swift_tests"]}
    assert "AlarmKitSpikeTests/HolidayCalendarTests/testPlainDays" in unlinked
    assert "TC-N2" in payload["unmapped_active_cases"]
    # 探索性用例既不映射也不进未映射噪声
    assert "TC-EXP" not in payload["unmapped_active_cases"]
    assert summary["bindings"] == 4  # A1×2 + A2 + U1
    assert any("重复标记: TC-A1" in w for w in summary["scan_warnings"])

    # raw_json.automation 投影：映射用例正态、未映射用例负态，且不破坏原 raw_json
    mapped_case = store.get_test_case("urn:case:TC-A1")  # 模型可正常还原即 raw_json 未损坏
    raw = store.conn.execute(
        "SELECT raw_json FROM test_cases WHERE external_id='TC-A1'").fetchone()
    automation = json.loads(raw[0])["automation"]
    assert automation["mapped"] is True
    assert len(automation["bindings"]) == 2
    assert automation["head_commit"] == "c" * 40
    raw_n2 = store.conn.execute(
        "SELECT raw_json FROM test_cases WHERE external_id='TC-N2'").fetchone()
    assert json.loads(raw_n2[0])["automation"]["mapped"] is False
    assert mapped_case.external_id == "TC-A1"


def test_review_and_manual_bind_persist_across_rescan(tmp_path):
    store = _seed_store(tmp_path)
    repo = _seed_repo(tmp_path)
    container = _container(store, repo, tmp_path)

    asyncio.run(run_scan(
        container,
        review_pairs=[("TC-A1", "alice")],
        bind_specs=["TC-N2=AlarmKitSpikeTests/HolidayCalendarTests/testPlainDays"],
        now=_T0))
    map_path = tmp_path / "work" / "automation-map.json"
    first = json.loads(map_path.read_text(encoding="utf-8"))
    a1_rows = [b for b in first["bindings"] if b["case_external_id"] == "TC-A1"]
    assert all(b["reviewed_by"] == "alice" for b in a1_rows)
    manual = next(b for b in first["bindings"] if b["case_external_id"] == "TC-N2")
    # --by alice 同时登记到同次调用的 --bind 手工绑定
    assert manual["origin"] == "manual" and manual["reviewed_by"] == "alice"

    # 无参数重扫：人审 marker 与 manual 绑定全部保留，且 manual 不重复增生
    asyncio.run(run_scan(container, now=_T0))
    second = json.loads(map_path.read_text(encoding="utf-8"))
    assert len(second["bindings"]) == len(first["bindings"])
    a1_rows = [b for b in second["bindings"] if b["case_external_id"] == "TC-A1"]
    assert all(b["reviewed_by"] == "alice" for b in a1_rows)
    assert sum(1 for b in second["bindings"] if b["case_external_id"] == "TC-N2") == 1
    # 同 HEAD 同内容重扫 map_hash 稳定
    assert second["map_hash"] == first["map_hash"]


def test_dangling_manual_binding_fails_closed_and_keeps_old_map(tmp_path):
    store = _seed_store(tmp_path)
    repo = _seed_repo(tmp_path)
    container = _container(store, repo, tmp_path)
    map_path = tmp_path / "work" / "automation-map.json"
    asyncio.run(run_scan(
        container,
        bind_specs=["TC-N2=AlarmKitSpikeTests/HolidayCalendarTests/testPlainDays"],
        now=_T0))
    before = map_path.read_text(encoding="utf-8")

    # 人删掉被手工绑定的方法文件后重扫：硬错误，旧 map 不被覆盖
    (repo / "AlarmKitSpike" / "Tests" / "HolidayCalendarTests.swift").unlink()
    with pytest.raises(AutomationMapError, match="已不存在"):
        asyncio.run(run_scan(container, now=_T0))
    assert map_path.read_text(encoding="utf-8") == before


def test_missing_repo_dir_raises_filenotfound(tmp_path):
    store = _seed_store(tmp_path)
    container = make_fake_container(
        store, tmp_path / "nope", data_dir=tmp_path / "work")
    with pytest.raises(FileNotFoundError):
        asyncio.run(run_scan(container, now=_T0))


def test_cli_exit_codes_clean_warning_hard_error(tmp_path, monkeypatch):
    store = _seed_store(tmp_path)
    repo = _seed_repo(tmp_path)
    container = _container(store, repo, tmp_path)
    monkeypatch.setattr(sa, "bootstrap", lambda name: container)

    # 重复标记存在 → exit 3（map 照常产出）
    monkeypatch.setattr("sys.argv", ["testpilot-scan-automation"])
    with pytest.raises(SystemExit) as exc:
        sa.main()
    assert exc.value.code == 3

    # 硬错误：未知 TC 标记 → exit 2
    bad = repo / "AlarmKitSpike" / "Tests" / "GhostTests.swift"
    bad.write_text(
        "final class GhostTests: XCTestCase {\n"
        "  // testpilot-case: TC-GHOST\n  func test_ghost() { }\n}\n",
        encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        sa.main()
    assert exc.value.code == 2

    # 干净库：移除硬错误源与重复标记源，只剩单标记 TC-U1 → exit 0
    import shutil

    bad.unlink()
    shutil.rmtree(repo / "AlarmKitSpike" / "Tests" / "AgentGenerated")
    monkeypatch.setattr(
        "sys.argv",
        ["testpilot-scan-automation", "--review", "TC-U1", "--by", "alice"])
    with pytest.raises(SystemExit) as exc:
        sa.main()
    assert exc.value.code == 0
