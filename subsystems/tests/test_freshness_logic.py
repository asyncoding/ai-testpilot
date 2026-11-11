"""重构感知纯逻辑 hostless 测试（FR-FRS-002，core/freshness.py）。

零 IO、零三方：只验符号 id 切分、selector token 相似度、rename 一对一贪心
配对与高价值判定。存储/事件编排见 tests/test_freshness_service.py。
"""

from __future__ import annotations

import pytest

from testpilot_core.enums import ChangeType
from testpilot_core.freshness import (
    RENAME_SIMILARITY_THRESHOLD,
    is_high_value,
    pair_symbol_renames,
    split_symbol_id,
    token_jaccard,
    tokenize_selector,
)
from testpilot_core.models import FileRename, SymbolChange


def _change(symbol_id: str, change_type: ChangeType) -> SymbolChange:
    return SymbolChange(symbol_id=symbol_id, change_type=change_type)


def _removed(symbol_id: str) -> SymbolChange:
    return _change(symbol_id, ChangeType.REMOVED)


def _added(symbol_id: str) -> SymbolChange:
    return _change(symbol_id, ChangeType.ADDED)


# ── id 切分 / token 化 ──────────────────────────────────────────────────────

def test_split_symbol_id_with_and_without_hash():
    assert split_symbol_id("Sources/A.swift#C.foo()") == (
        "Sources/A.swift", "C.foo()",
    )
    # 防御：无 # 不抛，path 留空
    assert split_symbol_id("C.foo()") == ("", "C.foo()")


def test_tokenize_selector_camel_and_snake():
    assert tokenize_selector("schedulePrimaryV2") == (
        "schedule", "primary", "v2",
    )
    assert tokenize_selector("kill_backup-interval") == (
        "kill", "backup", "interval",
    )


def test_token_jaccard_zero_division_returns_zero():
    assert token_jaccard("()", "[]") == 0.0
    assert token_jaccard("foo", "foo") == 1.0
    # schedule/primary 二词对三词：交 2 并 3
    assert token_jaccard("schedulePrimary", "schedulePrimaryV2") == pytest.approx(2 / 3)


# ── rename 配对 ─────────────────────────────────────────────────────────────

def test_same_file_selector_rename_pairs_with_confidence():
    changes = [
        _removed("Sources/A.swift#C.schedulePrimary()"),
        _added("Sources/A.swift#C.schedulePrimaryV2()"),
    ]
    renames = pair_symbol_renames(changes)
    assert len(renames) == 1
    rename = renames[0]
    assert rename.reason == "selector_rename"
    assert rename.old_symbol_id.endswith("schedulePrimary()")
    assert rename.new_symbol_id.endswith("schedulePrimaryV2()")
    assert RENAME_SIMILARITY_THRESHOLD <= rename.confidence < 1.0
    assert 0.0 <= rename.confidence <= 1.0  # pydantic 边界内


def test_file_move_same_anchor_is_file_move_confidence():
    changes = [
        _removed("Sources/Old.swift#C.keep()"),
        _added("Sources/New.swift#C.keep()"),
    ]
    renames = pair_symbol_renames(changes, [
        FileRename(old_path="Sources/Old.swift", new_path="Sources/New.swift",
                   similarity=100),
    ])
    assert len(renames) == 1
    assert renames[0].reason == "file_move"
    assert renames[0].confidence == 0.95


def test_file_move_plus_selector_change_is_both_reason():
    changes = [
        _removed("Sources/Old.swift#C.schedulePrimary()"),
        _added("Sources/New.swift#C.schedulePrimaryV2()"),
    ]
    renames = pair_symbol_renames(changes, [
        FileRename(old_path="Sources/Old.swift", new_path="Sources/New.swift",
                   similarity=80),
    ])
    assert len(renames) == 1
    assert renames[0].reason == "file_move_and_selector_rename"


def test_dissimilar_names_never_pair_even_with_file_rename():
    changes = [
        _removed("Sources/Old.swift#C.foo()"),
        _added("Sources/New.swift#C.barBaz()"),
    ]
    renames = pair_symbol_renames(changes, [
        FileRename(old_path="Sources/Old.swift", new_path="Sources/New.swift"),
    ])
    # 纯文件移动但锚点全换且 token 零交集：不硬凑，保持删旧+增新语义
    assert renames == []


def test_cross_file_without_git_rename_is_not_speculation():
    changes = [
        _removed("Sources/A.swift#C.schedulePrimary()"),
        _added("Sources/B.swift#C.schedulePrimaryV2()"),
    ]
    # 无 git rename 背书的跨文件相似符号：拒绝配对，防臆测
    assert pair_symbol_renames(changes) == []


def test_greedy_one_to_one_and_deterministic_tiebreak():
    changes = [
        _removed("Sources/A.swift#C.schedulePrimary()"),
        _removed("Sources/A.swift#C.scheduleBackup()"),
        _added("Sources/A.swift#C.schedulePrimaryV2()"),
    ]
    first = pair_symbol_renames(changes)
    second = pair_symbol_renames(list(reversed(changes)))
    assert len(first) == 1  # 一个新符号最多配一个旧符号
    assert first[0].old_symbol_id.endswith("schedulePrimary()")
    # 输入乱序不影响输出（确定性）
    assert first == second


def test_modified_and_signature_changes_are_not_rename_inputs():
    changes = [
        _change("Sources/A.swift#C.keep()", ChangeType.MODIFIED),
        _change("Sources/A.swift#C.tick()", ChangeType.SIGNATURE_CHANGED),
    ]
    assert pair_symbol_renames(changes) == []


# ── 高价值判定 ──────────────────────────────────────────────────────────────

def test_is_high_value_or_semantics():
    assert is_high_value(
        quality_score=0.9, reference_count=0,
        quality_threshold=0.7, reference_threshold=2,
    )
    assert is_high_value(
        quality_score=0.2, reference_count=3,
        quality_threshold=0.7, reference_threshold=2,
    )
    assert not is_high_value(
        quality_score=0.2, reference_count=1,
        quality_threshold=0.7, reference_threshold=2,
    )
    # 边界：等于阈值即高价值
    assert is_high_value(
        quality_score=0.7, reference_count=0,
        quality_threshold=0.7, reference_threshold=2,
    )
