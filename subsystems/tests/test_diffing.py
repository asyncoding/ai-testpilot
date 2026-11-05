"""符号集对账纯函数测试（docs/08 FR-CON-001 AC1）。"""

from testpilot_core.chunking import RawChunk
from testpilot_core.diffing import diff_symbol_sets
from testpilot_core.enums import ChangeType


def sym(anchor: str, *, text: str = "body\n", signature: str = "") -> RawChunk:
    return RawChunk(
        kind="code_symbol",
        anchor=anchor,
        title=anchor.split("#L", 1)[0],
        text=text,
        start_line=1,
        end_line=2,
        signature=signature,
    )


def as_map(changes):
    return {c.symbol_id: c.change_type for c in changes}


def test_added_removed_classified_and_unchanged_absent():
    old = [sym("keep.foo()"), sym("gone.bar()")]
    new = [sym("keep.foo()"), sym("fresh.baz()")]
    result = as_map(diff_symbol_sets(old, new))
    assert result == {
        "gone.bar()": ChangeType.REMOVED,
        "fresh.baz()": ChangeType.ADDED,
    }
    assert "keep.foo()" not in result


def test_text_only_change_is_modified():
    old = [sym("S.foo()", text="func foo() { return 1 }\n")]
    new = [sym("S.foo()", text="func foo() { return 2 }\n")]
    assert as_map(diff_symbol_sets(old, new)) == {"S.foo()": ChangeType.MODIFIED}


def test_signature_change_takes_priority_over_modified():
    # selector 不变（参数标签同为 a:）但签名类型变化 → SIGNATURE_CHANGED；
    # 此时文本必然也变，只报一次，不重复报 MODIFIED。
    # 注：参数标签变化会改变 anchor/selector，那属于 removed+added。
    old = [sym("S.foo(a:)", text="old body\n", signature="func foo(a: Int)")]
    new = [sym("S.foo(a:)", text="new body\n", signature="func foo(a: String)")]
    changes = diff_symbol_sets(old, new)
    assert len(changes) == 1
    assert changes[0].change_type == ChangeType.SIGNATURE_CHANGED


def test_no_change_and_empty_inputs():
    chunks = [sym("S.foo()"), sym("S.bar()")]
    assert diff_symbol_sets(chunks, list(chunks)) == []
    assert diff_symbol_sets([], []) == []


def test_line_disambiguation_suffix_does_not_cause_false_remove_add():
    # 重名 full_id 的 #L 行号后缀随版本漂移：配对按身份序号，不许报 removed+added
    old = [
        sym("X.foo()#L10", text="a\n"),
        sym("X.foo()#L30", text="b\n"),
    ]
    new = [
        sym("X.foo()#L12", text="a\n"),  # 行号漂移，内容不变
        sym("X.foo()#L40", text="changed\n"),  # 第二声明体变了
    ]
    assert as_map(diff_symbol_sets(old, new)) == {"X.foo()": ChangeType.MODIFIED}


def test_duplicate_ordinal_count_change_reports_removed_or_added():
    # 同名声明数量变化：少一个→REMOVED；多一个→ADDED（相同 symbol_id 的变化各保留一条）
    old_two = [sym("X.foo()#L10", text="first\n"), sym("X.foo()#L30", text="second\n")]
    new_one = [sym("X.foo()#L12", text="first\n")]
    assert as_map(diff_symbol_sets(old_two, new_one)) == {"X.foo()": ChangeType.REMOVED}

    old_one = [sym("X.foo()", text="first\n")]
    new_two = [sym("X.foo()#L10", text="first\n"), sym("X.foo()#L30", text="third\n")]
    assert as_map(diff_symbol_sets(old_one, new_two)) == {"X.foo()": ChangeType.ADDED}


def test_type_block_cascades_modified_when_member_changes():
    # 类型块文本含全部成员：属性 90s→30s 这类改动必然让类型块报 MODIFIED
    old = [
        sym("C", text="final class C {\n  let v = 90\n}\n", signature="final class C"),
        sym("C.use()", text="func use() { print(v) }\n"),
    ]
    new = [
        sym("C", text="final class C {\n  let v = 30\n}\n", signature="final class C"),
        sym("C.use()", text="func use() { print(v) }\n"),
    ]
    result = as_map(diff_symbol_sets(old, new))
    assert result == {"C": ChangeType.MODIFIED}


def test_text_chunks_are_ignored_when_no_symbol_change():
    old = [
        RawChunk(kind="text", anchor="背景", title="", text="x\n",
                 start_line=1, end_line=1),
        sym("S.foo()", text="a\n"),
    ]
    new = [
        RawChunk(kind="text", anchor="背景", title="", text="y\n",
                 start_line=1, end_line=1),
        sym("S.foo()", text="a\n"),
    ]
    # text 块不参与符号对账；完全无符号变化
    assert diff_symbol_sets(old, new) == []


def test_output_order_removed_then_added_is_stable():
    old = [sym(f"old{i}.m()") for i in range(3)]
    new = [sym(f"new{i}.m()") for i in range(3)]
    changes = diff_symbol_sets(old, new)
    assert [c.change_type for c in changes[:3]] == [ChangeType.REMOVED] * 3
    assert [c.change_type for c in changes[3:]] == [ChangeType.ADDED] * 3
    assert [c.symbol_id for c in changes[:3]] == [f"old{i}.m()" for i in range(3)]
