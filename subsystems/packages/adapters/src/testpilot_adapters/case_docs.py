"""用例 Markdown 文档连接器（docs/08 FR-CON-003，S2.1）。

只读扫描配置目录下的用例 Markdown，调用 core 零依赖解析器产出结构化
``TestCase``；root 既可是目录（递归 glob）也可是单个文件（精确收录，
alias 取父目录名，external_id 与目录形态同形）；与 ``fs_docs`` 同构的
根别名/排除/隔离语义：

- URN 基于「根别名/相对路径」，不随内容变化，内容变化体现在 content_hash；
- 单文件读取失败隔离为 error，不阻断整批（AC2 的连接器侧一半，解析侧的
  单条死信由 core 解析器产出）；
- 生产替代：TestRail/飞书文档/Git Wiki（docs/04 §4.1），端口契约保持不变。
"""

from __future__ import annotations

import fnmatch
import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from testpilot_core.errors import AdapterUnavailableError
from testpilot_core.testcase import CaseParseResult, parse_case_markdown
from testpilot_core.urn import make_urn

SOURCE_SYSTEM = "fs-cases"
_GLOB = "**/*.md"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CaseRoot:
    path: Path
    alias: str


@dataclass(frozen=True)
class ParsedCaseDoc:
    """一份用例文档的解析批次（doc_urn 供整组替换/对账）。"""

    doc_urn: str
    external_id: str  # 根别名/相对路径
    uri: str
    content_hash: str
    result: CaseParseResult
    # docs/17 M1：lint 探针的原文规则（缺冒号/段结构/表格识别）需要 raw 文本；
    # 非持久化字段，不参与任何 URN/hash 计算，老装配方按关键字构造不受影响
    text: str = ""


class CaseDocumentAdapter:
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
    def _build_roots(roots: Iterable[str | Path]) -> list[CaseRoot]:
        specs: list[CaseRoot] = []
        used: dict[str, int] = {}
        for raw in roots:
            path = Path(raw).expanduser().resolve()
            # 单文件 root：alias 取父目录名，使 external_id 与目录 root 扫到
            # 同一文件时完全同形（docs/x.md），两种配法 URN 稳定可互换
            alias_base = path.parent.name if path.is_file() else (path.name or "root")
            used[alias_base] = used.get(alias_base, 0) + 1
            alias = alias_base if used[alias_base] == 1 else f"{alias_base}-{used[alias_base]}"
            specs.append(CaseRoot(path=path, alias=alias))
        return specs

    def _is_excluded(self, root: CaseRoot, rel: str, absolute: str) -> bool:
        candidate = f"{root.alias}/{rel}"
        return any(
            fnmatch.fnmatch(p, pat) for pat in self.excludes
            for p in (rel, candidate, absolute)
        )

    def _iter_files(self) -> list[tuple[CaseRoot, Path]]:
        found: list[tuple[CaseRoot, Path]] = []
        for root in self.roots:
            if not root.path.exists():
                raise AdapterUnavailableError(f"用例文档根目录不存在: {root.path}")
            if root.path.is_file():
                # 单文件 root：精确收录该文件，不 glob、不应用 excludes
                # （显式点名即最高优先级，同目录其他文档不扫入）
                found.append((root, root.path))
                continue
            for path in sorted(root.path.glob(_GLOB)):
                if not path.is_file():
                    continue
                rel = path.relative_to(root.path).as_posix()
                if self._is_excluded(root, rel, str(path)):
                    continue
                found.append((root, path))
        return found

    def load(self) -> tuple[list[ParsedCaseDoc], list[dict]]:
        """扫描并解析全部文档；返回 (解析批次, 读取级错误)。

        解析级单条死信在每个 ``ParsedCaseDoc.result.dead_letters`` 内，
        与读取级错误分开记账——前者是文档内容问题，后者是环境/IO 问题。
        """
        docs: list[ParsedCaseDoc] = []
        errors: list[dict] = []
        for root, path in self._iter_files():
            rel = root.path.name if root.path.is_file() \
                else path.relative_to(root.path).as_posix()
            external_id = f"{root.alias}/{rel}"
            urn = make_urn(self.source_system, "document", external_id)
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
            result = parse_case_markdown(
                text,
                doc_external_id=external_id,
                source_system=self.source_system,
                version=_sha256(text)[:12],
            )
            docs.append(ParsedCaseDoc(
                doc_urn=urn,
                external_id=external_id,
                uri=f"file://{path.resolve()}",
                content_hash=_sha256(text),
                result=result,
                text=text,
            ))
        return docs, errors
