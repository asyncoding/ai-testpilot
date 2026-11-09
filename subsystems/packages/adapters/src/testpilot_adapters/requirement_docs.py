"""需求 Markdown 文档连接器（docs/08 FR-TD-001，S2.2）。

只读扫描配置目录下的需求 Markdown，调用 core 零依赖抽取器产出结构化
``RequirementParseResult``；与 ``case_docs`` 同构的根别名/排除/隔离语义：

- URN 基于「根别名/相对路径」，不随内容变化，内容变化体现在 content_hash；
- 单文件读取失败隔离为 error，不阻断整批；解析级单条死信在结果的
  ``dead_letters`` 内，与读取级错误分开记账；
- 生产替代：飞书文档/Confluence/Jira 需求（docs/04 §4.1），端口契约保持不变。
"""

from __future__ import annotations

import fnmatch
import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from testpilot_core.errors import AdapterUnavailableError
from testpilot_core.requirement import RequirementParseResult, parse_requirement_markdown
from testpilot_core.urn import make_urn

SOURCE_SYSTEM = "fs-reqs"
_GLOB = "**/*.md"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RequirementRoot:
    path: Path
    alias: str


@dataclass(frozen=True)
class ParsedRequirementDoc:
    """一份需求文档的抽取批次（doc_urn 供整组替换/对账）。"""

    doc_urn: str
    external_id: str  # 根别名/相对路径
    uri: str
    content_hash: str
    result: RequirementParseResult


class RequirementDocumentAdapter:
    def __init__(
        self,
        roots: Iterable[str | Path],
        *,
        excludes: Sequence[str] = (),
        branch: str = "main",
        source_system: str = SOURCE_SYSTEM,
    ) -> None:
        self.roots = self._build_roots(roots)
        self.excludes = list(excludes)
        self.branch = branch
        self.source_system = source_system

    @staticmethod
    def _build_roots(roots: Iterable[str | Path]) -> list[RequirementRoot]:
        specs: list[RequirementRoot] = []
        used: dict[str, int] = {}
        for raw in roots:
            path = Path(raw).expanduser().resolve()
            alias = path.name or "root"
            used[alias] = used.get(alias, 0) + 1
            if used[alias] > 1:
                alias = f"{alias}-{used[alias]}"
            specs.append(RequirementRoot(path=path, alias=alias))
        return specs

    def _is_excluded(self, root: RequirementRoot, rel: str, absolute: str) -> bool:
        candidate = f"{root.alias}/{rel}"
        return any(
            fnmatch.fnmatch(p, pat) for pat in self.excludes
            for p in (rel, candidate, absolute)
        )

    def _iter_files(self) -> list[tuple[RequirementRoot, Path]]:
        found: list[tuple[RequirementRoot, Path]] = []
        for root in self.roots:
            if not root.path.exists():
                raise AdapterUnavailableError(f"需求文档根目录不存在: {root.path}")
            for path in sorted(root.path.glob(_GLOB)):
                if not path.is_file():
                    continue
                rel = path.relative_to(root.path).as_posix()
                if self._is_excluded(root, rel, str(path)):
                    continue
                found.append((root, path))
        return found

    def load(self) -> tuple[list[ParsedRequirementDoc], list[dict]]:
        """扫描并解析全部文档；返回 (抽取批次, 读取级错误)。"""
        docs: list[ParsedRequirementDoc] = []
        errors: list[dict] = []
        for root, path in self._iter_files():
            rel = path.relative_to(root.path).as_posix()
            external_id = f"{root.alias}/{rel}"
            urn = make_urn(self.source_system, "requirement_doc", external_id)
            try:
                text = path.read_text(encoding="utf-8")
            except Exception as exc:  # noqa: BLE001 - 单文件隔离是显式设计
                errors.append({
                    "urn": urn,
                    "uri": f"file://{path.resolve()}",
                    "relative_path": rel,
                    "error": f"{type(exc).__name__}: {exc}",
                })
                continue
            result = parse_requirement_markdown(
                text,
                doc_external_id=external_id,
                source_system=self.source_system,
                version=_sha256(text)[:12],
            )
            docs.append(ParsedRequirementDoc(
                doc_urn=urn,
                external_id=external_id,
                uri=f"file://{path.resolve()}",
                content_hash=_sha256(text),
                result=result,
            ))
        return docs, errors
