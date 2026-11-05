"""录制型 git 仓库适配器（CodeRepositoryPort）——offline-demo profile / 离线测试用。

应答全部来自 tests/fixtures/git/ 下由 scripts/record_git_fixtures.py 用**真实
GitCodeRepository** 录下的 JSON（固定 author/date 的确定性演示仓，sha 跨机器可复现），
契约测试 tests/test_contract_git.py 会实时重建同一演示仓逐字段对账（FR-CFG-002 AC2：
每个端口必须同时有真实适配器与离线录制适配器）。

与真实适配器同构的错误语义：
- 夹具目录缺失 / 录制中没有对应 key → AdapterUnavailableError（调用方按"git 不可达"降级）；
- 录制中显式标 missing 的 blob → PathMissingAtCommitError（仓库可达、该 commit 下无此路径）。

path_map：离线 profile 里文档根（tests/fixtures/docs）不在录制仓内，没有 git 历史。
逻辑路径映射到当前磁盘文件后，read_file_at_commit 直接读盘——等价于"HEAD 工作区内容"，
让离线 live_verify 能真实重切块并判定 hash（改文件即可复现 body_changed）。
相对路径相对 fixtures_dir 解析，保证配置不写死机器绝对路径。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from testpilot_core.errors import AdapterUnavailableError, PathMissingAtCommitError
from testpilot_core.models import (
    ChangeSet,
    EventCheckpoint,
    EventEnvelope,
    RepoRef,
    SymbolSnapshot,
)

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class RecordedGitRepository:
    def __init__(
        self,
        repo: RepoRef,
        fixtures_dir: str | Path,
        *,
        path_map: dict[str, str] | None = None,
    ) -> None:
        self.repo = repo
        self.dir = Path(fixtures_dir)
        self.path_map = dict(path_map or {})
        self._meta: dict[str, Any] | None = None
        self._manifests: dict[str, dict[str, Any]] = {}

    def _load_meta(self) -> dict[str, Any]:
        if self._meta is None:
            meta_path = self.dir / "repo.json"
            if not meta_path.is_file():
                raise AdapterUnavailableError(
                    f"录制 git 夹具缺失或不可读: {meta_path}（运行 "
                    f"scripts/record_git_fixtures.py 重新生成）"
                )
            self._meta = json.loads(meta_path.read_text(encoding="utf-8"))
        return self._meta

    def _manifest(self, name: str) -> dict[str, Any]:
        if name not in self._manifests:
            p = self.dir / name / "manifest.json"
            if not p.is_file():
                raise AdapterUnavailableError(f"录制 git 夹具缺少 manifest: {p}")
            self._manifests[name] = json.loads(p.read_text(encoding="utf-8"))
        return self._manifests[name]

    def _resolve(self, rev: str) -> str:
        """角色名（HEAD/root/head）与完整 sha 可解析；其余一律不可达。"""
        meta = self._load_meta()
        if rev == "HEAD":
            return meta["head_sha"]
        role = meta.get("commits", {}).get(rev)
        if role:
            return role
        if _SHA_RE.match(rev):
            return rev
        raise AdapterUnavailableError(f"录制 git 夹具无法解析版本引用: {rev!r}")

    async def head_sha(self, repo: RepoRef | None = None) -> str:
        return self._load_meta()["head_sha"]

    async def read_file_at_commit(
        self, repo: RepoRef, path: str, commit: str
    ) -> str:
        sha = self._resolve(commit)
        if path in self.path_map:
            mapped = Path(self.path_map[path])
            if not mapped.is_absolute():
                mapped = self.dir / mapped
            if not mapped.is_file():
                raise AdapterUnavailableError(
                    f"path_map 映射的文件不存在: {path} -> {mapped}"
                )
            return mapped.read_text(encoding="utf-8")
        entry = self._manifest("blobs").get(f"{sha}:{path}")
        if entry is None:
            raise AdapterUnavailableError(
                f"录制 git 夹具缺少 blob 应答: {sha[:12]}:{path}"
            )
        if entry.get("missing"):
            raise PathMissingAtCommitError(
                f"录制仓中 {path} 在 {sha[:12]} 下不存在（{entry.get('reason', '')}）"
            )
        return (self.dir / entry["file"]).read_text(encoding="utf-8")

    async def get_change_set(
        self, repo: RepoRef, base: str, head: str
    ) -> ChangeSet:
        base_sha = self._resolve(base)
        head_sha = self._resolve(head)
        rel = self._manifest("change_sets").get(f"{base_sha}..{head_sha}")
        if rel is None:
            raise AdapterUnavailableError(
                f"录制 git 夹具缺少 ChangeSet: {base_sha[:12]}..{head_sha[:12]}"
            )
        data = json.loads((self.dir / rel).read_text(encoding="utf-8"))
        data["repo"] = self.repo.model_dump()
        return ChangeSet.model_validate(data)

    async def get_symbol_at_commit(
        self, repo: RepoRef, symbol_id: str, commit: str
    ) -> SymbolSnapshot:
        sha = self._resolve(commit)
        rel = self._manifest("snapshots").get(f"{sha}:{symbol_id}")
        if rel is None:
            raise AdapterUnavailableError(
                f"录制 git 夹具缺少符号快照: {sha[:12]}:{symbol_id}"
            )
        data = json.loads((self.dir / rel).read_text(encoding="utf-8"))
        return SymbolSnapshot.model_validate(data)

    async def list_changes(
        self, checkpoint: EventCheckpoint
    ) -> list[EventEnvelope]:
        # 与真实适配器一致：无 cursor 做首次全量引导 → 返回空。
        if not checkpoint.last_cursor:
            return []
        sha = self._resolve(checkpoint.last_cursor)
        rel = self._manifest("commits").get(sha)
        if rel is None:
            raise AdapterUnavailableError(
                f"录制 git 夹具缺少 commit 回放: {sha[:12]}"
            )
        data = json.loads((self.dir / rel).read_text(encoding="utf-8"))
        return [EventEnvelope.model_validate(item) for item in data]
