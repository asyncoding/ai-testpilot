"""E6 tree-sitter Swift 切块器（docs/08 FR-ING-002 AC3 选型复核，S1.6）。

定位与边界：
- tree-sitter / tree-sitter-swift 是 **optional dependency group**（见 adapters
  pyproject `[project.optional-dependencies]`），本模块不在顶层导入语法库，
  缺包时构造器抛 AdapterUnavailableError，由装配层决定显式失败还是回退；
  core 零三方红线不动，词法器 chunk_swift 永远是离线/解析失败降级路径。
- AST 只替换「声明发现 + 体范围」这一步；selector 命名、调用关系、anchor 去歧、
  doc comment 判定全部**复用 core.chunking 的同一实现**，保证双索引对比只隔离
  符号边界这一个变量，URN 口径与词法器产出严格一致（同文件同符号同 anchor）。
- tree-sitter 给的是 UTF-8 字节偏移而 core 辅助函数按字符偏移切片；含中文注释时
  两者不一致，故每个节点先过 byte→char 映射再切片。
- grammar 对个别合法语法（如嵌套类型）可能挂 ERROR 节点但仍恢复出子声明：
  遍历不剪 ERROR 枝（恢复出的声明照常切块），同时统计 error_node_count 作为
  解析健康度证据写进日志/死信，不静默吞。
"""

from __future__ import annotations

from dataclasses import dataclass

from testpilot_core.chunking import (
    RawChunk,
    _extract_calls,
    _has_doc_comment,
    _line_at,
    _selector,
)
from testpilot_core.errors import AdapterUnavailableError

TREE_SITTER_SWIFT_CHUNKER_VERSION = "tree-sitter-swift-1"

# tree-sitter-swift 0.7.x：struct/enum/actor/extension 都归 class_declaration，
# 靠首子节点关键字文本区分；protocol 单独一类。
_TYPE_NODES = frozenset({"class_declaration", "protocol_declaration"})
_FUNC_NODES = frozenset({
    "function_declaration",
    "protocol_function_declaration",
    "init_declaration",
    "deinit_declaration",
    "subscript_declaration",
})
_BODY_NODES = frozenset({
    "function_body", "class_body", "protocol_body", "enum_class_body",
})
_KEYWORD_BY_NODE = {
    "class_declaration": ("class", "struct", "enum", "actor", "extension"),
}


@dataclass(frozen=True)
class TreeSwiftParseInfo:
    """单文件解析健康度（写 trace/死信用，不进 RawChunk 契约）。"""

    error_node_count: int
    declaration_count: int


def build_byte_to_char(source: str) -> dict[int, int]:
    """UTF-8 字节偏移 → Python 字符偏移映射（仅记边界点，O(字符数)）。"""
    mapping: dict[int, int] = {}
    byte = 0
    for char_pos, ch in enumerate(source):
        mapping[byte] = char_pos
        byte += len(ch.encode("utf-8"))
    mapping[byte] = len(source)
    return mapping


def _node_text(node, source: str, b2c: dict[int, int]) -> str:
    # 统一走 byte→char 映射：tree-sitter 只暴露字节偏移，直接切 str 会在中文注释上错位
    return source[b2c[node.start_byte]:b2c[node.end_byte]]


