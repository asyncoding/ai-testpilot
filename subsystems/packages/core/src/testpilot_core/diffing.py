"""符号集对账纯逻辑（docs/08 FR-CON-001 AC1/AC2）。

输入同一文件在 base/head 两个版本上的词法切块结果（chunk_swift 产出），
输出 SymbolChange 列表。零 IO、零三方依赖，import-lint 守门。

身份键的关键约束：chunk_swift 对同文件重名 full_id（struct X + extension X、
重载 selector 撞名）会追加 ``#L<行号>`` 去歧，而行号随版本漂移——直接按 anchor
字符串对账会把所有去歧符号误判成 removed+added。因此身份键是
「去掉 #L 后缀的 full_id + 声明序号（RawChunk 顺序即声明顺序）」。
"""

from __future__ import annotations

from .chunking import RawChunk, mask_swift_comments
from .enums import ChangeType
from .models import SymbolChange


def _base_id(chunk: RawChunk) -> str:
    """anchor → 跨版本稳定身份（剥 #L 行号后缀）。"""
    return chunk.anchor.split("#L", 1)[0]


def _code_identity(text: str) -> str:
    """注释掩码 + 空白归一后的代码身份串（D3）。

    纯注释改动（含 ``///`` doc 注释、``MARK``）与纯格式改动（缩进/空行）归一后
    身份不变，不再误报 MODIFIED；字符串字面量不在掩码范围，改文案/key/URL 仍报。
    """
    return " ".join(mask_swift_comments(text).split())


def _index_by_identity(chunks: list[RawChunk]) -> dict[tuple[str, int], RawChunk]:
    """按 (base_id, 第 N 次声明) 建索引；只收代码符号块。"""
    counts: dict[str, int] = {}
    indexed: dict[tuple[str, int], RawChunk] = {}
    for chunk in chunks:
        if chunk.kind != "code_symbol":
            continue
        base = _base_id(chunk)
        ordinal = counts.get(base, 0)
        counts[base] = ordinal + 1
        indexed[(base, ordinal)] = chunk
    return indexed


def diff_symbol_sets(old: list[RawChunk], new: list[RawChunk]) -> list[SymbolChange]:
    """两版本符号块对账。

    判定优先级：消失=removed / 新增=added → 签名变化=signature_changed
    （文本通常也变，不再重复报 modified）→ 仅代码文本变化=modified → 无变化不报。
    文本比对走 :func:`_code_identity`：注释（含 MARK/doc 注释）与空白差异先归一，
    纯注释/纯格式改动不报 modified（D3），字符串字面量差异保留。
    输出顺序稳定：先按 old 声明序报 removed/变化，再按 new 声明序报 added；
    同一 (symbol_id, change_type) 去重。
    """
    old_map = _index_by_identity(old)
    new_map = _index_by_identity(new)

    changes: list[SymbolChange] = []
    seen: set[tuple[str, ChangeType]] = set()

    def add(symbol_id: str, change_type: ChangeType) -> None:
        key = (symbol_id, change_type)
        if key not in seen:
            seen.add(key)
            changes.append(SymbolChange(symbol_id=symbol_id, change_type=change_type))

    # old 声明序：removed / signature_changed / modified
    for key, old_chunk in old_map.items():
        symbol_id = key[0]
        new_chunk = new_map.get(key)
        if new_chunk is None:
            add(symbol_id, ChangeType.REMOVED)
        elif old_chunk.signature != new_chunk.signature:
            add(symbol_id, ChangeType.SIGNATURE_CHANGED)
        elif _code_identity(old_chunk.text) != _code_identity(new_chunk.text):
            add(symbol_id, ChangeType.MODIFIED)

    # new 声明序：added
    for key, new_chunk in new_map.items():
        if key not in old_map:
            add(key[0], ChangeType.ADDED)

    return changes
