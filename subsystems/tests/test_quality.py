"""质量评分测试（docs/08 FR-ING-003：可解释、权威权重可配、只降权不删除）。"""

from testpilot_core.enums import AuthorityLevel
from testpilot_core.quality import score_code_symbol, score_text


def test_text_rich_evidence_scores_higher_than_plain():
    rich = """# 复盘\n\n## 根因\n见 [PRD](http://x/p) 与 `MissionCoordinator`。\n\n- 已验证于版本 abc123\n- 日志显示 os_log 断流\n\n更多上下文段落二。\n"""
    plain = "很短一句话。"
    rich_q = score_text(rich, heading_path=("复盘", "根因"))
    plain_q = score_text(plain)
    assert rich_q.score > plain_q.score
    assert set(rich_q.components) == {"structure", "evidence", "completeness", "authority"}
    assert rich_q.components["evidence"] > plain_q.components["evidence"]
    # 每个分项都有理由
    assert any("外链" in r for r in rich_q.reasons)
    assert 0.0 <= plain_q.score <= 1.0


def test_authority_level_moves_score_and_is_configurable():
    text = "普通段落，没有结构也没有引用，长度大概几十个字用来比较权威等级影响。" * 2
    high = score_text(text, authority_level=AuthorityLevel.L1_AUTHORITATIVE)
    low = score_text(text, authority_level=AuthorityLevel.L2_EXPERIENCE)
    assert high.components["authority"] == 1.0
    assert high.score > low.score

    # AC2：人工可配置权威权重（把权威权重设 0 后两版分数应一致）
    weights = {"structure": 0.4, "evidence": 0.3, "completeness": 0.3, "authority": 0.0}
    a = score_text(text, authority_level=AuthorityLevel.L1_AUTHORITATIVE, weights=weights)
    b = score_text(text, authority_level=AuthorityLevel.L2_EXPERIENCE, weights=weights)
    assert a.score == b.score


def test_code_symbol_doc_comment_and_calls_score():
    with_doc = score_code_symbol(
        "/// 文档\nfunc f() {\n    a()\n    b()\n}\n",
        signature="func f()", has_doc_comment=True, call_count=2,
    )
    bare = score_code_symbol("func g() {}", signature="func g()")
    assert with_doc.components["structure"] > bare.components["structure"]
    assert with_doc.components["evidence"] > bare.components["evidence"]
    assert any("doc comment" in r for r in with_doc.reasons)
