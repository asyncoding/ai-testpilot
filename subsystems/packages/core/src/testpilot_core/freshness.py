"""重构感知纯逻辑（docs/08 FR-FRS-002，S4.2）。

零 IO、零三方依赖（import-lint 守门）：符号 id 切分、selector 相似度、
removed/added 符号的 rename 配对、高价值判定。消费 git 适配器产出的
ChangeSet（symbol_changes + file_renames），不接触存储与进程。

配对纪律：一个旧符号最多配一个新符号（一对一贪心，按置信度降序），
配不上的 removed/added 保持原语义交给调用方标 STALE/新增，绝不硬凑。
"""

from __future__ import annotations

import re

from .enums import ChangeType
from .models import FileRename, SymbolChange, SymbolRename

# selector token Jaccard 达到该阈值才认可名内重命名（低于则视为删旧+增新两个符号）
RENAME_SIMILARITY_THRESHOLD = 0.5
# 置信度口径：纯文件移动且锚点不变最可信；其余按相似度折算
_CONF_FILE_MOVE = 0.95
_CONF_SIM_BASE = 0.5
_CONF_SIM_SCALE = 0.5

_REASON_FILE_MOVE = "file_move"
_REASON_SELECTOR = "selector_rename"
_REASON_BOTH = "file_move_and_selector_rename"

_NON_ALNUM = re.compile(r"[^0-9a-zA-Z]+")
_CAMEL_BOUNDARY = re.compile(r"(?<!^)(?=[A-Z])")


def split_symbol_id(symbol_id: str) -> tuple[str, str]:
    """``path#anchor`` → (path, anchor)；无 # 时 path 为空（防御，不抛）。"""
    path, sep, anchor = symbol_id.partition("#")
    return (path, anchor) if sep else ("", symbol_id)


def tokenize_selector(name: str) -> tuple[str, ...]:
    """Swift selector/符号名 → 小写 token 元组（camelCase 与蛇形都切）。

    ``schedulePrimaryV2`` → ("schedule", "primary", "v2")；
    ``kill_backup-interval`` → ("kill", "backup", "interval")。
    """
    spaced = _CAMEL_BOUNDARY.sub(" ", name)
    tokens: list[str] = []
    for part in _NON_ALNUM.sub(" ", spaced).split():
        tokens.extend(t.lower() for t in part.split() if t)
    return tuple(tokens)


def token_jaccard(left: str, right: str) -> float:
    """两个符号名的 token 集合 Jaccard；任一为空集返回 0.0（零除不造 1）。"""
    a = set(tokenize_selector(left))
    b = set(tokenize_selector(right))
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _candidate(
    old_path: str, old_anchor: str, new_path: str, new_anchor: str,
    file_rename_map: dict[str, str],
) -> tuple[float, str] | None:
    file_moved = file_rename_map.get(old_path) == new_path
    similarity = token_jaccard(old_anchor, new_anchor)
    if file_moved and old_anchor == new_anchor:
        return _CONF_FILE_MOVE, _REASON_FILE_MOVE
    if similarity < RENAME_SIMILARITY_THRESHOLD:
        return None
    if file_moved:
        return _CONF_SIM_BASE + _CONF_SIM_SCALE * similarity, _REASON_BOTH
    if old_path == new_path:
        return _CONF_SIM_BASE + _CONF_SIM_SCALE * similarity, _REASON_SELECTOR
    return None  # 跨文件又无 git rename 背书：不配对，防臆测


def pair_symbol_renames(
    symbol_changes: list[SymbolChange],
    file_renames: list[FileRename] | None = None,
) -> list[SymbolRename]:
    """把 removed/added 符号配对成 rename 链；输入无序，输出按旧 id 排序稳定。"""
    file_rename_map = {r.old_path: r.new_path for r in (file_renames or [])}
    removed = [c for c in symbol_changes if c.change_type == ChangeType.REMOVED]
    added = [c for c in symbol_changes if c.change_type == ChangeType.ADDED]

    candidates: list[tuple[float, int, int, str]] = []
    for i, old_change in enumerate(removed):
        old_path, old_anchor = split_symbol_id(old_change.symbol_id)
        for j, new_change in enumerate(added):
            new_path, new_anchor = split_symbol_id(new_change.symbol_id)
            verdict = _candidate(
                old_path, old_anchor, new_path, new_anchor, file_rename_map
            )
            if verdict is not None:
                confidence, reason = verdict
                candidates.append((confidence, i, j, reason))

    # 置信度降序，平手按声明序（i 再 j），保证确定性
    candidates.sort(key=lambda item: (-item[0], item[1], item[2]))
    paired_old: set[int] = set()
    paired_new: set[int] = set()
    renames: list[SymbolRename] = []
    for confidence, i, j, reason in candidates:
        if i in paired_old or j in paired_new:
            continue
        paired_old.add(i)
        paired_new.add(j)
        renames.append(SymbolRename(
            old_symbol_id=removed[i].symbol_id,
            new_symbol_id=added[j].symbol_id,
            confidence=round(confidence, 3),
            reason=reason,
        ))
    renames.sort(key=lambda r: r.old_symbol_id)
    return renames


def is_high_value(
    *,
    quality_score: float,
    reference_count: int,
    quality_threshold: float,
    reference_threshold: int,
) -> bool:
    """高价值条目：内在质量过硬 **或** 被多处引用（满足其一即立即 reindex）。

    两个阈值都由 profile freshness 段注入，纯函数不读配置，便于 hostless 测试。
    """
    return (
        quality_score >= quality_threshold
        or reference_count >= reference_threshold
    )
