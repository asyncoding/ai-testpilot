"""GS 静态红线假仓红证（转正自主套件，血缘：批次 S5 hermetic 13 条）。

与 test_generated_swift_lint.py（core 规则层，内联字符串夹具）和
test_generated_swift_scan_worker.py（worker 封装层，tmp_path 散文件）并存不合并：
本文件保的是**整仓形态**叙事——选盘层圈定 AgentGenerated、SPM 圈外整文件跳过、
真实 CLI 子进程 0/3/2 三退出码，静态基线夹具落在 tests/fixtures/gs-fake-repo/。

纪律：全部变异只发生在 tmp_path 假仓副本上；gs-fake-repo/ 静态基线夹具只读，
模块级 autouse 守卫在每条用例后校验夹具字节零变化（零 TEMP-RED 残留）。
覆盖：
- 基线合规稿 exit 0 / 零违规（setUp 的 launch 不计入 GS-002/003）；
- GS-001 裸 sleep/Thread.sleep、GS-002 activate+launch、GS-003 双 launch
  植入必红、复原必绿；
- 注释/字符串遮罩不误伤；
- 作用域守卫：SPM 圈外文件（含裸 sleep）选盘层整文件跳过，直送 core 被拒；
- 诚实记录规则缺口：Task.sleep 当前不被 GS-001 匹配（无承诺，未来规则收紧时
  本用例应被有意改写）；
- 真实 CLI 子进程三退出码 0/3/2。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from testpilot_core.generated_swift_lint import (
    GeneratedSwiftLintError,
    scan_agent_generated_files,
    scan_generated_swift,
)
from testpilot_workers.scan_generated_swift import (
    iter_agent_generated_swift,
    scan_agent_generated_dir,
)

FIXTURE_REPO = Path(__file__).resolve().parent / "fixtures" / "gs-fake-repo"
AG_REL = "AlarmKitSpike/UITests/AgentGenerated/QuickAlarmAgentUITests.swift"
SPM_REL = "Packages/TestPilotKit/Sources/TestPilotKit/TPWebSocketServer.swift"


def _snapshot_fixture() -> dict[str, str]:
    return {
        str(p.relative_to(FIXTURE_REPO)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in FIXTURE_REPO.rglob("*.swift")
    }


FIXTURE_HASHES = _snapshot_fixture()


@pytest.fixture(autouse=True)
def _fixture_byte_guard():
    yield
    assert _snapshot_fixture() == FIXTURE_HASHES, "静态夹具被污染，变异漏到了 tmp_path 之外"


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    dst = tmp_path / "fake-repo"
    shutil.copytree(FIXTURE_REPO, dst)
    return dst


def _ag_file(repo: Path) -> Path:
    return repo / AG_REL


def _scan_ag(repo: Path):
    text = _ag_file(repo).read_text(encoding="utf-8")
    return scan_generated_swift(AG_REL, text)


def _rule_ids(violations) -> set[str]:
    return {v.rule_id for v in violations}


# ── 基线绿路 ────────────────────────────────────────────────────────────────


def test_baseline_fixture_is_clean(fake_repo: Path):
    files = iter_agent_generated_swift(fake_repo)
    # 选盘层只收 AgentGenerated 圈内文件，SPM 包整体跳过。
    assert set(files) == {AG_REL}
    assert _rule_ids(_scan_ag(fake_repo)) == set()
    report = scan_agent_generated_dir(fake_repo)
    assert report["scanned_files"] == 1
    assert report["violation_count"] == 0


def test_setup_launch_not_counted(fake_repo: Path):
    # 基线稿异步 setUp() 内恰有一次 app.launch()，test 方法体零 launch：
    # GS-002/003 只切 test 方法体，基线零违规即证明 setUp 冷启动不被计违规。
    text = _ag_file(fake_repo).read_text(encoding="utf-8")
    assert text.count(".launch(") == 1
    assert _scan_ag(fake_repo) == []


# ── GS-001 固定 sleep ────────────────────────────────────────────────────────


def test_gs001_bare_sleep_red_then_revert_green(fake_repo: Path):
    path = _ag_file(fake_repo)
    original = path.read_text(encoding="utf-8")
    mutated = original.replace(
        "        app.activate()", "        sleep(2)\n        app.activate()", 1
    )
    path.write_text(mutated, encoding="utf-8")
    violations = _scan_ag(fake_repo)
    assert _rule_ids(violations) == {"GS-001"}
    assert "sleep/usleep" in violations[0].message
    # 复原即绿。
    path.write_text(original, encoding="utf-8")
    assert _scan_ag(fake_repo) == []


def test_gs001_thread_sleep_red(fake_repo: Path):
    path = _ag_file(fake_repo)
    original = path.read_text(encoding="utf-8")
    path.write_text(
        original.replace(
            "        app.activate()",
            "        Thread.sleep(forTimeInterval: 1)\n        app.activate()",
            1,
        ),
        encoding="utf-8",
    )
    assert _rule_ids(_scan_ag(fake_repo)) == {"GS-001"}


def test_gs001_comment_and_string_are_masked(fake_repo: Path):
    path = _ag_file(fake_repo)
    original = path.read_text(encoding="utf-8")
    path.write_text(
        original.replace(
            "        app.activate()",
            '        // sleep(9) 注释里的 app.launch() 不算违规\n'
            '        let doc = "sleep(1) app.launch()"\n'
            "        app.activate()",
            1,
        ),
        encoding="utf-8",
    )
    assert _scan_ag(fake_repo) == []


def test_gs001_task_sleep_is_known_gap_not_flagged(fake_repo: Path):
    # 诚实记录规则缺口：GS-001 正则当前不匹配 Task.sleep（SDK 内部竞速在用）。
    # 若未来收紧规则，本用例应被有意改写而非静默变红。
    path = _ag_file(fake_repo)
    original = path.read_text(encoding="utf-8")
    path.write_text(
        original.replace(
            "        app.activate()",
            "        try? await Task.sleep(nanoseconds: 1)\n        app.activate()",
            1,
        ),
        encoding="utf-8",
    )
    assert _scan_ag(fake_repo) == []


# ── GS-002/003 冷启动毁现场 ──────────────────────────────────────────────────


def test_gs002_activate_and_launch_coexist_red(fake_repo: Path):
    path = _ag_file(fake_repo)
    original = path.read_text(encoding="utf-8")
    # 方法体内 launch 恰好 1 次且与 activate 并存：只触 GS-002，不触 GS-003。
    path.write_text(
        original.replace("        app.activate()", "        app.launch()\n        app.activate()", 1),
        encoding="utf-8",
    )
    violations = _scan_ag(fake_repo)
    assert _rule_ids(violations) == {"GS-002"}
    assert "activate 与 launch" in violations[0].message


def test_gs003_double_launch_red(fake_repo: Path):
    path = _ag_file(fake_repo)
    original = path.read_text(encoding="utf-8")
    # 方法体内两次 launch、零 activate：只触 GS-003。
    path.write_text(
        original.replace(
            "        app.activate()", "        app.launch()\n        app.launch()", 1
        ),
        encoding="utf-8",
    )
    violations = _scan_ag(fake_repo)
    assert _rule_ids(violations) == {"GS-003"}
    assert "2 次 launch" in violations[0].message


# ── 作用域守卫与 SPM 圈外证明 ────────────────────────────────────────────────


def test_scope_guard_rejects_outside_paths():
    spm_text = (FIXTURE_REPO / SPM_REL).read_text(encoding="utf-8")
    with pytest.raises(GeneratedSwiftLintError, match="圈外路径拒绝扫描"):
        scan_generated_swift(SPM_REL, spm_text)
    with pytest.raises(GeneratedSwiftLintError, match="圈外路径拒绝扫描"):
        scan_generated_swift("UITests/Manual/OldTests.swift", "sleep(1)")
    with pytest.raises(GeneratedSwiftLintError, match="只扫描 .swift"):
        scan_generated_swift("UITests/AgentGenerated/notes.txt", "x")


def test_spm_outside_circle_never_scanned_even_with_bare_sleep(fake_repo: Path):
    # SPM 夹具内含裸 sleep(1) 与 Task.sleep；选盘层不收 → 整仓报告零违规。
    spm_text = (fake_repo / SPM_REL).read_text(encoding="utf-8")
    assert "sleep(1)" in spm_text
    report = scan_agent_generated_dir(fake_repo)
    assert report["scanned_files"] == 1
    assert report["violation_count"] == 0
    # 批量 API 同样 fail-closed：混入围外文件整批拒绝而非悄悄跳过。
    with pytest.raises(GeneratedSwiftLintError):
        scan_agent_generated_files({SPM_REL: spm_text})


# ── 真实 CLI 子进程三退出码 ──────────────────────────────────────────────────


def _run_cli(repo: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "testpilot_workers.scan_generated_swift",
         "--repo", str(repo)],
        capture_output=True, text=True,
    )


def test_cli_clean_repo_exit0(fake_repo: Path):
    proc = _run_cli(fake_repo)
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout)
    assert report["violation_count"] == 0
    assert report["scanned_files"] == 1


def test_cli_violation_exit3(fake_repo: Path):
    path = _ag_file(fake_repo)
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "        app.activate()", "        sleep(2)\n        app.activate()", 1
        ),
        encoding="utf-8",
    )
    proc = _run_cli(fake_repo)
    assert proc.returncode == 3
    report = json.loads(proc.stdout)
    assert report["rule_breakdown"] == {"GS-001": 1}


def test_cli_missing_repo_exit2(tmp_path: Path):
    proc = _run_cli(tmp_path / "does-not-exist")
    assert proc.returncode == 2
    assert "扫描被拒" in proc.stderr
