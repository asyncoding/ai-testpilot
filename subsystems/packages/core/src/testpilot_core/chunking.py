"""知识切块：纯函数、零 IO、零三方依赖（core 防腐层，import-lint 守门）。

- Markdown：按 ATX 标题层级切章节，anchor 为标题 slug，保留标题路径；
  超长章节按段落兜底切分，避免单个 chunk 稀释检索。
- Swift：词法扫描（注释/字符串屏蔽 + 大括号配平）做"最简符号切"——
  class/struct/enum/actor/protocol/extension + func，符号带所属类型、行范围、
  签名、doc comment 标记与同文件调用关系。生产侧可用 SwiftSyntax 替换本模块，
  产出契约（RawChunk）不变（docs/08 FR-ING-002）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ── 切块器版本位 ────────────────────────────────────────────────────────────
# 写入 chunk metadata；live-verify 跨版本比对 hash 时先比对版本位：
# 只要 RawChunk.text 的生成口径发生变化（哪怕一行），必须 bump 对应常量，
# 否则旧索引的 content_hash 会被误判成"代码变了"（FR-QA-002 防误报）。
SWIFT_CHUNKER_VERSION = "lexical-swift-1"
MARKDOWN_CHUNKER_VERSION = "heading-md-1"

# ── 通用产出契约 ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CallRelation:
    """同文件内最简调用关系（FR-ING-002 AC2）。"""

    target_symbol: str
    evidence: str  # 命中的源码行（去空白），可点开证据


@dataclass(frozen=True)
class RawChunk:
    kind: str  # text | code_symbol
    anchor: str  # 文档内稳定锚点：heading slug 或 符号 ID
    title: str
    text: str
    start_line: int  # 1-based
    end_line: int
    signature: str = ""
    heading_path: tuple[str, ...] = ()
    has_doc_comment: bool = False
    relations: tuple[CallRelation, ...] = ()


def select_chunk_by_anchor(chunks: list[RawChunk], anchor: str) -> RawChunk | None:
    """按 anchor 在重切结果里取回同一个块（live-verify / git 快照共用口径）。

    1) 精确命中（含 chunk_swift 的 `#L<行号>` 去歧 anchor）；
    2) 退化为去 #L 身份匹配：多个同名声明时按 #L 行号就近选，否则取首个；
    3) 都没有 → None（符号在目标版本不存在）。
    行号在版本间漂移，所以 #L 只做同身份内的就近提示，绝不参与身份判定。
    """
    for c in chunks:
        if c.anchor == anchor:
            return c
    base = anchor.split("#L", 1)[0]
    candidates = [c for c in chunks if c.anchor.split("#L", 1)[0] == base]
    if not candidates:
        return None
    line_marker = anchor.split("#L", 1)[1] if "#L" in anchor else ""
    if line_marker.isdigit():
        target_line = int(line_marker)
        return min(candidates, key=lambda c: abs(c.start_line - target_line))
    return candidates[0]


# ── Markdown ───────────────────────────────────────────────────────────────

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")
_SLUG_STRIP_RE = re.compile(r"[^\w一-鿿]+", re.UNICODE)


def _slug(text: str) -> str:
    slug = _SLUG_STRIP_RE.sub("-", text.lower()).strip("-")
    return slug or "section"


def _split_long(body: str, anchor: str, max_chars: int) -> list[tuple[str, str, int]]:
    """超长章节按段落（空行）贪心打包；返回 (anchor, text, 相对章节首行的行偏移)。"""
    if len(body) <= max_chars:
        return [(anchor, body, 0)]
    # 用空行正则切出段落及其在 body 中的起始位置，保证行号可复现
    paras: list[tuple[int, str]] = []
    start = 0
    for m in re.finditer(r"\n[ \t]*\n", body):
        paras.append((start, body[start : m.start()]))
        start = m.end()
    paras.append((start, body[start:]))

    parts: list[tuple[str, str, int]] = []
    seg_starts: list[int] = []
    seg_texts: list[str] = []
    seg_len = 0

    def flush() -> None:
        if not seg_texts:
            return
        # 首块沿用章节 anchor（即使单段超长也如此），后续块才编号
        sub_anchor = anchor if not parts else f"{anchor}__p{len(parts) + 1}"
        parts.append((sub_anchor, "\n\n".join(seg_texts),
                      body.count("\n", 0, seg_starts[0])))

    for pos, para in paras:
        plen = len(para) + (2 if seg_texts else 0)
        if seg_texts and seg_len + plen > max_chars:
            flush()
            seg_starts, seg_texts, seg_len = [], [], 0
        if not seg_texts:
            seg_starts.append(pos)
        seg_texts.append(para)
        seg_len += plen
    flush()
    return parts


def chunk_markdown(text: str, *, max_chars: int = 1600) -> list[RawChunk]:
    """按标题切章节。代码围栏内的 # 不当标题。"""
    lines = text.splitlines()
    sections: list[dict] = []  # {level,title,path,lines:[],start_line}
    stack: list[tuple[int, str]] = []
    current: dict | None = None

    in_fence = False
    for lineno, line in enumerate(lines, start=1):
        fence = _FENCE_RE.match(line)
        if fence:
            in_fence = not in_fence
        m = None if in_fence else _HEADING_RE.match(line)
        if m:
            if current is not None:
                sections.append(current)
            level = len(m.group(1))
            title = m.group(2).strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            # content 只收正文行，不含任何标题行；标题经 levels/path 在成块时整体前缀化
            current = {
                "level": level,
                "title": title,
                "path": tuple(t for _, t in stack),
                "levels": tuple(l for l, _ in stack),
                "content": [],
                "start_line": lineno,
            }
        else:
            if current is None:
                # 标题前的序言
                current = {"level": 0, "title": "", "path": (), "levels": (),
                           "content": [], "start_line": lineno}
            current["content"].append(line)
    if current is not None:
        sections.append(current)

    chunks: list[RawChunk] = []
    seen_anchors: dict[str, int] = {}
    for sec in sections:
        content = "\n".join(sec["content"]).strip("\n")
        # 标题独占（如 H1 后直接跟 H2）不单独成块
        if not content.strip():
            continue
        base_anchor = _slug(sec["title"]) if sec["title"] else "preamble"
        seen_anchors[base_anchor] = seen_anchors.get(base_anchor, 0) + 1
        anchor = base_anchor if seen_anchors[base_anchor] == 1 else f"{base_anchor}-{seen_anchors[base_anchor]}"
        title = " / ".join(sec["path"]) if sec["path"] else (sec["title"] or "序言")
        # 完整标题路径作为前缀进正文：每个块自描述（检索结果自带上下文），且保证 H1 紧贴
        # H2 等"独占标题"不会从可检索正文丢失——FTS 只索引 text，标题不进 text 就永远
        # 召不回（2026-11-03 recall_probe 实测：事故文档标题词 0 命中）。
        prefix = "\n".join(
            f"{'#' * lvl} {t}" for lvl, t in zip(sec["levels"], sec["path"])
        )
        for sub_anchor, part_text, line_off in _split_long(content, anchor, max_chars):
            text = (f"{prefix}\n{part_text.strip()}\n" if prefix else part_text.strip() + "\n")
            # 行范围映射回原文件：有序言的章节首块从标题行起；序言块从自身首行起
            if sec["level"]:
                start = sec["start_line"] if line_off == 0 else sec["start_line"] + 1 + line_off
                end = sec["start_line"] + 1 + line_off + part_text.count("\n")
            else:
                start = sec["start_line"] + line_off
                end = start + part_text.count("\n")
            chunks.append(
                RawChunk(
                    kind="text",
                    anchor=sub_anchor,
                    title=title,
                    text=text,
                    start_line=start,
                    end_line=end,
                    heading_path=sec["path"],
                )
            )
    return chunks


