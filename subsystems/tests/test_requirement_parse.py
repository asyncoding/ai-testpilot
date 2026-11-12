"""需求骨架抽取纯函数测试（docs/08 FR-TD-001，S2.2）。

夹具 tests/fixtures/requirements/synthetic-requirements.md 是自建小样
（不拷 V1 原文），覆盖 FR 子编号/缺优先级/取消/损坏行/NFR 两列表/跨行 AC/
显式 FR 引用/无 ID 风险表/代码围栏假结构。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from testpilot_core.requirement import (
    SkeletonValidationError,
    apply_linking,
    build_linking_messages,
    linking_json_schema,
    parse_requirement_markdown,
)

FIXTURE = Path(__file__).parent / "fixtures" / "requirements" / "synthetic-requirements.md"
DOC_ID = "requirements/synthetic-requirements.md"


def _parse():
    return parse_requirement_markdown(
        FIXTURE.read_text(encoding="utf-8"),
        doc_external_id=DOC_ID, version="hash34",
    )


def test_fixture_extracts_features_rules_modules_and_skips_other_tables():
    result = _parse()
    by_id = {f.fr_id: f for f in result.features}
    # 活跃 6（FR-1/2/2a/3/5/6）+ 取消 FR-4；损坏行 FR-7 不产生功能点
    assert set(by_id) == {"FR-1", "FR-2", "FR-2a", "FR-3", "FR-4", "FR-5", "FR-6"}
    assert len(result.active_features) == 6
    assert {r.rule_id for r in result.rules} == {"NFR-1", "NFR-2"}
    assert result.modules == ["2.1 备忘 CRUD", "2.2 提醒触达"]
    # 风险表无 ID/需求列且在 other 节：整体跳过，不是死信
    assert all("FR-FAKE" not in f.fr_id for f in result.features)
    assert result.version == "hash34"  # 版本挂在抽取结果上，URN 本身保持稳定


def test_sub_id_priority_and_cancelled_lifecycle():
    result = _parse()
    by_id = {f.fr_id: f for f in result.features}
    sub = by_id["FR-2a"]  # 子编号不被拆坏
    assert sub.priority == "P1" and sub.module == "2.1 备忘 CRUD" and sub.source_line
    assert by_id["FR-1"].priority == "P0"
    cancelled = by_id["FR-4"]
    assert cancelled.lifecycle == "cancelled" and cancelled.priority == ""
    gap_subjects = {g.subject for g in result.gaps if g.kind == "cancelled_feature"}
    assert "FR-4" in gap_subjects


def test_missing_priority_is_gap_not_fabrication():
    result = _parse()
    fr3 = next(f for f in result.features if f.fr_id == "FR-3")
    assert fr3.priority == "" and fr3.lifecycle == "active"
    gap = next(g for g in result.gaps
               if g.kind == "priority_missing" and g.subject == "FR-3")
    assert "优先级" in gap.detail


def test_malformed_row_dead_letter_does_not_block_batch():
    result = _parse()
    assert len(result.dead_letters) == 1
    dead = result.dead_letters[0]
    assert dead.subject_hint == "FR-7" and "列数" in dead.error
    # 同表其他行正常解析
    assert "FR-1" in {f.fr_id for f in result.features}
    malformed_gaps = [g for g in result.gaps if g.kind == "malformed_row"]
    assert len(malformed_gaps) == 1 and malformed_gaps[0].subject == "FR-7"


def test_acceptance_wrapped_lines_join_and_explicit_linking():
    result = _parse()
    assert len(result.acceptance) == 2
    ac1, ac2 = result.acceptance
    # 跨行续接拼到同一条，显式提及的 FR ID 建立确定性链接
    assert "跨行条目" in ac1.text and ac1.linked_fr_ids == ["FR-1", "FR-2"]
    assert ac2.linked_fr_ids == ["FR-5"]


def test_ac_missing_gaps_only_for_uncovered_active_features():
    result = _parse()
    missing = sorted(
        g.subject for g in result.gaps if g.kind == "ac_missing_for_feature")
    # FR-2a/FR-3/FR-6 无任何 AC 提及；取消态 FR-4 不参与覆盖对账
    assert missing == ["FR-2a", "FR-3", "FR-6"]


def test_skeleton_shape_marks_present_false_without_dropping_source_acs():
    result = _parse()
    skeleton = result.to_skeleton()
    assert skeleton.source_urn == result.doc_urn
    assert skeleton.modules == result.modules
    assert len(skeleton.features) == 6  # 取消态不进骨架功能点
    assert all(s.startswith("FR-") for s in skeleton.features)
    assert len(skeleton.rules) == 2 and all(s.startswith("NFR-") for s in skeleton.rules)
    present = [c for c in skeleton.acceptance_criteria if c.present]
    missing = [c for c in skeleton.acceptance_criteria if not c.present]
    assert len(present) == 2 and len(missing) == 3
    assert {c.text.split(" ")[0] for c in missing} == {"FR-2a", "FR-3", "FR-6"}


def test_missing_ac_section_flags_every_active_feature():
    text = (
        "# 无验收节\n\n## 1. 功能需求（FR）\n\n### 1.1 模块\n\n"
        "| ID | 需求 | 优先级 |\n|---|---|---|\n"
        "| FR-1 | 功能一 | P0 |\n| FR-2 | 功能二 | P1 |\n"
    )
    result = parse_requirement_markdown(text, doc_external_id="d.md")
    assert any(g.kind == "ac_section_missing" for g in result.gaps)
    skeleton = result.to_skeleton()
    assert all(not c.present for c in skeleton.acceptance_criteria)
    assert {c.text.split(" ")[0] for c in skeleton.acceptance_criteria} == {"FR-1", "FR-2"}


def test_urn_stable_across_reparse():
    text = (
        "## 1. 功能需求（FR）\n\n| ID | 需求 | 优先级 |\n|---|---|---|\n"
        "| FR-1 | 功能一 | P0 |\n"
    )
    first = parse_requirement_markdown(text, doc_external_id="d.md")
    second = parse_requirement_markdown(text, doc_external_id="d.md")
    assert first.features[0].urn == second.features[0].urn
    assert first.doc_urn == second.doc_urn
    assert first.features[0].urn.startswith("urn:testpilot:fs-reqs:requirement:d.md#FR-1")


def test_fenced_fr_table_is_not_structure():
    text = (
        "## 1. 功能需求（FR）\n\n"
        "| ID | 需求 | 优先级 |\n|---|---|---|\n| FR-1 | 真需求 | P0 |\n\n"
        "```markdown\n| FR-FAKE | 围栏假需求 | P0 |\n```\n"
    )
    result = parse_requirement_markdown(text, doc_external_id="d.md")
    assert {f.fr_id for f in result.features} == {"FR-1"}
    assert result.dead_letters == []


def test_apply_linking_unions_deterministic_links_and_recomputes_gaps():
    result = _parse()
    payload = {"links": [
        {"ac_index": 0, "fr_ids": ["FR-2a"]},      # 补上确定性未命中的 FR-2a
        {"ac_index": 1, "fr_ids": ["FR-6"]},       # 补 FR-6
    ]}
    linked = apply_linking(result, payload)
    assert linked.linking_source == "model"
    ac0 = next(a for a in linked.acceptance if a.index == 0)
    assert set(ac0.linked_fr_ids) == {"FR-1", "FR-2", "FR-2a"}  # 并集非覆盖
    missing = [g for g in linked.gaps if g.kind == "ac_missing_for_feature"]
    assert [g.subject for g in missing] == ["FR-3"]
    # 原结果不可变（model_copy 语义）
    assert result.linking_source == "deterministic"


def test_apply_linking_rejects_unknown_fr_id_fail_closed():
    result = _parse()
    payload = {"links": [{"ac_index": 0, "fr_ids": ["FR-999"]}]}
    with pytest.raises(SkeletonValidationError, match="FR-999"):
        apply_linking(result, payload)
    # 拒绝是整包的：不得部分写入
    assert all(a.linked_fr_ids == ["FR-1", "FR-2"]
               for a in result.acceptance if a.index == 0)


def test_apply_linking_rejects_out_of_range_ac_index_and_bad_shape():
    result = _parse()
    with pytest.raises(SkeletonValidationError, match="ac_index"):
        apply_linking(result, {"links": [{"ac_index": 99, "fr_ids": ["FR-1"]}]})
    with pytest.raises(SkeletonValidationError, match="links"):
        apply_linking(result, {"nope": []})
    with pytest.raises(SkeletonValidationError, match="fr_ids"):
        apply_linking(result, {"links": [{"ac_index": 0, "fr_ids": "FR-1"}]})


# ── L3-5/L3-6 红证：NFR 子串碰撞与 AC 主/次 FR 分级（W1/W2 盲评回流） ────────

def test_l3_5_nfr_id_not_misread_as_fr_substring():
    from testpilot_core.requirement import _FR_ID_RE

    # 旧正则 FR-\d+[a-z]? 会把 NFR-1 内部的 "FR-1" 抠出来，造成跨层假链接
    assert _FR_ID_RE.findall("NFR-1") == []
    assert _FR_ID_RE.findall("NFR-12 与 FR-3a") == ["FR-3a"]
    assert _FR_ID_RE.findall("FR-1") == ["FR-1"]  # 正常 FR 引用不受影响

    text = (
        "## 1. 功能需求（FR）\n\n"
        "| ID | 需求 | 优先级 |\n|---|---|---|\n"
        "| FR-1 | 功能一 | P0 |\n| FR-2 | 功能二 | P1 |\n\n"
        "## 2. 非功能需求（NFR）\n\n"
        "| ID | 需求 |\n|---|---|\n"
        "| NFR-1 | 写操作串行化 |\n\n"
        "## 3. 验收标准\n\n"
        "1. 功能一必须满足 NFR-1 的串行化约束，不得重复落盘。\n"
    )
    result = parse_requirement_markdown(text, doc_external_id="d.md")
    ac = result.acceptance[0]
    # 整条 AC 只提到 NFR-1：旧逻辑伪造 FR-1 链接，新逻辑一个 FR 链接都不产
    assert ac.primary_fr_ids == [] and ac.secondary_fr_ids == []
    assert ac.linked_fr_ids == []
    # FR-1/FR-2 如实进 ac_missing 对账，而不是被 NFR 子串假覆盖
    missing = {g.subject for g in result.gaps
               if g.kind == "ac_missing_for_feature"}
    assert missing == {"FR-1", "FR-2"}


def test_l3_6_split_helper_levels_and_dedup():
    from testpilot_core.requirement import _split_ac_fr_refs

    primary, secondary = _split_ac_fr_refs(
        "（FR-3、FR-3a）跨模块联动，正文补 FR-5；FR-3 重复提及不降级"
    )
    assert primary == ["FR-3", "FR-3a"]
    assert secondary == ["FR-5"]  # 标题里已声明的 FR-3 重复出现不算正文提及
    # 无标题括号的文档：primary 为空，全部走正文级（合成夹具旧口径）
    assert _split_ac_fr_refs("覆盖 FR-5 的触达保证。") == ([], ["FR-5"])
    # 标题里的 NFR 不产 FR（L3-5 词边界在分级函数里同样生效）
    assert _split_ac_fr_refs("（FR-1，NFR-1）串行化验收") == (["FR-1"], [])
    # 半角括号同等识别
    assert _split_ac_fr_refs("(FR-2) 半角标题验收")[0] == ["FR-2"]


def test_l3_6_ac_header_parenthesis_is_primary_body_mention_secondary():
    text = (
        "## 1. 功能需求（FR）\n\n"
        "| ID | 需求 | 优先级 |\n|---|---|---|\n"
        "| FR-1 | 功能一 | P0 |\n| FR-2 | 功能二 | P0 |\n"
        "| FR-3 | 功能三 | P1 |\n\n"
        "## 2. 验收标准\n\n"
        "1. （FR-1、FR-2）功能一与功能二联动，正文另见 FR-3 的配套约束。\n"
    )
    result = parse_requirement_markdown(text, doc_external_id="d.md")
    ac = result.acceptance[0]
    assert ac.primary_fr_ids == ["FR-1", "FR-2"]
    assert ac.secondary_fr_ids == ["FR-3"]
    # linked_fr_ids 保持主+次并集旧口径，覆盖率/gap 对账行为不变
    assert ac.linked_fr_ids == ["FR-1", "FR-2", "FR-3"]


def test_l3_6_headerless_fixture_acs_keep_union_fallback():
    # 合成夹具 AC 无句首括号习惯：primary 必须为空，linked 回退主次并集
    result = _parse()
    ac1, ac2 = result.acceptance
    assert ac1.primary_fr_ids == []
    assert ac1.secondary_fr_ids == ["FR-1", "FR-2"]
    assert ac1.linked_fr_ids == ["FR-1", "FR-2"]
    assert ac2.primary_fr_ids == []
    assert ac2.secondary_fr_ids == ["FR-5"]
    assert ac2.linked_fr_ids == ["FR-5"]


def test_prompt_and_schema_are_self_contained():
    result = _parse()
    messages = build_linking_messages(result)
    assert messages[0]["role"] == "system" and messages[1]["role"] == "user"
    user = messages[1]["content"]
    assert "FR-1" in user and "FR-6" in user and "ac_index" in user
    schema = linking_json_schema()
    assert schema["required"] == ["links"]
    item_props = schema["properties"]["links"]["items"]["properties"]
    assert set(item_props) == {"ac_index", "fr_ids"}