class TreeSwiftChunker:
    """tree-sitter AST 切块器；产出与 core.chunk_swift 同形的 RawChunk 列表。"""

    version = TREE_SITTER_SWIFT_CHUNKER_VERSION

    def __init__(self) -> None:
        try:
            from tree_sitter import Language, Parser
            import tree_sitter_swift
        except ImportError as exc:  # optional group 未装
            raise AdapterUnavailableError(
                "tree-sitter/tree-sitter-swift 未安装：请以 optional dependency "
                "group 安装（uv pip install -e '.[tree-sitter]'），或把 profile 的 "
                "docs.swift_chunker 切回 lexical（默认）"
            ) from exc
        self._parser = Parser(Language(tree_sitter_swift.language()))
        self.parse_info: TreeSwiftParseInfo | None = None

    # ── AST 采集 ─────────────────────────────────────────────────────────────

    def _collect(self, node, source: str, b2c: dict, owner_stack: list[dict],
                 decls: list[dict], errors: list[int]) -> None:
        ntype = node.type
        if ntype == "ERROR":
            errors.append(node.start_byte)
            # 不 return：ERROR 下恢复出的声明仍要切块（grammar 已知嵌套类型行为）

        if ntype in _TYPE_NODES:
            decl = self._type_decl(node, source, b2c)
            if decl is not None:
                decls.append(decl)
                owner_stack.append(decl)
                for child in node.children:
                    self._collect(child, source, b2c, owner_stack, decls, errors)
                owner_stack.pop()
                return
        if ntype in _FUNC_NODES:
            decl = self._func_decl(node, source, b2c, owner_stack)
            if decl is not None:
                decls.append(decl)
            # 函数体内即便有嵌套 func 也不继续采（与词法器 enclosing_type 口径一致：
            # 词法器把嵌套 func 算作外层类型成员，这里在 owner_stack 不变的前提下
            # 继续下探，让嵌套 func 以同 owner 入表）
        for child in node.children:
            self._collect(child, source, b2c, owner_stack, decls, errors)

    def _type_decl(self, node, source: str, b2c: dict) -> dict | None:
        # 只认声明节点的**直接子节点**：class/struct/enum/actor/protocol 的名字是
        # 直接 type_identifier；extension 的名字在直接 user_type 之下。属性里的
        # 类型（@MainActor、@available(...)）挂在 modifiers>attribute>user_type 下，
        # 继承/泛型约束也各自包在 user_type/type_parameters 里，全不会被误取。
        name_node = next(
            (c for c in node.children if c.type == "type_identifier"), None)
        if name_node is None:
            user_type = next(
                (c for c in node.children if c.type == "user_type"), None)
            if user_type is None:
                return None
            name_node = next(
                (c for c in user_type.children if c.type == "type_identifier"), None)
            if name_node is None:
                return None
        start = b2c[node.start_byte]
        end = b2c[node.end_byte]
        body = next((c for c in node.children if c.type in _BODY_NODES), None)
        if body is not None:
            body_open = b2c[body.start_byte]
            body_close = b2c[body.end_byte] - 1  # 与词法器一致：指向 '}' 字符位
            sig_end = body_open
        else:
            body_open = body_close = None
            sig_end = end
        keyword = next(
            (c.text.decode() for c in node.children
             if c.type in ("class", "struct", "enum", "actor", "extension", "protocol")),
            "class",
        )
        return {
            "kind": "type", "keyword": keyword,
            "name": _node_text(name_node, source, b2c),
            "start": start, "end": end, "sig_end": sig_end,
            "body_open": body_open, "body_close": body_close,
        }

    def _func_decl(self, node, source: str, b2c: dict,
                   owner_stack: list[dict]) -> dict | None:
        if node.type == "init_declaration":
            name = "init"
        elif node.type == "deinit_declaration":
            name = "deinit"
        elif node.type == "subscript_declaration":
            name = "subscript"
        else:
            name_node = next(
                (c for c in node.children if c.type == "simple_identifier"), None)
            if name_node is None:
                return None
            name = _node_text(name_node, source, b2c)
        start = b2c[node.start_byte]
        end = b2c[node.end_byte]
        body = next((c for c in node.children if c.type == "function_body"), None)
        if body is not None:
            body_open = b2c[body.start_byte]
            body_close = b2c[body.end_byte] - 1
            sig_end = body_open
        else:
            # protocol 无体声明：签名到节点结束
            body_open = body_close = None
            sig_end = end
        owner = owner_stack[-1] if owner_stack else None
        return {
            "kind": "func", "keyword": "func", "name": name,
            "start": start, "end": end, "sig_end": sig_end,
            "body_open": body_open, "body_close": body_close,
            "owner": owner,
        }

    # ── 对外契约 ─────────────────────────────────────────────────────────────

    def chunk(self, source: str) -> list[RawChunk]:
        raw = source.encode("utf-8")
        tree = self._parser.parse(raw)
        b2c = build_byte_to_char(source)
        decls: list[dict] = []
        errors: list[int] = []
        self._collect(tree.root_node, source, b2c, [], decls, errors)
        decls.sort(key=lambda d: d["start"])
        self.parse_info = TreeSwiftParseInfo(
            error_node_count=len(errors), declaration_count=len(decls))

        types = [d for d in decls if d["kind"] == "type"]

        def enclosing_type(decl: dict) -> dict | None:
            if decl.get("owner") is not None:
                return decl["owner"]
            owner = None
            for t in types:
                if t["body_open"] is not None and t["body_close"] is not None \
                        and t["start"] < decl["start"] < t["body_close"]:
                    if owner is None or t["start"] > owner["start"]:
                        owner = t
            return owner

        # 符号表与 full_id：selector 直接复用 core._selector，保证 URN 同口径
        symbol_table: dict[str, list[str]] = {}
        for decl in decls:
            if decl["kind"] == "type":
                decl["full_id"] = decl["name"]
            else:
                owner = enclosing_type(decl)
                signature = source[decl["start"]: decl["sig_end"]].strip()
                decl["signature"] = signature
                selector = _selector(decl["name"], signature)
                decl["full_id"] = (
                    f"{owner['name']}.{selector}" if owner else selector)
                symbol_table.setdefault(decl["name"], []).append(decl["full_id"])

        # 重名 anchor 去歧：与词法器同规则（#L<起始行>）
        id_counts: dict[str, int] = {}
        for decl in decls:
            id_counts[decl["full_id"]] = id_counts.get(decl["full_id"], 0) + 1
        for decl in decls:
            decl["anchor_id"] = (
                f"{decl['full_id']}#L{_line_at(decl['start'], source)}"
                if id_counts[decl["full_id"]] > 1 else decl["full_id"]
            )

        chunks: list[RawChunk] = []
        for decl in decls:
            end_pos = (decl["body_close"] + 1 if decl["body_close"] is not None
                       else decl["sig_end"])
            text = source[decl["start"]:end_pos].strip() + "\n"
            if not text.strip():
                continue
            if decl["body_open"] is not None:
                signature = source[decl["start"]:decl["body_open"]].strip()
            else:
                signature = decl.get("signature", text.strip().splitlines()[0])
            owner = enclosing_type(decl)
            start_line = _line_at(decl["start"], source)
            last_line = _line_at(max(decl["start"], end_pos - 1), source)
            # 与词法器一致：类型块也抽体内调用（_extract_calls 对无体声明返 []）
            relations = _extract_calls(decl, owner, source, symbol_table)
            chunks.append(RawChunk(
                kind="code_symbol",
                anchor=decl["anchor_id"],
                title=decl["full_id"],
                text=text,
                start_line=start_line,
                end_line=last_line,
                signature=" ".join(signature.split()),
                heading_path=(owner["name"],) if owner else (),
                has_doc_comment=_has_doc_comment(source, decl["start"]),
                relations=tuple(relations),
            ))
        return chunks


def tree_sitter_available() -> bool:
    """探针：optional group 是否已装（profile 校验/UI 状态用，不触发装配异常）。"""
    try:
        import tree_sitter  # noqa: F401
        import tree_sitter_swift  # noqa: F401
    except ImportError:
        return False
    return True