# ── Swift 最简符号切 ─────────────────────────────────────────────────────────

_LEAD_MODIFIERS = (
    r"(?:@[A-Za-z_]\w*(?:\([^)]*\))?\s*|"
    r"(?:public|open|internal|fileprivate|private|final|indirect|static|class|override|"
    r"nonisolated|isolated|consuming|borrowing|mutating|nonmutating|convenience|required|"
    r"prefix|postfix|infix|weak|lazy|unowned)\s+)*"
)
_TYPE_RE = re.compile(
    r"(?m)^[ \t]*" + _LEAD_MODIFIERS +
    r"(class|struct|enum|actor|protocol|extension)\s+([A-Za-z_]\w*)"
)
_FUNC_RE = re.compile(
    r"(?m)^[ \t]*" + _LEAD_MODIFIERS +
    r"func\s+([A-Za-z_]\w*)"
)
_DOC_LINE_RE = re.compile(r"^\s*///")
_CALL_KEYWORDS = {
    "if", "guard", "else", "while", "for", "repeat", "switch", "return", "print", "NSLog",
    "assert", "assertionFailure", "fatalError", "await", "try", "throw", "throws", "catch",
    "let", "var", "func", "self", "super", "init", "deinit", "Any", "some", "Task",
    "XCTAssert", "XCTAssertEqual", "XCTAssertTrue", "XCTAssertFalse", "XCTAssertNotNil",
}
_EXPLICIT_CALL_RE = re.compile(r"(?:self|([A-Z][A-Za-z0-9_]*))\.([A-Za-z_]\w*)\s*\(")
_BARE_CALL_RE = re.compile(r"(?<![A-Za-z0-9_.])([A-Za-z_]\w*)\s*\(")


