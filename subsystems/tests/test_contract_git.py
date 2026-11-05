"""GitCodeRepository ↔ RecordedGitRepository 双适配器契约（FR-CFG-002 AC2）。

同一组调用同时打在真实适配器（tmp 确定性演示仓）与录制适配器
（tests/fixtures/git，由 scripts/record_git_fixtures.py 从同一演示仓录制）上，
逐字段对账：录制物必须与真实 git 输出一致，而不是手写的"看起来对"的 JSON。
无 git 二进制时整文件 skip（hostless 单测在 test_git_repo.py）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from git_helpers import GIT_BIN, build_demo_repo
from testpilot_adapters.git_repo import GitCodeRepository
from testpilot_adapters.recorded_git import RecordedGitRepository
from testpilot_core.errors import AdapterUnavailableError, PathMissingAtCommitError
from testpilot_core.models import EventCheckpoint, RepoRef

pytestmark = pytest.mark.skipif(GIT_BIN is None, reason="环境无 git 二进制")

FIX_DIR = Path(__file__).parent / "fixtures" / "git"


@pytest.fixture
def adapters(tmp_path: Path):
    cwd, root, head = build_demo_repo(tmp_path)
    repo = RepoRef(project="demo", repo_path=str(cwd), default_branch="main")
    real = GitCodeRepository(repo, git_path=GIT_BIN)
    recorded = RecordedGitRepository(repo, FIX_DIR)
    return real, recorded, repo, root, head


async def test_pinned_shas_match_live_repo(adapters):
    _real, recorded, _repo, root, head = adapters
    # 录制 sha 必须与实时重建仓一致：内容/身份/日期任一漂移，这里先红
    assert await recorded.head_sha() == head
    meta = recorded._load_meta()
    assert meta["commits"] == {"root": root, "head": head}
    assert await recorded.head_sha() == head  # 两适配器 head 相同在下面逐调用对账


async def test_head_sha_identical(adapters):
    real, recorded, repo, _root, _head = adapters
    assert await real.head_sha(repo) == await recorded.head_sha(repo)


async def test_read_blobs_identical(adapters):
    real, recorded, repo, root, head = adapters
    cases = [
        ("README.md", root),
        ("README.md", head),
        ("Sources/A.swift", root),
        ("Sources/B.swift", head),
    ]
    for path, sha in cases:
        assert await real.read_file_at_commit(repo, path, sha) == \
            await recorded.read_file_at_commit(repo, path, sha), path
    # commit 下缺路径：两适配器同为 PathMissing（不是不可达）
    for adapter in (real, recorded):
        with pytest.raises(PathMissingAtCommitError):
            await adapter.read_file_at_commit(repo, "Sources/B.swift", root)


async def test_change_set_identical(adapters):
    real, recorded, repo, root, head = adapters
    cs_real = await real.get_change_set(repo, root, head)
    cs_rec = await recorded.get_change_set(repo, root, head)
    assert cs_rec.repo == repo  # RepoRef 在回放时绑定，不烤进录制物
    assert cs_real.model_dump(exclude={"repo"}, mode="json") == \
        cs_rec.model_dump(exclude={"repo"}, mode="json")
    assert cs_real.base_commit == root and cs_real.head_commit == head
    assert cs_rec.changed_files  # 空 message 提交照样对出文件清单
    assert cs_rec.symbol_changes  # 与符号增删改


async def test_symbol_snapshots_identical(adapters):
    real, recorded, repo, root, head = adapters
    cases = [
        ("Sources/B.swift#C.fresh()", head),   # 新增符号，body_changed=True
        ("Sources/B.swift#C", head),           # 类型块（成员改动级联）
        ("Sources/A.swift#C.ghost()", root),   # 不存在 → exists=False
    ]
    for symbol_id, sha in cases:
        snap_real = await real.get_symbol_at_commit(repo, symbol_id, sha)
        snap_rec = await recorded.get_symbol_at_commit(repo, symbol_id, sha)
        assert snap_real.model_dump(mode="json") == \
            snap_rec.model_dump(mode="json"), symbol_id


async def test_list_changes_identical(adapters):
    real, recorded, _repo, root, _head = adapters
    cp = EventCheckpoint(source_system="git", last_cursor=root)
    ev_real = [e.model_dump(mode="json") for e in await real.list_changes(cp)]
    ev_rec = [e.model_dump(mode="json") for e in await recorded.list_changes(cp)]
    assert ev_real == ev_rec
    assert ev_rec and ev_rec[0]["event_type"] == "CommitPushed"
    assert "message" not in ev_rec[0]["payload"]  # 不依赖 commit message


async def test_no_cursor_is_empty_for_both(adapters):
    real, recorded, _repo, _root, _head = adapters
    cp = EventCheckpoint(source_system="git")
    assert await real.list_changes(cp) == []
    assert await recorded.list_changes(cp) == []


async def test_unrecorded_key_is_adapter_unavailable(adapters):
    _real, recorded, repo, _root, head = adapters
    with pytest.raises(AdapterUnavailableError, match="缺少 blob 应答"):
        await recorded.read_file_at_commit(repo, "Sources/unrecorded.swift", head)
    with pytest.raises(AdapterUnavailableError):
        await recorded.get_symbol_at_commit(repo, "X#nope()", head)


async def test_unknown_revision_is_unavailable(adapters):
    _real, recorded, repo, _root, _head = adapters
    with pytest.raises(AdapterUnavailableError, match="无法解析版本引用"):
        await recorded.read_file_at_commit(repo, "README.md", "v9.9-tag")


async def test_missing_fixtures_dir_is_unavailable(tmp_path: Path):
    repo = RepoRef(project="demo", repo_path="/tmp/demo-repo")
    rec = RecordedGitRepository(repo, tmp_path / "no-such-fixtures")
    with pytest.raises(AdapterUnavailableError, match="夹具缺失"):
        await rec.head_sha()


async def test_path_map_serves_current_file_relative_to_fixtures(adapters):
    _real, recorded, repo, root, _head = adapters
    # 逻辑路径映射到夹具内真实文件（相对路径相对 fixtures_dir）：
    # 离线 live_verify 靠它对文档根里的 md 重新切块验 hash
    recorded.path_map = {"docs/head.md": "blobs/head_README.md"}
    text = await recorded.read_file_at_commit(repo, "docs/head.md", root)
    assert text == (FIX_DIR / "blobs" / "head_README.md").read_text("utf-8")
