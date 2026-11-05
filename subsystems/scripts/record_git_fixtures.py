#!/usr/bin/env python3
"""生成 RecordedGitRepository 的离线录制夹具（docs/08 FR-CFG-002 AC2）。

在临时目录用固定 author/date 构造确定性演示仓（tests/git_helpers.build_demo_repo），
通过**真实 GitCodeRepository** 产出全部应答并落盘为 JSON：

    tests/fixtures/git/
    ├── repo.json                 # head_sha/角色 sha/元信息
    ├── blobs/                    # 指定 commit 的文件内容 + manifest
    ├── change_sets/              # base..head ChangeSet（不含 RepoRef，回放时绑定）
    ├── snapshots/                # get_symbol_at_commit 应答 + manifest
    └── commits/                  # list_changes 事件回放 + manifest

sha 由内容/作者/日期决定，跨机器可复现；契约测试（tests/test_contract_git.py）
会实时重建同一演示仓逐字段对账。仓内容或 git 输出口径变化后必须重新生成本夹具：

    cd subsystems && .venv/bin/python scripts/record_git_fixtures.py
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from git_helpers import build_demo_repo  # noqa: E402

from testpilot_adapters.git_repo import GitCodeRepository  # noqa: E402
from testpilot_core.models import EventCheckpoint, RepoRef  # noqa: E402

FIX_DIR = ROOT / "tests" / "fixtures" / "git"


def _dump_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="tp-git-record-"))
    try:
        cwd, root, head = build_demo_repo(tmp)
        repo = RepoRef(project="demo", repo_path=str(cwd), default_branch="main")
        git = GitCodeRepository(repo)

        if FIX_DIR.exists():
            shutil.rmtree(FIX_DIR)

        # ── repo.json ──
        _dump_json(FIX_DIR / "repo.json", {
            "project": "demo",
            "default_branch": "main",
            "head_sha": head,
            "commits": {"root": root, "head": head},
            "generated_by": "scripts/record_git_fixtures.py",
            "note": "固定 author/date 内容寻址，sha 跨机器可复现；改演示仓后重新生成",
        })

        # ── blobs ──
        blobs = {
            f"{root}:README.md": ("root_README.md", None),
            f"{head}:README.md": ("head_README.md", None),
            f"{root}:Sources/A.swift": ("root_A.swift", None),
            f"{head}:Sources/B.swift": ("head_B.swift", None),
            f"{root}:Sources/B.swift": (None, "recorded-missing-at-commit"),
        }
        manifest = {}
        (FIX_DIR / "blobs").mkdir(parents=True, exist_ok=True)
        for key, (fname, missing_reason) in blobs.items():
            if missing_reason:
                manifest[key] = {"missing": True, "reason": missing_reason}
                continue
            sha, path = key.split(":", 1)
            text = await git.read_file_at_commit(repo, path, sha)
            (FIX_DIR / "blobs" / fname).write_text(text, encoding="utf-8")
            manifest[key] = {"file": f"blobs/{fname}"}
        _dump_json(FIX_DIR / "blobs" / "manifest.json", manifest)

        # ── change set ──
        cs = await git.get_change_set(repo, root, head)
        cs_data = cs.model_dump(exclude={"repo"}, mode="json")
        _dump_json(
            FIX_DIR / "change_sets" / f"{root}..{head}.json", cs_data
        )
        _dump_json(FIX_DIR / "change_sets" / "manifest.json", {
            f"{root}..{head}": f"change_sets/{root}..{head}.json",
        })

        # ── snapshots ──
        snapshot_keys = [
            f"{head}:Sources/B.swift#C.fresh()",
            f"{head}:Sources/B.swift#C",
            f"{root}:Sources/A.swift#C.ghost()",
        ]
        snap_manifest = {}
        for i, key in enumerate(snapshot_keys):
            sha, symbol_id = key.split(":", 1)
            snap = await git.get_symbol_at_commit(repo, symbol_id, sha)
            fname = f"snapshots/snap_{i}.json"
            _dump_json(FIX_DIR / fname, snap.model_dump(mode="json"))
            snap_manifest[key] = fname
        _dump_json(FIX_DIR / "snapshots" / "manifest.json", snap_manifest)

        # ── commits 回放 ──
        events = await git.list_changes(
            EventCheckpoint(source_system="git", last_cursor=root)
        )
        _dump_json(
            FIX_DIR / "commits" / f"{root}.json",
            [e.model_dump(mode="json") for e in events],
        )
        _dump_json(FIX_DIR / "commits" / "manifest.json", {
            root: f"commits/{root}.json",
        })

        print(f"录制夹具已写入 {FIX_DIR}")
        print(f"root={root}")
        print(f"head={head}（空 message 提交）")
        print(f"changed_files={cs_data['changed_files']}")
        print(f"symbol_changes={len(cs_data['symbol_changes'])} 条")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
