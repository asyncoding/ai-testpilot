"""Git 仓库连接器（CodeRepositoryPort 真实适配器，docs/08 FR-CON-001）。

零三方依赖：通过 git CLI 子进程工作（与项目"零 SDK"哲学一致；未来换 Git API
只需替换适配器）。纪律：

- **纯懒构造**：__init__ 不起任何 git 进程（装配测试使用不存在的 repo 路径），
  首次调用才 rev-parse 发现仓库根并缓存；
- 子进程 env 注 GIT_TERMINAL_PROMPT=0（远端不可达绝不卡在凭据输入）、LC_ALL=C
  （stderr 分类句式稳定）；
- 故障全部归类成可识别错误：超时/git 缺失/非仓库/损坏仓 → AdapterUnavailableError；
  「仓库可达但 commit 下无此路径」→ PathMissingAtCommitError（live-verify 据此
  判 exists=False，而不是 degraded）；
- 不依赖 commit message：ChangeSet 与事件全部从 tree/diff 内容产出，
  空 message commit 同样有效（FR-CON-001 AC2）。

生产替代：平台 Git API（GitHub/GitLab/内部 Gerrit）适配器实现同一端口。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from testpilot_core.chunking import RawChunk, chunk_swift, select_chunk_by_anchor
from testpilot_core.diffing import diff_symbol_sets
from testpilot_core.errors import AdapterUnavailableError, PathMissingAtCommitError
from testpilot_core.models import (
    ChangeSet,
    CodeSymbol,
    EventCheckpoint,
    EventEnvelope,
    FileRename,
    RepoRef,
    SymbolChange,
    SymbolSnapshot,
)
from testpilot_core.urn import make_urn

_EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"  # git 空树魔法 sha
_SWIFT_SUFFIX = ".swift"
_MAX_CHANGES = 50
_ALL_TREE_CACHE = 8  # 裸 selector 全仓兜底索引的 LRU 封顶（commit 数）


@dataclass(frozen=True)
class GitResult:
    rc: int
    stdout: bytes
    stderr: str


class SubprocessGitRunner:
    """默认 runner：asyncio 子进程执行 git；超时 kill，不残留进程。"""

    def __init__(self, git_path: str = "git") -> None:
        self.git_path = git_path

    async def run(
        self, args: list[str], *, cwd: Path, timeout_s: float, extra_env: dict[str, str]
    ) -> GitResult:
        env = {**os.environ, **extra_env}
        # cwd 不存在时子进程抛的也是 FileNotFoundError，会与"git 二进制缺失"混淆；
        # NFR-006 要求错误可识别，先显式判目录（外部仓未挂载/路径配错走这条）
        if not cwd.is_dir():
            raise AdapterUnavailableError(f"仓库目录不存在或不可访问: {cwd}")
        try:
            proc = await asyncio.create_subprocess_exec(
                self.git_path, *args,
                cwd=str(cwd),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise AdapterUnavailableError(
                f"找不到 git 可执行文件: {self.git_path}"
            ) from exc
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise AdapterUnavailableError(
                f"git 命令超时（>{timeout_s}s）: {' '.join(args[:3])}"
            )
        return GitResult(
            rc=proc.returncode or 0,
            stdout=out,
            stderr=err.decode("utf-8", errors="replace"),
        )


# ── -z 输出解析（纯函数，便于 hostless 单测）─────────────────────────────────


def parse_name_status(stdout: bytes) -> list[tuple[str, str, str | None]]:
    """解析 git diff --name-status -z：[(status, path_old, path_new|None)]。

    M/A/D/T 单路径；R/C（相似度分数，如 R100）双路径。未知状态码防御性按
    修改处理（文件仍计入 changed_files），不抛解析异常。
    """
    tokens = [t.decode("utf-8", errors="replace") for t in stdout.split(b"\0") if t]
    entries: list[tuple[str, str, str | None]] = []
    i = 0
    while i < len(tokens):
        status = tokens[i]
        code = status[:1]
        if code in {"R", "C"} and i + 2 < len(tokens):
            entries.append((status, tokens[i + 1], tokens[i + 2]))
            i += 3
        elif code and i + 1 < len(tokens):
            entries.append((status, tokens[i + 1], None))
            i += 2
        else:  # 畸形尾部，不静默丢信息
            entries.append((f"X{status}", status, None))
            i += 1
    return entries


def parse_log_name_status(
    stdout: bytes,
) -> list[tuple[str, str, list[tuple[str, str, str | None]]]]:
    """解析 git log --name-status -z --format=%H\\x01%cI：[(sha, 提交时间, 文件变更)]。

    每个 commit 块以含 \\x01 的 header 起头；其后是与 name-status 同构的状态对。
    状态 token 可能带前导 \\n（log 在 header 后固定输出换行）。
    """
    tokens = [t for t in stdout.split(b"\0") if t]
    commits: list[tuple[str, str, list[tuple[str, str, str | None]]]] = []
    current_sha: str | None = None
    current_date = ""
    files: list[tuple[str, str, str | None]] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if b"\x01" in token:
            if current_sha is not None:
                commits.append((current_sha, current_date, files))
            sha_b, _, date_b = token.partition(b"\x01")
            current_sha = sha_b.decode().strip()
            current_date = date_b.decode().strip()
            files = []
            i += 1
            continue
        status = token.decode("utf-8", errors="replace").strip()
        code = status[:1]
        if code in {"R", "C"} and i + 2 < len(tokens):
            files.append((status, tokens[i + 1].decode(), tokens[i + 2].decode()))
            i += 3
        elif code and i + 1 < len(tokens):
            files.append((status, tokens[i + 1].decode(), None))
            i += 2
        else:
            i += 1
    if current_sha is not None:
        commits.append((current_sha, current_date, files))
    return commits


def _validate_git_path(path: str) -> None:
    """git show <rev>:<path> 无法用 -- 隔断，路径参数必须自验，防注入/穿越。"""
    if not path or "\0" in path:
        raise ValueError("非法 git 路径（空或含 NUL）")
    if path.startswith("-") or path.startswith("/") or path.startswith("\\"):
        raise ValueError(f"非法 git 路径（前导字符）: {path}")
    parts = path.replace("\\", "/").split("/")
    if any(p in ("", "..", ".") for p in parts) or ":" in path:
        raise ValueError(f"非法 git 路径（含 .. 或绝对段或 ':'）: {path}")


def _split_symbol_id(symbol_id: str) -> tuple[str, str]:
    """<git 相对路径>#<anchor>；anchor 内部可能再带 #L 行号去歧。"""
    path, sep, anchor = symbol_id.partition("#")
    return (path, anchor) if sep else ("", symbol_id)


