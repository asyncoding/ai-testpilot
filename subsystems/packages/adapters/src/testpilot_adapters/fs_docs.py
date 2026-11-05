"""文件系统知识源适配器（KnowledgeSourcePort，docs/08 FR-CON-002）。

- 只读扫描配置目录；Markdown 与 Swift 源码两类根；
- 文档 URN 基于「根别名/相对路径」，**不随内容变化**，内容变化体现在
  version/content_hash（hash 由入库管线对账，表达"同一文档的新版本"）；
- 权威等级可按 glob 覆盖；支持语料盘点导出（K1 开建用）。
生产替代：Confluence/飞书/内部知识库/Git API（docs/04 §4.1）。
"""

from __future__ import annotations

import fnmatch
import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from testpilot_core.enums import AuthorityLevel
from testpilot_core.errors import AdapterUnavailableError
from testpilot_core.models import EventCheckpoint, SourceDocument
from testpilot_core.urn import make_urn, parse_urn

_SOURCE_SYSTEM = "fs-docs"
_MD_PATTERN = "**/*.md"
_SWIFT_PATTERN = "**/*.swift"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RootSpec:
    path: Path
    alias: str
    pattern: str = _MD_PATTERN


@dataclass(frozen=True)
class AuthorityRule:
    glob: str
    level: AuthorityLevel


