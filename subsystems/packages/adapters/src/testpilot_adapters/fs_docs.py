"""文件系统知识源适配器（KnowledgeSourcePort）。

生产替代：Confluence/飞书/内部知识库 API（docs/04 §4.1）。
S1 起语料真实指向 AlarmKitSpike 的 docs/，路径走配置，不写死。
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path

from testpilot_core.enums import AuthorityLevel
from testpilot_core.errors import AdapterUnavailableError
from testpilot_core.models import EventCheckpoint, SourceDocument
from testpilot_core.urn import make_urn

_SOURCE_SYSTEM = "fs-docs"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class FileSystemDocsAdapter:
    def __init__(
        self,
        roots: Iterable[str | Path],
        *,
        pattern: str = "**/*.md",
        default_authority: AuthorityLevel = AuthorityLevel.L2_EXPERIENCE,
    ) -> None:
        self.roots = [Path(p) for p in roots]
        self.pattern = pattern
        self.default_authority = default_authority

    def _iter_files(self) -> list[Path]:
        files: list[Path] = []
        for root in self.roots:
            if not root.exists():
                raise AdapterUnavailableError(f"文档根目录不存在: {root}")
            files.extend(sorted(p for p in root.glob(self.pattern) if p.is_file()))
        return files

    def _to_document(self, path: Path) -> SourceDocument:
        text = path.read_text(encoding="utf-8")
        stat = path.stat()
        rel = str(path)
        return SourceDocument(
            urn=make_urn(_SOURCE_SYSTEM, "document", rel, _sha256(text)[:12]),
            source_system=_SOURCE_SYSTEM,
            uri=f"file://{path.resolve()}",
            title=path.stem,
            version=_sha256(text)[:12],
            content_hash=_sha256(text),
            authority_level=self.default_authority,
            mtime=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
            metadata={"relative_path": rel, "size": stat.st_size},
        )

    async def list_documents(self) -> list[SourceDocument]:
        return [self._to_document(p) for p in self._iter_files()]

    async def read_document(self, urn: str) -> SourceDocument:
        from testpilot_core.urn import parse_urn

        _, _, external_id, _ = parse_urn(urn)
        path = Path(external_id)
        if not path.exists():
            raise AdapterUnavailableError(f"文档已不存在: {path}")
        return self._to_document(path)

    async def list_changes(self, checkpoint: EventCheckpoint) -> list:
        """增量：以 checkpoint.last_cursor（上次扫描时间 ISO）为水位，返回变更事件。

        S0 只实现 mtime 水位的最小语义；删除事件在 S1 补（docs/08 FR-CON-002 AC2）。
        """
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
