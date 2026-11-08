"""E6 tree-sitter Swift 切块器测试（docs/08 FR-ING-002 AC3 选型复核）。

纪律：tree-sitter 是 adapters optional dependency，本文件仅在包装好的环境运行；
core 零三方红线由 import-lint 独立守。测试重点是与词法器的**契约一致性**
（同符号同 anchor/行范围/selector/关系）与 AST 增量覆盖面，不是 grammar 本身。
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from testpilot_adapters.store import bootstrap_store
from testpilot_adapters.tree_sitter_chunker import (
    TREE_SITTER_SWIFT_CHUNKER_VERSION,
    TreeSwiftChunker,
    build_byte_to_char,
    tree_sitter_available,
)
from testpilot_core.chunking import (
    SWIFT_CHUNKER_VERSION,
    chunk_swift,
)
from testpilot_core.errors import AdapterUnavailableError
from testpilot_workers.ingestion import (
    _build_chunks,
    _chunk_swift_source,
)

STANDARD_SRC = '''import Foundation

/// 协调器（中文注释：多字节偏移考验）
final class MissionCoordinator {
    /// 调度主任务
    func schedulePrimary(notification: String, at date: Date) {
        self.cancelExisting()
        armTimer()
    }

    func cancelExisting() {}
}

func armTimer() {}
'''

COMPACT_SRC = '''protocol P { func req() -> Int }
struct S { init(name: String) { self.name = name } let name: String }
extension MissionCoordinator { func ext() { helper() } }
'''

# 真实仓回归夹具：@MainActor / @available 属性在 grammar 里挂
# modifiers>attribute>user_type>type_identifier，DFS 首命中会把属性名误当 owner
ATTRIBUTED_SRC = '''@MainActor
final class ViewModel: ObservableObject {
    @available(iOS 17.0, *)
    func modern() {}
}

@available(iOS 17.0, *)
extension ViewModel {
    func ext() {}
}
'''


def _anchor_map(chunks):
    return {c.anchor: c for c in chunks}


def test_standard_declarations_match_lexical_contract():
    """常规 class/func：两切块器 anchor、行范围、签名、doc 标记逐项一致。"""
    tc = TreeSwiftChunker()
    ts_chunks = tc.chunk(STANDARD_SRC)
    lex_chunks = chunk_swift(STANDARD_SRC)
    ts_map, lex_map = _anchor_map(ts_chunks), _anchor_map(lex_chunks)
    assert set(lex_map) <= set(ts_map)  # AST 是超集，不允许丢词法器能切出的符号
    for anchor, lex in lex_map.items():
        ast = ts_map[anchor]
        assert ast.start_line == lex.start_line
        assert ast.end_line == lex.end_line
        assert ast.signature == lex.signature
        assert ast.has_doc_comment == lex.has_doc_comment
        assert ast.heading_path == lex.heading_path
        assert ast.kind == "code_symbol"
    # 关系口径复用同一 core 函数：主任务块的两个调用必须一致
    primary = next(c for c in ts_chunks if "schedulePrimary" in c.anchor)
    targets = {r.target_symbol for r in primary.relations}
    assert "MissionCoordinator.cancelExisting()" in targets
    assert "armTimer()" in targets


def test_selector_labels_follow_swift_convention():
    src = "class C {\n func f(_ internal: Int, at date: Date) {}\n}\n"
    anchors = {c.anchor for c in TreeSwiftChunker().chunk(src)}
    assert "C.f(_:at:)" in anchors


def test_tree_sitter_captures_compact_declarations_lexical_misses():
    """单行 protocol 方法/extension 方法/无 func 关键字的 init 是词法器已知盲区。"""
    lex_anchors = {c.anchor for c in chunk_swift(COMPACT_SRC)}
    ts_anchors = {c.anchor for c in TreeSwiftChunker().chunk(COMPACT_SRC)}
    assert {"P.req()", "S.init(name:)", "MissionCoordinator.ext()"} <= ts_anchors
    # 显式记录词法器盲区（选型对比的关键证据；若词法器未来补强，本测试要同步更新）
    assert "P.req()" not in lex_anchors
    assert "S.init(name:)" not in lex_anchors


def test_attributes_do_not_pollute_type_owner_naming():
    """真实仓回归：@MainActor/@available 的属性类型名不能成为声明 owner。

    grammar 把属性挂在 modifiers>attribute>user_type>type_identifier，
    全树 DFS 首个 type_identifier 会先命中属性名（真实仓曾切出
    MainActor.xxx / available.xxx 错误 anchor，见 E6 chunker-comparison 报告）。
    """
    anchors = {c.anchor for c in TreeSwiftChunker().chunk(ATTRIBUTED_SRC)}
    assert "ViewModel.modern()" in anchors
    assert "ViewModel.ext()" in anchors
    # class 与同名 extension 走 #L 去歧，两个类型块都归 ViewModel（裸名不再存在）
    type_blocks = {a for a in anchors if a.startswith("ViewModel#L")}
    assert len(type_blocks) == 2
    leaked = {a for a in anchors if "MainActor" in a or a.startswith("available")}
    assert leaked == set()


def test_extension_shares_type_identity_with_L_disambiguation():
    src = "class Foo { func a() {} }\nextension Foo { func b() {} }\n"
    anchors = {c.anchor for c in TreeSwiftChunker().chunk(src)}
    # 与词法器同规则：class 与 extension 同名 → 类型块 #L 去歧，方法挂同 owner
    assert "Foo#L1" in anchors and "Foo#L2" in anchors
    assert "Foo.a()" in anchors and "Foo.b()" in anchors


def test_multibyte_comments_keep_byte_char_offsets_aligned():
    b2c = build_byte_to_char("中a文")
    # “中”=3 字节，字符位 1 对应字节 3
    assert b2c[0] == 0 and b2c[3] == 1 and b2c[4] == 2
    chunks = TreeSwiftChunker().chunk(STANDARD_SRC)
    lines = STANDARD_SRC.splitlines()
    for c in chunks:
        # 切回的首行必须能在原文对应行号找到（错位会直接断言失败）
        first = c.text.splitlines()[0].strip()
        assert first in lines[c.start_line - 1]


def test_parse_info_counts_error_nodes_but_keeps_recovered_symbols():
    # 实测 tree-sitter-swift 0.7.x：嵌套类后还有同级成员时挂 ERROR 恢复节点，
    # 但恢复出的子声明与同级方法都不能丢（遍历不剪 ERROR 枝的守卫测试）
    src = "class Outer { class Inner { func f() {} } func g() {} }\n"
    tc = TreeSwiftChunker()
    anchors = {c.anchor for c in tc.chunk(src)}
    assert tc.parse_info.error_node_count >= 1
    assert "Inner.f()" in anchors and "Outer.g()" in anchors


def test_missing_optional_dependency_raises_adapter_unavailable(monkeypatch):
    # sys.modules[name]=None 会让 import 抛 ImportError（PEP 328），模拟未装 optional group
    monkeypatch.setitem(sys.modules, "tree_sitter", None)
    monkeypatch.setitem(sys.modules, "tree_sitter_swift", None)
    with pytest.raises(AdapterUnavailableError, match="tree-sitter"):
        TreeSwiftChunker()


def test_availability_probe_returns_bool():
    assert tree_sitter_available() is True


# ── ingestion 接缝：版本位透传与单文件回退 ──────────────────────────────────

class _FakeChunker:
    version = "fake-ts-1"

    def __init__(self, fail: bool = False):
        self._fail = fail
        self.called = 0

    def chunk(self, text):
        self.called += 1
        if self._fail:
            raise RuntimeError("grammar exploded")
        return chunk_swift(text)  # 替身只负责验证接缝，切块仍委托真词法器


def test_chunk_swift_source_default_path_is_lexical():
    # swift_chunker=None 即默认 profile：不接触 store，直接走 core 词法器
    raws, version = _chunk_swift_source(STANDARD_SRC, None, None, "urn:x", "run-1")
    assert version == SWIFT_CHUNKER_VERSION
    assert raws == chunk_swift(STANDARD_SRC)


def test_chunk_swift_source_uses_injected_chunker_version():
    fake = _FakeChunker()
    raws, version = _chunk_swift_source(STANDARD_SRC, fake, None, "urn:x", "run-1")
    assert version == "fake-ts-1" and fake.called == 1
    assert raws == chunk_swift(STANDARD_SRC)


def test_chunk_swift_source_fallback_writes_dead_letter(tmp_path):
    store = bootstrap_store(tmp_path)
    fake = _FakeChunker(fail=True)
    try:
        raws, version = _chunk_swift_source(
            STANDARD_SRC, fake, store, "urn:test:doc:x", "run-9")
        assert version == SWIFT_CHUNKER_VERSION  # 回退词法器
        assert {c.anchor for c in raws} == {c.anchor for c in chunk_swift(STANDARD_SRC)}
        row = store.conn.execute(
            "SELECT source, payload_json, error FROM dead_letters "
            "WHERE source='ingestion-tree-sitter-fallback'").fetchone()
        assert row is not None
        assert "run-9" in row["payload_json"] and "grammar exploded" in row["error"]
    finally:
        store.close()


def test_build_chunks_stamps_injected_chunker_version(tmp_path):
    store = bootstrap_store(tmp_path)
    try:
        doc = SimpleNamespace(
            urn="urn:testpilot:fs-docs:doc:repo/Sources/Foo.swift",
            authority_level="team", branch="main")
        raws = chunk_swift(STANDARD_SRC)
        chunks, _ = _build_chunks(
            doc, raws, valid_from="abc123",
            swift_chunker_version=TREE_SITTER_SWIFT_CHUNKER_VERSION)
        code_chunks = [c for c in chunks if c.kind == "code_symbol"]
        assert code_chunks and all(
            c.metadata_["chunker_version"] == TREE_SITTER_SWIFT_CHUNKER_VERSION
            for c in code_chunks)
    finally:
        store.close()