class FileSystemDocsAdapter:
    def __init__(
        self,
        roots: Iterable[str | Path],
        *,
        code_roots: Iterable[str | Path] = (),
        excludes: Sequence[str] = (),
        authority_overrides: Sequence[AuthorityRule | dict] = (),
        default_authority: AuthorityLevel = AuthorityLevel.L2_EXPERIENCE,
        code_authority: AuthorityLevel = AuthorityLevel.L1_AUTHORITATIVE,
        branch: str = "main",
    ) -> None:
        self.roots = self._build_specs(roots, _MD_PATTERN)
        self.roots.extend(self._build_specs(code_roots, _SWIFT_PATTERN))
        self.branch = branch
        self.excludes = list(excludes)
        self.rules = [
            r if isinstance(r, AuthorityRule)
            else AuthorityRule(glob=r["glob"], level=AuthorityLevel(r["level"]))
            for r in authority_overrides
        ]
        self.default_authority = default_authority
        self.code_authority = code_authority

    @staticmethod
    def _build_specs(roots: Iterable[str | Path], pattern: str) -> list[RootSpec]:
        specs: list[RootSpec] = []
        used: dict[str, int] = {}
        for raw in roots:
            path = Path(raw).expanduser().resolve()
            alias = path.name or "root"
            used[alias] = used.get(alias, 0) + 1
            if used[alias] > 1:
                alias = f"{alias}-{used[alias]}"
            specs.append(RootSpec(path=path, alias=alias, pattern=pattern))
        return specs

    # ── 扫描 ────────────────────────────────────────────────────────────
    def _is_excluded(self, candidate: str) -> bool:
        # candidate 同时给绝对路径和「根别名/相对路径」两种形态匹配
        return any(fnmatch.fnmatch(candidate, pat) for pat in self.excludes)

    def _iter_files(self) -> list[tuple[RootSpec, Path]]:
        found: list[tuple[RootSpec, Path]] = []
        for spec in self.roots:
            if not spec.path.exists():
                raise AdapterUnavailableError(f"文档根目录不存在: {spec.path}")
            for p in sorted(spec.path.glob(spec.pattern)):
                if not p.is_file():
                    continue
                rel = p.relative_to(spec.path).as_posix()
                if self._is_excluded(rel) or self._is_excluded(f"{spec.alias}/{rel}") \
                        or self._is_excluded(str(p)):
                    continue
                found.append((spec, p))
        return found

    def _external_id(self, spec: RootSpec, path: Path) -> str:
        rel = path.relative_to(spec.path).as_posix()
        return f"{spec.alias}/{rel}"

    def _authority_for(self, spec: RootSpec, rel: str, pattern: str) -> AuthorityLevel:
        candidate = f"{spec.alias}/{rel}"
        for rule in self.rules:
            if fnmatch.fnmatch(candidate, rule.glob) or fnmatch.fnmatch(rel, rule.glob):
                return rule.level
        return self.code_authority if pattern == _SWIFT_PATTERN else self.default_authority

    def _to_document(self, spec: RootSpec, path: Path) -> SourceDocument:
        text = path.read_text(encoding="utf-8")
        stat = path.stat()
        rel = path.relative_to(spec.path).as_posix()
        ext = path.suffix.lower()
        kind_hint = "swift" if ext == ".swift" else "markdown"
        return SourceDocument(
            urn=make_urn(_SOURCE_SYSTEM, "document", self._external_id(spec, path)),
            source_system=_SOURCE_SYSTEM,
            uri=f"file://{path.resolve()}",
            title=path.stem,
            version=_sha256(text)[:12],
            content_hash=_sha256(text),
            authority_level=self._authority_for(spec, rel, spec.pattern),
            branch=self.branch,  # factory 注入 project.default_branch（S1.3 branch 一致性）
            mtime=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
            metadata_={
                "relative_path": str(path),
                "root_alias": spec.alias,
                "relative_in_root": rel,
                "kind_hint": kind_hint,
                "size": stat.st_size,
            },
        )

    async def list_documents(self) -> list[SourceDocument]:
        return [self._to_document(spec, p) for spec, p in self._iter_files()]

    def scan_safely(self) -> tuple[list[SourceDocument], list[dict]]:
        """逐文件扫描并隔离读失败（FR-ING-001 AC2：单文档失败不影响整批）。

        返回 (documents, errors)；errors 元素含 urn/uri/error，供管线进死信。
        """
        docs: list[SourceDocument] = []
        errors: list[dict] = []
        for spec, p in self._iter_files():
            try:
                docs.append(self._to_document(spec, p))
            except Exception as exc:  # noqa: BLE001 - 扫描隔离是显式设计
                rel = p.relative_to(spec.path).as_posix()
                errors.append({
                    "urn": make_urn(_SOURCE_SYSTEM, "document", f"{spec.alias}/{rel}"),
                    "uri": f"file://{p.resolve()}",
                    "relative_path": rel,
                    "error": f"{type(exc).__name__}: {exc}",
                })
        return docs, errors

    def _resolve_path(self, urn: str) -> tuple[RootSpec, Path]:
        _, _, external_id, _ = parse_urn(urn)
        alias, _, rel = external_id.partition("/")
        spec = next((s for s in self.roots if s.alias == alias), None)
        if spec is None or not rel:
            raise AdapterUnavailableError(f"URN 不属于本知识源的已配置根: {urn}")
        path = (spec.path / rel).resolve()
        # 防 rel 越界（../）逃出配置根
        if not (str(path) == str(spec.path) or str(path).startswith(str(spec.path) + "/")):
            raise AdapterUnavailableError(f"文档路径越界: {urn}")
        return spec, path

    def read_content(self, urn: str) -> str:
        """读正文（入库管线加工用；KnowledgeSourcePort.read_document 只返回元数据）。"""
        _, path = self._resolve_path(urn)
        if not path.exists():
            raise AdapterUnavailableError(f"文档已不存在: {path}")
        return path.read_text(encoding="utf-8")

    async def read_document(self, urn: str) -> SourceDocument:
        spec, path = self._resolve_path(urn)
        if not path.exists():
            raise AdapterUnavailableError(f"文档已不存在: {path}")
        return self._to_document(spec, path)

    async def list_changes(self, checkpoint: EventCheckpoint) -> list:
        """mtime 水位增量（事件源语义）；删除/哈希对账在入库管线中做（FR-CON-002 AC1/AC2）。"""
        from testpilot_core.models import EventEnvelope

        since = datetime.fromisoformat(checkpoint.last_cursor) if checkpoint.last_cursor else None
        events: list[EventEnvelope] = []
        for doc in await self.list_documents():
            if since is None or (doc.mtime and doc.mtime > since):
                events.append(
                    EventEnvelope(
                        event_id=f"doc:{doc.urn}",
                        event_type="DocumentChanged",
                        source_system=_SOURCE_SYSTEM,
                        occurred_at=doc.mtime or datetime.now(timezone.utc),
                        project="",
                        subject_ref=doc.urn,
                        payload={"content_hash": doc.content_hash},
                        idempotency_key=f"doc:{doc.urn}",
                    )
                )
        return events

    def inventory(self) -> list[dict]:
        """语料盘点视图（位置/mtime/hash/权威等级/类型），FR-CON-002 AC3。同步方法供 CLI 导出。"""
        rows: list[dict] = []
        for spec, path in self._iter_files():
            doc = self._to_document(spec, path)
            rows.append({
                "urn": doc.urn,
                "uri": doc.uri,
                "root_alias": spec.alias,
                "relative_path": doc.metadata_["relative_in_root"],
                "kind": doc.metadata_["kind_hint"],
                "mtime": doc.mtime.isoformat() if doc.mtime else None,
                "size": doc.metadata_["size"],
                "content_hash": doc.content_hash,
                "authority_level": str(doc.authority_level),
            })
        return rows
