"""切块纯函数测试（docs/08 FR-ING-002）。"""

from testpilot_core.chunking import chunk_markdown, chunk_swift


# ── Markdown ────────────────────────────────────────────────────────────────

def test_markdown_sections_with_heading_path():
    text = "# 标题\n\n## 背景\n正文一\n\n## 根因\n正文二\n"
    chunks = chunk_markdown(text)
    # H1 标题独占不成块（其标题体现在子章节 heading_path）
    assert [c.anchor for c in chunks] == ["背景", "根因"]
    assert chunks[0].heading_path == ("标题", "背景")
    assert chunks[0].kind == "text"
    assert "正文一" in chunks[0].text


def test_markdown_heading_inside_code_fence_is_not_section():
    text = "# 真实标题\n\n```swift\n// # 这是代码不是标题\nfunc x() {}\n```\n\n正文\n"
    chunks = chunk_markdown(text)
    assert len(chunks) == 1
    assert chunks[0].anchor == "真实标题"
    assert "# 这是代码不是标题" in chunks[0].text


def test_markdown_duplicate_headings_get_unique_anchors_and_chinese_slug():
    text = "## 问题\n\n一\n\n## 问题\n\n二\n"
    chunks = chunk_markdown(text)
    assert [c.anchor for c in chunks] == ["问题", "问题-2"]
    # 中文保留在 slug 中
    assert chunk_markdown("## 响铃链路\n\n正文\n")[0].anchor == "响铃链路"


def test_markdown_long_section_splits_with_stable_line_numbers():
    para = "段落内容。" * 200  # 1000+ 字
    text = f"## 长章节\n\n{para}\n\n{para}\n"
    chunks = chunk_markdown(text, max_chars=800)
    assert len(chunks) >= 2
    assert chunks[0].anchor == "长章节"
    assert chunks[1].anchor.startswith("长章节__p")
    # 第二段起始行晚于第一段
    assert chunks[1].start_line > chunks[0].start_line


# ── Swift 最简符号切 ─────────────────────────────────────────────────────────

SAMPLE = '''import Foundation

/// 协调主闹钟调度
final class MissionCoordinator: NSObject {
    /// 调度主任务
    func schedulePrimary(notification: String, at date: Date) {
        self.cancelExisting()
        armTimer()
    }

    func cancelExisting() {
        // fakeCallInComment() 不应成为关系
        let s = "fakeCallInString()"
    }
}

func armTimer() {
    schedulePrimary(notification: "x", at: Date())
}
'''


def test_swift_symbols_independently_chunked():
    chunks = chunk_swift(SAMPLE)
    anchors = {c.anchor: c for c in chunks}
    # FR-ING-002 AC1：符号可独立召回，selector 带参数标签
    assert "MissionCoordinator.schedulePrimary(notification:at:)" in anchors
    assert "MissionCoordinator.cancelExisting()" in anchors
    assert "armTimer()" in anchors
    assert "MissionCoordinator" in anchors

    method = anchors["MissionCoordinator.schedulePrimary(notification:at:)"]
    assert method.kind == "code_symbol"
    assert method.start_line == 6 and method.end_line == 9
    assert "func schedulePrimary" in method.signature
    assert method.has_doc_comment is True
    # 方法块文本不串到下一个符号
    assert "cancelExisting" in method.text and "func armTimer" not in method.text


def test_swift_same_file_call_relations():
    chunks = {c.anchor: c for c in chunk_swift(SAMPLE)}
    method = chunks["MissionCoordinator.schedulePrimary(notification:at:)"]
    targets = {r.target_symbol for r in method.relations}
    assert "MissionCoordinator.cancelExisting()" in targets  # self. 调用
    assert "armTimer()" in targets                                  # 裸名全局函数
    # 证据是真实源码行
    assert all("cancelExisting" in r.evidence or "armTimer" in r.evidence
               for r in method.relations)

    # 注释/字符串里的"调用"不算关系；自身函数声明不算关系
    cancel = chunks["MissionCoordinator.cancelExisting()"]
    assert [r for r in cancel.relations if "fake" in r.target_symbol] == []


def test_swift_comments_and_strings_are_not_symbols():
    chunks = chunk_swift(SAMPLE)
    names = {c.anchor for c in chunks}
    assert not any("fakeCall" in n for n in names)


def test_swift_type_and_extension_same_name_anchors_are_unique():
    src = '''struct Foo {
    func a() {}
}

extension Foo {
    func b() {}
}
'''
    chunks = chunk_swift(src)
    anchors = [c.anchor for c in chunks]
    assert len(anchors) == len(set(anchors)), anchors
    type_chunks = [c for c in chunks if c.title == "Foo"]
    assert len(type_chunks) == 2
    assert all("#L" in c.anchor for c in type_chunks)
    # 方法块仍保持干净 anchor
    titles = {c.title: c for c in chunks}
    assert "Foo.a()" in titles and "Foo.b()" in titles


def test_swift_protocol_bodyless_requirement_does_not_crash():
    src = "protocol AlarmStore {\n    func save(id: String)\n    var count: Int { get }\n}\n"
    chunks = chunk_swift(src)
    kind_names = {c.anchor for c in chunks}
    assert "AlarmStore" in kind_names
    assert "AlarmStore.save(id:)" in kind_names