def _mask_swift(source: str) -> str:
    """把注释与字符串内容替换为等长空白（保留换行），供大括号配平与声明匹配。"""
    out = list(source)
    i, n = 0, len(source)
    while i < n:
        c = source[i]
        nxt = source[i + 1] if i + 1 < n else ""
        if c == "/" and nxt == "/":
            j = source.find("\n", i)
            j = n if j == -1 else j
            for k in range(i, j):
                out[k] = " "
            i = j
        elif c == "/" and nxt == "*":
            depth = 1
            out[i] = out[i + 1] = " "
            i += 2
            while i < n and depth:
                if source[i] == "/" and i + 1 < n and source[i + 1] == "*":
                    depth += 1
                    out[i] = out[i + 1] = " "
                    i += 2
                elif source[i] == "*" and i + 1 < n and source[i + 1] == "/":
                    depth -= 1
                    out[i] = out[i + 1] = " "
                    i += 2
                else:
                    if source[i] != "\n":
                        out[i] = " "
                    i += 1
        elif c == '"':
            # 单行字符串与多行字符串统一处理：扫到下一个未转义引号
            out[i] = " "
            i += 1
            while i < n:
                if source[i] == "\\" and i + 1 < n:
                    if source[i] != "\n":
                        out[i] = " "
                    if source[i + 1] != "\n":
                        out[i + 1] = " "
                    i += 2
                    continue
                if source[i] == '"':
                    out[i] = " "
                    i += 1
                    break
                if source[i] != "\n":
                    out[i] = " "
                i += 1
        else:
            i += 1
    return "".join(out)


def _line_at(pos: int, source: str) -> int:
    return source.count("\n", 0, pos) + 1


