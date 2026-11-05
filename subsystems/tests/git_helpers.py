"""确定性演示 git 仓构造（FR-CON-001 契约与录制 fixture 共用）。

固定 author/committer 身份与日期 → 内容寻址 sha 跨机器可复现，
录制 JSON（tests/fixtures/git）才能与真适配器实时输出逐字段对账。
仓内容刻意覆盖：rename + 符号增/改、空 message commit、非 swift 文件。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

GIT_BIN = shutil.which("git")

ROOT_A_SWIFT = (
    "final class C {\n"
    "  var interval = 90\n"
    "  func keep() { print(interval) }\n"
    "}\n"
)
HEAD_B_SWIFT = (
    "final class C {\n"
    "  var interval = 30\n"
    "  func keep() { print(\"changed\", interval) }\n"
    "  func fresh() {}\n"
    "}\n"
)

_COMMIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t.t",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t.t",
}


def git(cwd: Path, *args: str, when: str = "2026-01-01T00:00:00+08:00") -> str:
    env = {
        **os.environ, **_COMMIT_ENV,
        "GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when,
    }
    proc = subprocess.run(
        [GIT_BIN, *args], cwd=cwd, env=env,
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, f"git {' '.join(args)}: {proc.stderr}"
    return proc.stdout.strip()


def build_demo_repo(base_dir: Path) -> tuple[Path, str, str]:
    """在 base_dir/repo 建两提交演示仓，返回 (仓路径, root_sha, head_sha)。"""
    cwd = base_dir / "repo"
    cwd.mkdir(parents=True, exist_ok=True)
    git(cwd, "init", "-q", "-b", "main")
    (cwd / "Sources").mkdir()
    (cwd / "Sources" / "A.swift").write_text(ROOT_A_SWIFT)
    (cwd / "README.md").write_text("# v1\n")
    git(cwd, "add", "-A")
    git(cwd, "commit", "-qm", "init")
    root = git(cwd, "rev-parse", "HEAD")

    (cwd / "Sources" / "A.swift").write_text(HEAD_B_SWIFT)
    git(cwd, "mv", "Sources/A.swift", "Sources/B.swift")
    (cwd / "README.md").write_text("# v2\n")
    # 带正文的 md（空标题章节不产块）：覆盖 live-verify 的 markdown 重切路径
    (cwd / "Docs").mkdir(exist_ok=True)
    (cwd / "Docs" / "guide.md").write_text("# 指南\n\n这是告警指南正文。\n")
    git(cwd, "add", "-A")
    # 第二提交刻意空 message（FR-CON-001 AC2：不依赖 commit message）
    git(cwd, "commit", "-qm", "", "--allow-empty-message", when="2026-01-02T00:00:00+08:00")
    head = git(cwd, "rev-parse", "HEAD")
    return cwd, root, head
