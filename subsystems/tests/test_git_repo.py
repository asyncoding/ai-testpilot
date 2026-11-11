"""GitCodeRepository 测试（docs/08 FR-CON-001 / NFR-006）。

两层：
1. FakeGitRunner：hostless，秒级，专测参数/解析/分类/缓存/超时，不依赖真 git；
2. tmp_path 真 git 仓（无 git 二进制自动 skip）：抓 fake 永远抓不到的东西——
   rename 相似度输出、-z 字节序、空 message commit、损坏仓真实退出码。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest

from git_helpers import GIT_BIN, build_demo_repo
from testpilot_adapters.git_repo import (
    GitCodeRepository,
    GitResult,
    SubprocessGitRunner,
    parse_log_name_status,
    parse_name_status,
)
from testpilot_core.errors import AdapterUnavailableError, PathMissingAtCommitError
from testpilot_core.enums import ChangeType
from testpilot_core.models import EventCheckpoint, FileRename, RepoRef

REPO = RepoRef(project="demo", repo_path="/tmp/demo-repo", default_branch="main")
# 必须是合法 hex：适配器对完整 sha 零进程直采缓存，h/b 等非 hex 字符会触发 rev-parse
BASE_SHA = "1" * 40
HEAD_SHA = "2" * 40


# ── 纯解析单测 ──────────────────────────────────────────────────────────────

def test_parse_name_status_plain_and_rename():
    out = b"R100\0old/A.swift\0new/B.swift\0M\0README.md\0D\0x.swift\0"
    assert parse_name_status(out) == [
        ("R100", "old/A.swift", "new/B.swift"),
        ("M", "README.md", None),
        ("D", "x.swift", None),
    ]


def test_parse_name_status_unknown_code_is_defensive():
    out = b"X99\0weird\0"
    entries = parse_name_status(out)
    assert entries[0][1] == "weird"  # 畸形也不丢文件


def test_parse_log_name_status_blocks_and_dates():
    out = (
        b"abc1234\x012026-01-01T00:00:00+08:00\0\nD\0a.swift\0A\0b.swift\0"
        b"def5678\x012026-01-02T00:00:00+08:00\0M\0c.md\0"
    )
    commits = parse_log_name_status(out)
    assert commits[0][0] == "abc1234"
    assert commits[0][1] == "2026-01-01T00:00:00+08:00"
    assert commits[0][2] == [("D", "a.swift", None), ("A", "b.swift", None)]
    assert commits[1][2] == [("M", "c.md", None)]


# ── FakeGitRunner ───────────────────────────────────────────────────────────

class FakeGitRunner:
    """按 git 子命令分发脚本；记录全部调用；可注入慢调用做超时故障。"""

    def __init__(self):
        self.handlers = {}
        self.calls: list[list[str]] = []
        self.slow_after: float | None = None

    def on(self, subcommand: str, handler):
        self.handlers[subcommand] = handler
        return self

    async def run(self, args, *, cwd, timeout_s, extra_env):
        self.calls.append(list(args))
        if self.slow_after is not None:
            await asyncio.sleep(self.slow_after)
        handler = self.handlers[args[0]]
        result = handler(args)
        return await result if asyncio.iscoroutine(result) else result


def ok(stdout: bytes = b"", stderr: str = "") -> GitResult:
    return GitResult(0, stdout, stderr)


def fail(rc: int, stderr: str) -> GitResult:
    return GitResult(rc, b"", stderr)


def make_adapter(runner, *, timeout_s=5.0):
    adapter = GitCodeRepository(REPO, runner=runner, timeout_s=timeout_s)
    return adapter


@pytest.fixture
def ready_adapter():
    """root 已发现、resolve 固定返回 HEAD_SHA 的标准夹具。"""
    runner = FakeGitRunner()
    root_calls = {"n": 0}

    def root(args):
        root_calls["n"] += 1
        return ok(b"/tmp/demo-repo\n")

    def rev_parse(args):
        ref = args[-1].rstrip("^{commit}")
        if ref == "HEAD":
            return ok(HEAD_SHA.encode() + b"\n")
        return ok(BASE_SHA.encode() + b"\n")

    runner.on("rev-parse", rev_parse)
    adapter = make_adapter(runner)
    adapter._root = Path("/tmp/demo-repo")
    return adapter, runner


async def test_ensure_root_failure_is_adapter_unavailable():
    runner = FakeGitRunner().on("rev-parse", lambda a: fail(128, "fatal: not a git repository"))
    adapter = make_adapter(runner)
    with pytest.raises(AdapterUnavailableError, match="不是可用 git 仓库"):
        await adapter.head_sha()


async def test_missing_cwd_distinguished_from_missing_git_binary(tmp_path):
    # NFR-006：仓库目录不存在（外部仓未挂载/路径配错）不能误报成"找不到 git"
    runner = SubprocessGitRunner(git_path="git")
    with pytest.raises(AdapterUnavailableError, match="仓库目录不存在"):
        await runner.run(
            ["rev-parse"], cwd=tmp_path / "no-such-repo",
            timeout_s=5.0, extra_env={},
        )


async def test_timeout_classified_as_unavailable():
    runner = FakeGitRunner()
    runner.slow_after = 0.2
    runner.on("rev-parse", lambda a: ok(b"/tmp/demo-repo\n"))
    adapter = make_adapter(runner, timeout_s=0.02)
    with pytest.raises(AdapterUnavailableError, match="超时"):
        await adapter.head_sha()


async def test_rename_paired_not_all_delete_add(ready_adapter):
    adapter, runner = ready_adapter
    same_swift = "final class C {\n  func foo(a: Int) { print(1) }\n}\n"

    def show(args):
        spec = args[1]
        if spec.startswith(HEAD_SHA) and spec.endswith("B.swift"):
            return ok(same_swift.encode())
        if spec.startswith(BASE_SHA) and spec.endswith("A.swift"):
            return ok(same_swift.encode())
        return fail(128, f"fatal: path does not exist in '{spec[:12]}'")

    def diff(args):
        return ok(b"R100\0A.swift\0B.swift\0M\0README.md\0")

    runner.on("show", show)
    runner.on("diff", diff)

    cs = await adapter.get_change_set(REPO, BASE_SHA, HEAD_SHA)
    assert cs.changed_files == ["A.swift", "B.swift", "README.md"]
    # rename 两侧同内容：零符号增删
    assert cs.symbol_changes == []


async def test_change_set_carries_file_renames_with_similarity(ready_adapter):
    adapter, runner = ready_adapter
    same_swift = "final class C {\n  func keep() {}\n}\n"

    def show(args):
        if args[1].endswith(("A.swift", "B.swift")):
            return ok(same_swift.encode())
        return fail(128, "missing")

    # R100 带相似度分数；裸 R 无分数 → similarity=None（不臆造）
    runner.on("show", show)
    runner.on("diff", lambda a: ok(
        b"R100\0old/A.swift\0new/B.swift\0R\0old/x.md\0new/y.md\0"))

    cs = await adapter.get_change_set(REPO, BASE_SHA, HEAD_SHA)

    assert cs.file_renames == [
        FileRename(old_path="old/A.swift", new_path="new/B.swift", similarity=100),
        FileRename(old_path="old/x.md", new_path="new/y.md", similarity=None),
    ]


async def test_added_swift_file_gives_added_symbols(ready_adapter):
    adapter, runner = ready_adapter
    new_swift = "final class C {\n  func bar() {}\n}\n"

    def show(args):
        spec = args[1]
        if spec.startswith(HEAD_SHA):
            return ok(new_swift.encode())
        return fail(128, f"fatal: path 'n.swift' does not exist in '{BASE_SHA[:12]}'")

    runner.on("show", show)
    runner.on("diff", lambda a: ok(b"A\0n.swift\0"))

    cs = await adapter.get_change_set(REPO, BASE_SHA, HEAD_SHA)
    kinds = {c.symbol_id: c.change_type for c in cs.symbol_changes}
    assert kinds.get("C") == ChangeType.ADDED
    assert kinds.get("C.bar()") == ChangeType.ADDED


async def test_missing_path_vs_unreachable_distinguished(ready_adapter):
    adapter, runner = ready_adapter

    runner.on("show", lambda a: fail(128, "fatal: path 'x.swift' does not exist in 'hhh'"))
    with pytest.raises(PathMissingAtCommitError):
        await adapter.read_file_at_commit(REPO, "x.swift", HEAD_SHA)

    runner.on("show", lambda a: fail(128, "fatal: invalid object name 'zzz'"))
    # 直接 show 已解析 sha 的场景：非"不存在"句式 → 不可达
    with pytest.raises(AdapterUnavailableError):
        await adapter.read_file_at_commit(REPO, "x.swift", HEAD_SHA)


async def test_empty_tree_base_skips_base_resolution(ready_adapter):
    adapter, runner = ready_adapter
    runner.on("diff", lambda a: ok(b""))
    await adapter.get_change_set(REPO, "", HEAD_SHA)
    diff_call = next(c for c in runner.calls if c[0] == "diff")
    assert "4b825dc642cb6eb9a060e54bf8d69288fbee4904" in diff_call


async def test_sha_resolution_cached_but_head_refresh_each_time(ready_adapter):
    adapter, runner = ready_adapter
    await adapter._resolve(HEAD_SHA)
    await adapter._resolve(HEAD_SHA)
    full_sha_calls = [c for c in runner.calls if c[0] == "rev-parse" and "HEAD" not in " ".join(c)]
    assert len(full_sha_calls) == 0  # 完整 sha 不起进程

    await adapter.head_sha()
    await adapter.head_sha()
    head_calls = [c for c in runner.calls if "HEAD^{commit}" in c]
    assert len(head_calls) == 2  # 分支/HEAD 每次重解析，防移动后脏缓存


async def test_path_traversal_rejected():
    runner = FakeGitRunner().on("rev-parse", lambda a: ok(b"/tmp/demo-repo\n"))
    adapter = make_adapter(runner)
    for bad in ("../secret", "/etc/passwd", "-x", "a/b\0c", "a/../../b"):
        with pytest.raises(ValueError):
            await adapter.read_file_at_commit(REPO, bad, HEAD_SHA)


async def test_list_changes_empty_without_cursor(ready_adapter):
    adapter, _ = ready_adapter
    events = await adapter.list_changes(EventCheckpoint(source_system="git"))
    assert events == []


async def test_list_changes_parses_commits_without_message(ready_adapter):
    adapter, runner = ready_adapter
    log = (
        b"c1\x012026-01-01T00:00:00+08:00\0\nM\0a.swift\0"
        b"c2\x012026-01-02T00:00:00+08:00\0A\0b.swift\0"
    )
    runner.on("log", lambda a: ok(log))
    events = await adapter.list_changes(
        EventCheckpoint(source_system="git", last_cursor=BASE_SHA)
    )
    assert [e.event_id for e in events] == ["c1", "c2"]
    assert all(e.event_type == "CommitPushed" for e in events)
    assert events[0].payload == {"base": BASE_SHA, "files": ["a.swift"]}
    # +08:00 输入：转 UTC 后是前一天 16:00
    assert events[0].occurred_at.astimezone(timezone.utc).isoformat() == \
        "2025-12-31T16:00:00+00:00"
    assert "message" not in events[0].payload  # AC2：不依赖 message


# ── 真 git 二进制契约（tmp 仓，构造器见 git_helpers.build_demo_repo）─────────

# 只给真 git 测试加 skip：fake-runner 测试必须在无 git 环境也能跑
requires_git = pytest.mark.skipif(GIT_BIN is None, reason="环境无 git 二进制")


@pytest.fixture
def real_repo(tmp_path: Path):
    return build_demo_repo(tmp_path)


@pytest.fixture
def real_adapter(real_repo):
    cwd, base, head = real_repo
    repo = RepoRef(project="demo", repo_path=str(cwd), default_branch="main")
    return GitCodeRepository(repo, git_path=GIT_BIN), cwd, base, head


async def test_real_head_sha_and_read_roundtrip(real_adapter):
    adapter, _cwd, _base, head = real_adapter
    assert await adapter.head_sha() == head
    text = await adapter.read_file_at_commit(adapter.repo, "README.md", head)
    assert text == "# v2\n"
    with pytest.raises(PathMissingAtCommitError):
        await adapter.read_file_at_commit(adapter.repo, "nope.swift", head)


@requires_git
async def test_real_change_set_empty_message_commit(real_adapter):
    adapter, _cwd, base, head = real_adapter
    cs = await adapter.get_change_set(adapter.repo, base, head)
    assert cs.base_commit == base and cs.head_commit == head
    # rename + 文档改动都在文件清单
    assert "README.md" in cs.changed_files
    assert any(p.endswith("B.swift") for p in cs.changed_files)
    kinds = {c.symbol_id: c.change_type for c in cs.symbol_changes}
    # 内容同时变化的 rename 在 -M 下实测表现为 D+A（R100 仅纯重命名）
    assert kinds.get("C.fresh()") == ChangeType.ADDED
    # 类型块因 interval 90→30 级联 modified（语义正确的噪声，测试固定行为）
    assert "C" in kinds


@requires_git
async def test_real_symbol_snapshot_exists_changed_removed(real_adapter):
    adapter, _cwd, base, head = real_adapter
    # fresh 只在 head 存在；head 提交相对父提交变化 → body_changed True
    snap_new = await adapter.get_symbol_at_commit(
        adapter.repo, "Sources/B.swift#C.fresh()", head
    )
    assert snap_new.exists is True and snap_new.body_changed is True
    assert snap_new.symbol.commit == head
    assert snap_new.symbol.signature.startswith("func fresh()")

    # base 版本读取旧路径：存在，且 base（根后的首提交）相对父提交该符号也算变化
    snap_base = await adapter.get_symbol_at_commit(
        adapter.repo, "Sources/A.swift#C.keep()", base
    )
    assert snap_base.exists is True
    assert snap_base.symbol.line_start and snap_base.symbol.line_end

    # head 下不存在的符号
    snap_gone = await adapter.get_symbol_at_commit(
        adapter.repo, "Sources/B.swift#C.ghost()", head
    )
    assert snap_gone.exists is False
    assert snap_gone.symbol.body_hash == ""


@requires_git
async def test_real_corrupt_repo_is_unavailable(real_repo):
    cwd, _base, _head = real_repo
    repo = RepoRef(project="demo", repo_path=str(cwd))
    # 写坏 HEAD：rev-parse HEAD（读对象）失败
    (cwd / ".git" / "HEAD").write_text("ref: refs/heads/does-not-exist\n")
    adapter = GitCodeRepository(repo, git_path=GIT_BIN)
    with pytest.raises(AdapterUnavailableError):
        await adapter.head_sha()


@requires_git
async def test_real_list_changes(real_adapter):
    adapter, _cwd, base, head = real_adapter
    events = await adapter.list_changes(
        EventCheckpoint(source_system="git", last_cursor=base)
    )
    assert len(events) == 1
    assert events[0].event_id == head
    assert events[0].idempotency_key == head
    assert any(p.endswith("B.swift") for p in events[0].payload["files"])