class GitCodeRepository:
    """CodeRepositoryPort 的 git CLI 实现；一个适配器绑一个 RepoRef。"""

    def __init__(
        self,
        repo: RepoRef,
        *,
        git_path: str = "git",
        timeout_s: float = 15.0,
        runner: Callable[..., Awaitable[GitResult]] | None = None,
    ) -> None:
        self.repo = repo
        self.timeout_s = timeout_s
        self._runner = runner or SubprocessGitRunner(git_path)
        self._extra_env = {"GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}
        self._root: Path | None = None
        self._sha_cache: dict[str, str] = {}  # 只缓存完整 sha，HEAD/分支名每次重解析
        self._chunks_cache: dict[tuple[str, str], list[RawChunk]] = {}
        self._tree_cache: OrderedDict[str, dict[str, list[RawChunk]]] = OrderedDict()

    # ── 低层 git 调用 ───────────────────────────────────────────────────────

    async def _exec(self, args: list[str]) -> GitResult:
        await self._ensure_root()
        cwd = Path(self.repo.repo_path).expanduser()
        try:
            return await asyncio.wait_for(
                self._runner.run(
                    args, cwd=cwd, timeout_s=self.timeout_s, extra_env=self._extra_env
                ),
                timeout=self.timeout_s,
            )
        except AdapterUnavailableError:
            raise
        except TimeoutError as exc:
            raise AdapterUnavailableError(
                f"git 命令超时（>{self.timeout_s}s）: {' '.join(args[:3])}"
            ) from exc

    async def _ensure_root(self) -> Path:
        if self._root is not None:
            return self._root
        cwd = Path(self.repo.repo_path).expanduser()
        try:
            res = await asyncio.wait_for(
                self._runner.run(
                    ["rev-parse", "--show-toplevel"],
                    cwd=cwd,
                    timeout_s=self.timeout_s,
                    extra_env=self._extra_env,
                ),
                timeout=self.timeout_s,
            )
        except AdapterUnavailableError:
            raise
        except TimeoutError as exc:
            raise AdapterUnavailableError("git rev-parse 超时") from exc
        if res.rc != 0:
            raise AdapterUnavailableError(
                f"路径不是可用 git 仓库（{cwd}）: {res.stderr.strip()[:200]}"
            )
        self._root = Path(res.stdout.decode("utf-8", errors="replace").strip())
        return self._root

    async def _resolve(self, rev: str) -> str:
        """ref/sha → 完整 sha。完整 sha 直接缓存；HEAD/分支名每次重解析（防分支移动脏缓存）。"""
        rev = rev.strip()
        if len(rev) == 40 and all(c in "0123456789abcdef" for c in rev.lower()):
            self._sha_cache[rev] = rev
            return rev
        res = await self._exec(["rev-parse", "--verify", f"{rev}^{{commit}}"])
        if res.rc != 0:
            raise AdapterUnavailableError(f"无法解析 git 版本 {rev!r}: {res.stderr.strip()[:200]}")
        sha = res.stdout.decode().strip()
        self._sha_cache[sha] = sha
        return sha

    async def head_sha(self, repo: RepoRef | None = None) -> str:
        return await self._resolve("HEAD")

    async def _show(self, sha: str, path: str) -> str | None:
        """读 blob；commit 下路径不存在返回 None；其他故障抛 AdapterUnavailableError。"""
        res = await self._exec(["show", f"{sha}:{path}"])
        if res.rc != 0:
            # git 有两种缺路径句式："does not exist in '<sha>'"（工作区也没有）与
            # "exists on disk, but not in '<sha>'"（该 commit 没有、工作区有）
            if res.rc == 128 and (
                "does not exist in" in res.stderr
                or "exists on disk, but not in" in res.stderr
            ):
                return None
            raise AdapterUnavailableError(
                f"git show 失败 {sha}:{path}: {res.stderr.strip()[:200]}"
            )
        try:
            return res.stdout.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AdapterUnavailableError(f"文件非 UTF-8，无法词法切块: {path}") from exc

    # ── 端口方法 ────────────────────────────────────────────────────────────

    async def read_file_at_commit(
        self, repo: RepoRef, path: str, commit: str
    ) -> str:
        _validate_git_path(path)
        sha = await self._resolve(commit)
        text = await self._show(sha, path)
        if text is None:
            raise PathMissingAtCommitError(f"路径 {path} 在 {sha[:12]} 不存在")
        return text

    async def get_change_set(
        self, repo: RepoRef, base: str, head: str
    ) -> ChangeSet:
        base_sha = _EMPTY_TREE if not base or base == _EMPTY_TREE else await self._resolve(base)
        head_sha = await self._resolve(head or "HEAD")
        res = await self._exec(
            ["diff", "--name-status", "-z", "-M", base_sha, head_sha]
        )
        if res.rc != 0:
            raise AdapterUnavailableError(f"git diff 失败: {res.stderr.strip()[:200]}")
        entries = parse_name_status(res.stdout)

        changed_files: list[str] = []
        symbol_changes: list[SymbolChange] = []
        file_renames: list[FileRename] = []
        for status, old_path, new_path in entries:
            code = status[:1]
            if code in {"R", "C"}:
                changed_files.extend(p for p in (old_path, new_path) if p not in changed_files)
                # S4.2：git -M 相似度后缀（R100/C75）；裸 R/C 无分数时给 None 不臆造
                score = status[1:]
                similarity = int(score) if score.isdigit() else None
                file_renames.append(
                    FileRename(old_path=old_path, new_path=new_path, similarity=similarity)
                )
            else:
                p = old_path
                if p not in changed_files:
                    changed_files.append(p)

            # 符号对账只对 swift 做；rename 用 old@base vs new@head 配对，
            # 不会被当成全删+全增（M/A/D 的路径在第二字段，R/C 第三字段才是新路径）
            old_p, new_p = (old_path, new_path) if code in {"R", "C"} else (old_path, old_path)
            if not old_p.endswith(_SWIFT_SUFFIX) and not (new_p or "").endswith(_SWIFT_SUFFIX):
                continue
            old_chunks = await self._swift_chunks(base_sha, old_p) if code != "A" else []
            new_chunks = (
                await self._swift_chunks(head_sha, new_p)
                if code != "D" else []
            )
            symbol_changes.extend(diff_symbol_sets(old_chunks or [], new_chunks or []))

        return ChangeSet(
            repo=self.repo,
            base_commit=base_sha,
            head_commit=head_sha,
            changed_files=changed_files,
            symbol_changes=symbol_changes,
            file_renames=file_renames,
        )

    async def get_symbol_at_commit(
        self, repo: RepoRef, symbol_id: str, commit: str
    ) -> SymbolSnapshot:
        path, anchor = _split_symbol_id(symbol_id)
        sha = await self._resolve(commit)

        if path:
            _validate_git_path(path)
            chunks = await self._swift_chunks(sha, path)
            parent_chunks = await self._parent_chunks(sha, path)
        else:
            tree = await self._all_swift_tree(sha)
            chunks = _find_in_tree(tree, anchor)
            parent_tree = await self._parent_tree(sha)
            parent_chunks = _find_in_tree(parent_tree, anchor)

        chunk = select_chunk_by_anchor(chunks, anchor)
        base_id = anchor.split("#L", 1)[0]
        if chunk is None:
            return SymbolSnapshot(
                symbol=CodeSymbol(
                    urn=make_urn(
                        "git", "symbol",
                        f"{path}#{base_id}" if path else base_id,
                    ),
                    language="swift",
                    symbol_id=base_id,
                    file_path=path,
                    commit=sha,
                    body_hash="",
                ),
                exists=False,
                body_changed=False,
            )

        parent_chunk = select_chunk_by_anchor(parent_chunks, anchor)
        body_changed = parent_chunk is None or parent_chunk.text != chunk.text
        called_by = sorted({
            c.anchor
            for c in chunks
            if any(r.target_symbol in {anchor, base_id} for r in c.relations)
        })
        return SymbolSnapshot(
            symbol=CodeSymbol(
                urn=make_urn("git", "symbol", f"{path}#{base_id}" if path else base_id),
                language="swift",
                symbol_id=base_id,
                file_path=path,
                line_start=chunk.start_line,
                line_end=chunk.end_line,
                signature=chunk.signature,
                commit=sha,
                body_hash=hashlib.sha256(chunk.text.encode("utf-8")).hexdigest(),
                calls=[r.target_symbol for r in chunk.relations],
                called_by=called_by,
            ),
            exists=True,
            body_changed=body_changed,
        )

    async def list_changes(self, checkpoint: EventCheckpoint) -> list[EventEnvelope]:
        """cursor（sha）..HEAD 的提交事件，封顶 50；无 cursor 返回 []（不倾倒全史）。"""
        cursor = checkpoint.last_cursor
        if not cursor:
            return []
        args = [
            "log", "--name-status", "-z", "-M",
            "--format=%H%x01%cI", f"--max-count={_MAX_CHANGES}",
            f"{cursor}..HEAD",
        ]
        if checkpoint.last_event_at is not None:
            args.insert(-1, f"--since={checkpoint.last_event_at.isoformat()}")
        res = await self._exec(args)
        if res.rc != 0:
            raise AdapterUnavailableError(f"git log 失败: {res.stderr.strip()[:200]}")

        events: list[EventEnvelope] = []
        for sha, date_iso, files in parse_log_name_status(res.stdout):
            paths: list[str] = []
            for _status, old_path, new_path in files:
                for p in (old_path, new_path):
                    if p and p not in paths:
                        paths.append(p)
            events.append(
                EventEnvelope(
                    event_id=sha,
                    event_type="CommitPushed",
                    source_system="git",
                    occurred_at=datetime.fromisoformat(date_iso),  # git %cI 严格 ISO
                    project=self.repo.project,
                    subject_ref=sha,
                    payload={"base": cursor, "files": paths},
                    idempotency_key=sha,
                )
            )
        return events

    # ── 切块缓存与全仓兜底 ──────────────────────────────────────────────────

    async def _swift_chunks(self, sha: str, path: str) -> list[RawChunk] | None:
        key = (sha, path)
        if key not in self._chunks_cache:
            text = await self._show(sha, path)
            self._chunks_cache[key] = chunk_swift(text) if text is not None else []
        return self._chunks_cache[key]

    async def _parent_sha(self, sha: str) -> str:
        res = await self._exec(["rev-parse", f"{sha}^@"])
        # 根提交无父提交：rc=128 → 空树
        if res.rc != 0 or not res.stdout.decode().strip():
            return _EMPTY_TREE
        return res.stdout.decode().strip().splitlines()[0]

    async def _parent_chunks(self, sha: str, path: str) -> list[RawChunk]:
        parent = await self._parent_sha(sha)
        if parent == _EMPTY_TREE:
            return []
        return await self._swift_chunks(parent, path) or []

    async def _all_swift_tree(self, sha: str) -> dict[str, list[RawChunk]]:
        if sha in self._tree_cache:
            self._tree_cache.move_to_end(sha)
            return self._tree_cache[sha]
        res = await self._exec(["ls-tree", "-r", "-z", "--name-only", sha])
        if res.rc != 0:
            raise AdapterUnavailableError(f"git ls-tree 失败: {res.stderr.strip()[:200]}")
        paths = [
            p.decode()
            for p in res.stdout.split(b"\0")
            if p and p.decode().endswith(_SWIFT_SUFFIX)
        ]
        tree: dict[str, list[RawChunk]] = {}
        for path in paths:
            chunks = await self._swift_chunks(sha, path)
            if chunks:
                tree[path] = chunks
        self._tree_cache[sha] = tree
        self._tree_cache.move_to_end(sha)
        while len(self._tree_cache) > _ALL_TREE_CACHE:
            self._tree_cache.popitem(last=False)
        return tree

    async def _parent_tree(self, sha: str) -> dict[str, list[RawChunk]]:
        parent = await self._parent_sha(sha)
        return {} if parent == _EMPTY_TREE else await self._all_swift_tree(parent)


def _find_in_tree(
    tree: dict[str, list[RawChunk]], anchor: str
) -> list[RawChunk]:
    found: list[RawChunk] = []
    base = anchor.split("#L", 1)[0]
    for chunks in tree.values():
        for c in chunks:
            if c.anchor == anchor or c.anchor.split("#L", 1)[0] == base:
                found.append(c)
    return found