def _match_body(masked: str, open_start: int) -> tuple[int, int] | None:
    """从 open_start（'{' 位置）配平，返回 (open, close)；配不平返回 None。"""
    depth = 0
    for i in range(open_start, len(masked)):
        ch = masked[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return (open_start, i)
    return None


def _find_span(masked: str, decl_end: int, next_decl: int) -> tuple[int, int | None, int | None]:
    """找声明体范围。返回 (signature_end_exclusive, body_open, body_close)。

    声明后在到下一声明之间扫描：遇到 '{' 则配平取函数体；
    遇到 ';' 或签名后实质内容结束则为无体声明（protocol 要求）。
    """
    i = decl_end
    paren = angle = 0
    while i < len(masked) and i < next_decl:
        ch = masked[i]
        if ch == "(":
            paren += 1
        elif ch == ")":
            paren = max(0, paren - 1)
        elif ch == "<":
            angle += 1
        elif ch == ">":
            angle = max(0, angle - 1)
        elif paren == 0 and angle == 0:
            if ch == "{":
                matched = _match_body(masked, i)
                if matched:
                    return (i, matched[0], matched[1])
                return (i, None, None)
            if ch == ";":
                return (i + 1, None, None)
            if ch == "\n":
                # protocol 中的无体声明：签名行结束且下一行不是续行（以 @/修饰符 开头算新声明）
                line_end = i
                rest = masked[line_end + 1 : next_decl].lstrip(" \t")
                if rest.startswith(("\n", "@")) or not rest.strip():
                    return (line_end, None, None)
        i += 1
    return (i, None, None)


def _has_doc_comment(source: str, decl_start: int) -> bool:
    line_start = source.rfind("\n", 0, decl_start) + 1
    prefix = source[:line_start]
    above = prefix.splitlines()
    contiguous = 0
    for line in reversed(above):
        if _DOC_LINE_RE.match(line):
            contiguous += 1
        elif line.strip() == "":
            if contiguous:
                break
        elif line.strip().endswith("*/"):
            return True
        else:
            break
    return contiguous > 0


def _selector(name: str, signature: str) -> str:
    """从签名文本提取 Swift selector 风格参数标签，如 schedulePrimary(notification:at:)。"""
    open_paren = signature.find("(")
    close_paren = signature.rfind(")")
    if open_paren == -1 or close_paren == -1 or close_paren <= open_paren:
        return f"{name}()"
    raw = signature[open_paren + 1 : close_paren]
    labels: list[str] = []
    depth = 0
    current = ""
    parts: list[str] = []
    for ch in raw:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += ch
    if current.strip():
        parts.append(current)
    for part in parts:
        # Swift 参数：`label:`、`external internal:`、`_ internal:`；取外部标签
        m = re.match(
            r"\s*(?:([A-Za-z_]\w*|_)\s+)?([A-Za-z_]\w*)\s*:", part.strip()
        )
        if not m:
            continue
        label = m.group(1) or m.group(2)
        labels.append("_" if label == "_" else label)
    if not labels:
        return f"{name}()"
    return f"{name}({':'.join(labels) + ':'})"


def chunk_swift(source: str) -> list[RawChunk]:
    """Swift 最简符号切块；返回类型块与方法块（内容有重叠，符号可独立召回）。"""
    masked = _mask_swift(source)

    # 1) 收集全部声明位置
    decls: list[dict] = []
    for m in _TYPE_RE.finditer(masked):
        decls.append({"kind": "type", "keyword": m.group(1), "name": m.group(2),
                      "start": m.start(), "end": m.end()})
    for m in _FUNC_RE.finditer(masked):
        decls.append({"kind": "func", "keyword": "func", "name": m.group(1),
                      "start": m.start(), "end": m.end()})
    decls.sort(key=lambda d: d["start"])

    # 2) 求每个声明的体范围
    for idx, decl in enumerate(decls):
        next_decl = decls[idx + 1]["start"] if idx + 1 < len(decls) else len(masked)
        sig_end, body_open, body_close = _find_span(masked, decl["end"], next_decl)
        decl["sig_end"] = sig_end
        decl["body_open"] = body_open
        decl["body_close"] = body_close

    # 3) 类型包容关系：func 落在哪个类型体内
    types = [d for d in decls if d["kind"] == "type"]

    def enclosing_type(decl: dict) -> dict | None:
        owner = None
        for t in types:
            if t["body_open"] is not None and t["body_close"] is not None \
                    and t["start"] < decl["start"] < t["body_close"]:
                if owner is None or t["start"] > owner["start"]:
                    owner = t
        return owner

    # 符号表：base name -> 全限定 ID
    symbol_table: dict[str, list[str]] = {}
    for decl in decls:
        if decl["kind"] == "type":
            decl["full_id"] = decl["name"]
        else:
            owner = enclosing_type(decl)
            signature = source[decl["start"] : decl["sig_end"]].strip()
            selector = _selector(decl["name"], signature)
            decl["full_id"] = f"{owner['name']}.{selector}" if owner else selector
            symbol_table.setdefault(decl["name"], []).append(decl["full_id"])

    # Swift 允许同文件 struct X + extension X，方法 selector 也可重载撞名；
    # 重名符号的稳定 anchor 追加 #L<起始行> 去歧（正常符号保持干净 ID）。
    id_counts: dict[str, int] = {}
    for decl in decls:
        fid = decl["full_id"]
        id_counts[fid] = id_counts.get(fid, 0) + 1
    for decl in decls:
        decl["anchor_id"] = (
            f"{decl['full_id']}#L{_line_at(decl['start'], source)}"
            if id_counts[decl["full_id"]] > 1 else decl["full_id"]
        )

    # 4) 切块
    chunks: list[RawChunk] = []
    for decl in decls:
        end_pos = decl["body_close"] + 1 if decl["body_close"] is not None else decl["sig_end"]
        text = source[decl["start"] : end_pos].strip() + "\n"
        if not text.strip():
            continue
        signature = source[decl["start"] : decl["body_open"]].strip() \
            if decl["body_open"] is not None else text.strip().splitlines()[0]
        start_line, last_line = _line_at(decl["start"], source), _line_at(max(decl["start"], end_pos - 1), source)
        owner = enclosing_type(decl)
        title = decl["full_id"]
        relations = _extract_calls(decl, owner, source, symbol_table)
        chunks.append(
            RawChunk(
                kind="code_symbol",
                anchor=decl["anchor_id"],
                title=title,
                text=text,
                start_line=start_line,
                end_line=last_line,
                signature=" ".join(signature.split()),
                heading_path=(owner["name"],) if owner else (),
                has_doc_comment=_has_doc_comment(source, decl["start"]),
                relations=tuple(relations),
            )
        )
    return chunks


_FUNC_DECL_RE = re.compile(r"\bfunc\s+([A-Za-z_]\w*)\s*(?:<[^>]*>)?\s*\(")


def _mask_func_decls(body: str) -> str:
    """把嵌套的 `func foo(` 声明头抹成空白，避免声明被当成对 foo() 的调用。"""
    def _blank(m: re.Match) -> str:
        return " " * (len(m.group(0)) - 1) + "("

    return _FUNC_DECL_RE.sub(_blank, body)


def _extract_calls(
    decl: dict,
    owner: dict | None,
    source: str,
    symbol_table: dict[str, list[str]],
) -> list[CallRelation]:
    if decl["body_open"] is None:
        return []
    raw_body = source[decl["body_open"] : (decl["body_close"] or len(source))]
    body = _mask_func_decls(raw_body)
    evidence_lines = raw_body.splitlines()

    def line_text(pos_in_body: int) -> str:
        line_idx = body.count("\n", 0, pos_in_body)
        return evidence_lines[line_idx].strip() if line_idx < len(evidence_lines) else ""

    found: dict[str, str] = {}

    def add(candidate_id: str, evidence: str) -> None:
        if candidate_id != decl["full_id"] and candidate_id not in found:
            found[candidate_id] = evidence

    # 显式 self.foo() / Type.foo()
    for m in _EXPLICIT_CALL_RE.finditer(body):
        type_name, method = m.group(1), m.group(2)
        evidence = line_text(m.start())
        if type_name:
            for full_id in symbol_table.get(method, []):
                if full_id.startswith(f"{type_name}."):
                    add(full_id, evidence)
        else:  # self.
            same_type = [f for f in symbol_table.get(method, [])
                         if owner and f.startswith(f"{owner['name']}.")]
            for full_id in same_type or symbol_table.get(method, []):
                add(full_id, evidence)

    # 裸名调用：优先同类型方法，其次文件内任意符号
    for m in _BARE_CALL_RE.finditer(body):
        name = m.group(1)
        if name in _CALL_KEYWORDS or name not in symbol_table:
            continue
        evidence = line_text(m.start())
        candidates = symbol_table[name]
        same_type = [f for f in candidates if owner and f.startswith(f"{owner['name']}.")]
        add((same_type or candidates)[0], evidence)

    return [CallRelation(target_symbol=tid, evidence=ev) for tid, ev in found.items()]
