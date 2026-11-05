"""live-verify 适配器（docs/08 FR-QA-002，S1.3 能力层）。

回答"索引时的代码证据在目标版本是否仍成立"，不做答案层再生（S1.4）：
  URN → 库内切块（content_hash/valid_from/chunker 版本 + 所属文档 file:// URI）
      → URI 相对 repo_path 反推 git 路径 → scm 读目标 commit blob
      → 同一 chunk_swift/chunk_markdown 重切 → anchor 取块 → sha256
      → core.verify_evidence 纯判定（unchanged / body_changed / missing / 版本漂移）。

硬纪律：
- **每次调用（含失败/降级）必在 verification_events 落一行**——NoopTracer 的 span
  不落盘，FR-QA-002 AC2 的「trace 可查」由这张表保证；
- git 不可达 / 路径越界 / URN 不可解析 / 索引缺失 → degraded 结果，**永不抛穿**
  （本地 sqlite 故障仍按硬错误抛出，与检索 FTS 通道一致）；
- 文件在目标 commit 缺失 / anchor 重切找不到 → exists=False（非降级：仓库可达、
  事实就是符号没了）；
- chunker 版本缺失或漂移 → body_changed=None，绝不把 hash 差异误报成代码变更。
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from testpilot_core.chunking import (
    MARKDOWN_CHUNKER_VERSION,
    SWIFT_CHUNKER_VERSION,
    chunk_markdown,
    chunk_swift,
    select_chunk_by_anchor,
)
from testpilot_core.errors import AdapterUnavailableError, PathMissingAtCommitError
from testpilot_core.models import LiveVerifyResult, RepoRef
from testpilot_core.urn import parse_urn
from testpilot_core.verification import verify_evidence

from .tracers import NoopTracer

_ERROR_MAX = 300


class LiveVerifier:
    def __init__(
        self,
        scm,  # CodeRepositoryPort | None（None 时校验以 degraded 返回，仍落痕）
        store,
        repo: RepoRef,
        *,
        tracer=None,
        project: str = "",
    ) -> None:
        self.scm = scm
        self.store = store
        self.repo = repo
        self.tracer = tracer or NoopTracer()
        self.project = project or repo.project

    async def verify(self, urn: str, commit: str = "") -> LiveVerifyResult:
        t0 = time.monotonic()
        with self.tracer.span("live.verify", metadata={"urn": urn, "commit": commit}):
            try:
                result = await self._verify(urn, commit, t0)
            except Exception as exc:  # 安全网：意料之外的异常也降级+留痕，不抛穿
                result = self._degraded(
                    urn, t0, reason="unavailable",
                    error=f"{type(exc).__name__}: {exc}",
                    requested=commit,
                    raw={"error_type": type(exc).__name__},
                )
            # 落痕在 span 内：trace 与表行同属一次校验；sqlite 故障是硬错误，照常抛出
            await asyncio.to_thread(
                self.store.record_verification_event,
                result=result.model_dump(),
            )
        return result

    async def _verify(self, urn: str, commit: str, t0: float) -> LiveVerifyResult:
        # 1) URN 解析：chunk / symbol URN 的 external_id 都是 <doc_external>#<anchor>
        try:
            _system, rtype, external, _ = parse_urn(urn)
            doc_external, sep, anchor = external.partition("#")
            if rtype not in ("chunk", "symbol") or not sep or not anchor \
                    or not doc_external:
                raise ValueError("URN external_id 应为 <doc_external>#<anchor>")
        except ValueError as exc:
            return self._degraded(
                urn, t0, reason="unparseable_urn", error=str(exc), requested=commit
            )

        # 2) 索引侧证据
        ctx = await asyncio.to_thread(self.store.get_verification_context, urn)
        if ctx is None:
            return self._degraded(
                urn, t0, reason="index_missing",
                error=f"索引中查不到切块或符号: {urn}", requested=commit,
            )
        branch = ctx["branch"]

        # 3) scm 与 HEAD
        if self.scm is None:
            return self._degraded(
                urn, t0, reason="unavailable",
                error="未配置 git 适配器（scm=None）", requested=commit, branch=branch,
            )
        try:
            head = await self.scm.head_sha(self.repo)
            sha = commit or head
        except AdapterUnavailableError as exc:
            return self._degraded(
                urn, t0, reason="unavailable", error=str(exc),
                requested=commit, branch=branch,
                raw={"error_type": "AdapterUnavailableError"},
            )

        # 4) file:// URI → git 根相对路径（URN 对三个 code root 有歧义，只信文档 URI）
        git_path = self._git_path_from_uri(ctx["doc_uri"])
        if git_path is None:
            return self._degraded(
                urn, t0, reason="path_outside_repo",
                error=f"文档 URI 不在仓库 {self.repo.repo_path} 内: {ctx['doc_uri']}",
                requested=commit, head=head, branch=branch,
            )

        # 5) 读目标 commit blob
        try:
            text = await self.scm.read_file_at_commit(self.repo, git_path, sha)
        except PathMissingAtCommitError:
            # 仓库可达、文件在该版本不存在：事实结论（符号没了），不是降级
            return self._success(
                urn, t0, exists=False,
                current_chunker_version="", ctx=ctx, requested=commit, head=head,
                reason="symbol_missing",
                raw={"git_path": git_path, "anchor": anchor, "file_missing": True},
            )
        except AdapterUnavailableError as exc:
            return self._degraded(
                urn, t0, reason="unavailable", error=str(exc),
                requested=commit, head=head, branch=branch,
                raw={"git_path": git_path, "error_type": "AdapterUnavailableError"},
            )

        # 6) 同一提取器重切 → anchor 取块 → 纯判定
        suffix = Path(git_path).suffix.lower()
        if suffix == ".swift":
            chunks = chunk_swift(text)
            current_version = SWIFT_CHUNKER_VERSION
        elif suffix in {".md", ".markdown"}:
            chunks = chunk_markdown(text)
            current_version = MARKDOWN_CHUNKER_VERSION
        else:
            return self._degraded(
                urn, t0, reason="unsupported_file_type",
                error=f"live-verify 不支持的文件类型: {git_path}",
                requested=commit, head=head, branch=branch,
                raw={"git_path": git_path},
            )

        current = select_chunk_by_anchor(chunks, anchor)
        current_hash = hashlib.sha256(current.text.encode("utf-8")).hexdigest() \
            if current is not None else None
        verdict = verify_evidence(
            exists=current is not None,
            indexed_hash=ctx["content_hash"],
            current_hash=current_hash,
            indexed_chunker_version=ctx["chunker_version"],
            current_chunker_version=current_version,
        )
        return self._success(
            urn, t0,
            exists=verdict.exists,
            current_chunker_version=current_version,
            ctx=ctx, requested=commit, head=head, reason=verdict.reason,
            body_changed=verdict.body_changed, verified=verdict.verified,
            raw={"git_path": git_path, "anchor": anchor},
        )

    def _git_path_from_uri(self, doc_uri: str) -> str | None:
        parsed = urlparse(doc_uri)
        if parsed.scheme != "file" or not parsed.path:
            return None
        try:
            disk = Path(unquote(parsed.path)).resolve(strict=False)
            root = Path(self.repo.repo_path).resolve(strict=False)
            return disk.relative_to(root).as_posix()
        except (ValueError, OSError):
            return None

    def _success(
        self, urn, t0, *, exists, current_chunker_version, ctx,
        requested, head, reason, body_changed=None, verified=False, raw=None,
    ) -> LiveVerifyResult:
        return LiveVerifyResult(
            urn=urn,
            kind=ctx["kind"],
            requested_commit=requested,
            head_commit=head,
            exists=exists,
            verified=verified,
            # None=无 body 可比（缺失）或不可归因（版本漂移）；True/False 才有事实含义
            body_changed=body_changed,
            reason=reason,
            degraded=False,
            indexed_commit=ctx["valid_from"],
            indexed_chunker_version=ctx["chunker_version"],
            current_chunker_version=current_chunker_version,
            latency_ms=int((time.monotonic() - t0) * 1000),
            project=self.project,
            branch=ctx["branch"],
            raw=raw or {},
        )

    def _degraded(
        self, urn, t0, *, reason, error, requested="", head="", branch="", raw=None
    ) -> LiveVerifyResult:
        return LiveVerifyResult(
            urn=urn,
            requested_commit=requested,
            head_commit=head,
            exists=None,
            verified=False,
            body_changed=None,
            reason=reason,
            degraded=True,
            error=str(error)[:_ERROR_MAX],
            latency_ms=int((time.monotonic() - t0) * 1000),
            project=self.project,
            branch=branch,
            raw=raw or {},
        )
