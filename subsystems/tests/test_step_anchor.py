"""docs/16 步骤锚点 step_id 全链路红证（P0+P1 合并切片，hostless 纯函数）。

覆盖红证矩阵中本切片负责的行：
- 解析器：显式「步骤 N / Step N」锚定、嵌套继承、越界死信、歧义死信、
  事件相对描述恒 None、表格体恒 None、历史无锚点文档全 None 不回归；
- round-trip：【类型｜步骤N】标签渲染→解析三字段无损，无锚点旧字节稳定；
- 挖掘：自有/平移断言 step_id 透传，suggestion_urn 不含锚点，模型越界拒入死信；
- 审批视图：队列载荷携 steps/preconditions，人审决定透传 step_id 进回写 Markdown。

知识型 bullet 分流（P2）与 case-export-v2（P3）不在本文件范围。
"""

from __future__ import annotations

from datetime import datetime, timezone

from testpilot_core.approval import (
    CaseDecisionRequest,
    SuggestionDecision,
    SuggestionView,
    build_review_queue,
    plan_case_decision,
    render_case_markdown,
)
from testpilot_core.enums import ApprovalDecision, OracleType
from testpilot_core.models import Oracle, TestCase
from testpilot_core.oracle_mining import (
    MinedOracle,
    apply_model_oracles,
    build_oracle_messages,
    mine_deterministic,
    oracle_json_schema,
    suggestion_urn,
)
from testpilot_core.testcase import (
    extract_step_anchor,
    parse_case_markdown,
    split_explicit_tag,
    split_explicit_type,
)
from testpilot_core.testpoint import ImpactEvidence

_NOW = datetime(2026, 11, 15, 9, 0, tzinfo=timezone.utc)
_CASE_URN = "urn:testpilot:fs-cases:test_case:cases/d.md#TC-S1"
_DOC_URN = "urn:testpilot:fs-cases:document:cases/d.md"
_INCIDENT_URN = "urn:testpilot:fs-knowledge:chunk:incidents/postmortem.md#L10"


def _narrative(oracle_lines: list[str], *, steps: int = 3) -> str:
    step_lines = "\n".join(f"{i}. 操作步骤 {i}" for i in range(1, steps + 1))
    bullets = "\n".join(f"- {line}" for line in oracle_lines)
    return (
        "## 模块 M\n\n"
        "### TC-S1 步骤锚点夹具（FR-1）P1\n\n"
        f"{step_lines}\n\n"
        "**预期**：\n"
        f"{bullets}\n"
    )


def _parse_one(text: str):
    result = parse_case_markdown(text, doc_external_id="cases/d.md")
    assert len(result.cases) == 1
    return result.cases[0], result.dead_letters


# ── 解析器：显式锚定（docs/16 §4.2 规则 1） ─────────────────────────────────

def test_explicit_step_anchor_forms_all_bind():
    case, dead = _parse_one(_narrative([
        "步骤1 后页面展示全屏横幅",       # 中文带空格语义（无空格写法）
        "步骤 2 执行后账本新增一行",      # 中文带空格
        "Step 3 后日志出现 RINGER 标记",  # 英文大小写不敏感
    ]))
    assert dead == []
    assert [o.step_id for o in case.oracles] == [1, 2, 3]


def test_event_relative_descriptions_remain_case_level():
    # 规则 3：T0/侧边键后/解锁后/亮屏瞬间不是稳定序号，硬映射就是猜测
    case, dead = _parse_one(_narrative([
        "T0 到侧边键之间不重复弹横幅",
        "侧边键后横幅消失且无补救通知",
        "解锁后进入 App 前入口依然可见可点击",
        "亮屏瞬间闹钟横幅重新展示",
    ]))
    assert dead == []
    assert [o.step_id for o in case.oracles] == [None, None, None, None]


def test_legacy_doc_without_anchors_all_none():
    # 规则 5：历史无锚点文档行为与升级前完全一致（现有 13 条解析测试之外的显式封条）
    case, dead = _parse_one(_narrative([
        "页面弹出全屏闹钟横幅",
        "`os_log` 记录一条 RINGER armed",
        "主观听感由人工确认",
    ]))
    assert dead == []
    assert all(o.step_id is None for o in case.oracles)


def test_nested_bullet_inherits_parent_anchor_and_cannot_override():
    # 规则 2：嵌套子弹挂同一条 oracle，继承父锚点；嵌套里另提步骤号不得改写
    text = (
        "## M\n\n### TC-S1 嵌套继承（FR-1）P1\n\n"
        "1. 步骤一\n2. 步骤二\n\n"
        "**预期**：\n"
        "- 步骤2 后账本写入接管记录\n"
        "  - 子项补充：记录状态为 stopped\n"
        "  - 子项提到步骤3 也不得改变父锚点\n"
        "- 无锚点顶层子弹保持用例级\n"
        "  - 嵌套细节：步骤1 的界面信号\n"
    )
    case, dead = _parse_one(text)
    assert dead == []
    assert [o.step_id for o in case.oracles] == [2, None]
    assert "步骤3" in case.oracles[0].expectation  # 文本保留，仅锚点不跟走
    assert "步骤1" in case.oracles[1].expectation   # 嵌套文本并入，锚点仍为 None


def test_out_of_range_anchor_is_dead_letter_but_batch_continues():
    # 用户拍板的 fail-loud：越界 step_id 置 None + CaseParseError，同批其他不断言
    text = (
        "## M\n\n"
        "### TC-S1 越界用例（FR-1）P1\n\n"
        "1. 步骤一\n2. 步骤二\n\n"
        "**预期**：\n"
        "- 步骤5 后应出现补发横幅\n"
        "- 步骤1 的正常断言不受影响\n"
        "\n"
        "### TC-S2 同批正常用例（FR-2）P1\n\n"
        "1. 唯一一步\n\n"
        "**预期**：\n"
        "- 步骤1 后日志打点完整\n"
    )
    result = parse_case_markdown(text, doc_external_id="cases/d.md")
    assert {c.external_id for c in result.cases} == {"TC-S1", "TC-S2"}
    s1 = next(c for c in result.cases if c.external_id == "TC-S1")
    assert [o.step_id for o in s1.oracles] == [None, 1]
    assert len(result.dead_letters) == 1
    letter = result.dead_letters[0]
    assert letter.case_id_hint == "TC-S1"
    assert "5" in letter.error and "越界" in letter.error and "2" in letter.error
    s2 = next(c for c in result.cases if c.external_id == "TC-S2")
    assert s2.oracles[0].step_id == 1  # 整批继续，后续用例不受死信影响


def test_ambiguous_multi_step_reference_is_dead_letter():
    # 一条断言同时引用两个步骤号属歧义，保守不锚并留痕，不替用户二选一
    case, dead = _parse_one(_narrative([
        "步骤2 与步骤3 的状态机均保持 ringing",
        "步骤1 的正常断言",
    ]))
    assert [o.step_id for o in case.oracles] == [None, 1]
    assert len(dead) == 1 and "多个步骤" in dead[0].error


def test_inline_expect_marker_anchor_collected():
    # **预期**： 同行内联形态（真档形态之一）同样走锚点入口
    text = (
        "## M\n\n### TC-S1 内联预期（FR-1）P1\n\n"
        "1. 步骤一\n2. 步骤二\n\n"
        "**预期**：步骤2 后横幅消失\n"
    )
    case, dead = _parse_one(text)
    assert dead == []
    assert case.oracles[0].step_id == 2


def test_table_oracles_always_case_level():
    # 规则 4：表格体单预期格无步骤结构，即使文本含「步骤 N」也不锚
    text = (
        "## M\n\n"
        "| ID | 步骤要点 | 预期 | 优先级 |\n"
        "| --- | --- | --- | --- |\n"
        "| TC-T1 | 前台等响 | 步骤1 后横幅展示 | P0 |\n"
    )
    case, dead = _parse_one(text)
    assert dead == []
    assert case.oracles[0].step_id is None


def test_extract_step_anchor_unit_contract():
    assert extract_step_anchor("步骤3 后展示横幅") == (3, "")
    assert extract_step_anchor("Step 2 done") == (2, "")
    assert extract_step_anchor("T0 后的事件描述") == (None, "")
    assert extract_step_anchor("footstep3 不含英文词边界") == (None, "")
    anchor, note = extract_step_anchor("步骤1 与步骤2 联动")
    assert anchor is None and "多个步骤" in note


# ── 显式标签：【类型｜步骤N】（docs/16 §4.6） ───────────────────────────────

def test_split_explicit_tag_parses_anchor_full_and_half_width():
    assert split_explicit_tag("【UI 断言｜步骤3】横幅消失") == (
        OracleType.UI_ASSERTION, 3, "横幅消失")
    assert split_explicit_tag("【日志断言|步骤2】`os_log` 打点") == (
        OracleType.LOG_ASSERTION, 2, "`os_log` 打点")
    # 无锚点段：旧标签形态 step_id 恒 None
    assert split_explicit_tag("【状态断言】账本落库") == (
        OracleType.STATE_ASSERTION, None, "账本落库")
    # 未知标签整体不识别，原文交回分类器
    assert split_explicit_tag("【随便写的｜步骤9】横幅") == (
        None, None, "【随便写的｜步骤9】横幅")


def test_legacy_split_explicit_type_wrapper_unchanged():
    # 旧调用方二值签名行为封条：锚点段对它们透明
    assert split_explicit_type("【UI 断言｜步骤3】横幅消失") == (
        OracleType.UI_ASSERTION, "横幅消失")
    assert split_explicit_type("普通预期") == (None, "普通预期")


def test_tag_anchor_out_of_range_also_dead_letters():
    case, dead = _parse_one(_narrative([
        "【UI 断言｜步骤9】回写文档里的越界锚点也要 fail-loud",
    ], steps=2))
    assert case.oracles[0].step_id is None
    assert len(dead) == 1 and "越界" in dead[0].error


# ── round-trip：渲染→重解析三字段无损（docs/16 §4.6） ───────────────────────

def test_render_parse_round_trip_preserves_step_id_type_text():
    case = TestCase(
        urn=_CASE_URN, external_id="TC-S1", title="步骤锚点夹具",
        steps=["操作步骤 1", "操作步骤 2", "操作步骤 3"],
        oracles=[], oracle_missing=True, tags=["FR-1"],
        module="模块 M", priority="P1",
    )
    oracles = [
        Oracle(type=OracleType.UI_ASSERTION, expectation="步骤3 的全屏横幅消失",
               source_refs=[_DOC_URN], confidence=0.8, step_id=3),
        Oracle(type=OracleType.LOG_ASSERTION, expectation="T0 见 RINGER armed",
               source_refs=[_DOC_URN], confidence=0.8, step_id=None),
        Oracle(type=OracleType.STATE_ASSERTION, expectation="账本新增 stopped 行",
               source_refs=[_DOC_URN], confidence=0.8, step_id=1),
    ]
    md = render_case_markdown(case, oracles, reviewed_by="expert-zhang",
                              decided_at=_NOW)
    assert "【UI 断言｜步骤3】" in md
    assert "【日志断言】T0" in md  # 用例级不输出锚点段
    parsed = parse_case_markdown(md, doc_external_id="cases/writeback/TC-S1.md")
    assert parsed.dead_letters == []
    rebuilt = parsed.cases[0]
    assert [(o.type, o.step_id, o.expectation) for o in rebuilt.oracles] == [
        (OracleType.UI_ASSERTION, 3, "步骤3 的全屏横幅消失"),
        (OracleType.LOG_ASSERTION, None, "T0 见 RINGER armed"),
        (OracleType.STATE_ASSERTION, 1, "账本新增 stopped 行"),
    ]


def test_render_without_anchors_keeps_legacy_byte_style():
    case = TestCase(
        urn=_CASE_URN, external_id="TC-S1", title="旧字节封条",
        steps=["s1"], oracles=[], oracle_missing=True,
    )
    md = render_case_markdown(
        case,
        [Oracle(type=OracleType.MANUAL, expectation="人工听感确认",
                source_refs=[_DOC_URN], confidence=0.5)],
        reviewed_by="r", decided_at=_NOW,
    )
    assert "- 【人工确认】人工听感确认" in md
    assert "｜" not in md and "步骤" not in md.split("**预期**", 1)[1]


# ── 挖掘透传与 URN 稳定（docs/16 §4.4） ─────────────────────────────────────

def _mining_case(**overrides) -> TestCase:
    base = dict(
        urn=_CASE_URN, external_id="TC-S1", title="挖掘透传夹具",
        steps=["操作步骤 1", "操作步骤 2"], oracles=[], oracle_missing=False,
    )
    base.update(overrides)
    return TestCase(**base)


def test_self_oracle_step_id_passes_through_to_suggestion():
    case = _mining_case(oracles=[
        Oracle(type=OracleType.STATE_ASSERTION, expectation="账本新增 stopped 行",
               source_refs=[_DOC_URN], confidence=0.5, step_id=2),
        Oracle(type=OracleType.UI_ASSERTION, expectation="横幅展示",
               source_refs=[_DOC_URN], confidence=0.5, step_id=None),
    ])
    result = mine_deterministic(case, None, [])
    by_text = {o.expectation: o for o in result.mined}
    assert by_text["账本新增 stopped 行"].step_id == 2
    assert by_text["横幅展示"].step_id is None


def test_transferred_oracle_carries_step_id():
    related = TestCase(
        urn="urn:testpilot:fs-cases:test_case:cases/d.md#TC-A5",
        external_id="TC-A5", title="相似用例", steps=["s1", "s2"],
        oracles=[Oracle(type=OracleType.LOG_ASSERTION,
                        expectation="补发链路日志完整",
                        source_refs=[_DOC_URN], confidence=0.5, step_id=2)],
    )
    case = _mining_case(oracle_missing=True)
    result = mine_deterministic(case, None, [], related_cases=[related])
    transferred = [o for o in result.mined
                   if o.source_kind == "existing_assertion"]
    assert len(transferred) == 1 and transferred[0].step_id == 2


def test_suggestion_urn_ignores_step_id_by_design():
    # 明确决策：URN 只哈希 type|expectation，锚点修订不换 URN，不产生孤儿审批
    anchored = MinedOracle(type=OracleType.STATE_ASSERTION,
                           expectation="账本新增 stopped 行",
                           source_kind="model_proposed", provenance="model",
                           step_id=2)
    case_level = anchored.model_copy(update={"step_id": None})
    assert suggestion_urn("TC-S1", anchored) == suggestion_urn("TC-S1", case_level)


def test_model_step_id_validation_accepts_in_range_and_null():
    evidence = [ImpactEvidence(urn=_INCIDENT_URN, kind="incident",
                               title="复盘", text="必须保留接管证据")]
    case = _mining_case(oracle_missing=True)
    spine = mine_deterministic(case, None, evidence)
    payload = {"oracles": [
        {"type": "state_assertion", "expectation": "锚到步骤一",
         "source_refs": [_INCIDENT_URN], "step_id": 1},
        {"type": "state_assertion", "expectation": "显式用例级",
         "source_refs": [_INCIDENT_URN], "step_id": None},
        {"type": "state_assertion", "expectation": "老模型缺字段等同 null",
         "source_refs": [_INCIDENT_URN]},
    ]}
    result = apply_model_oracles(case, spine, payload, None, evidence, [])
    assert result.rejected == []
    assert [o.step_id for o in result.model_oracles] == [1, None, None]


def test_model_step_id_out_of_range_bool_and_string_are_rejected():
    evidence = [ImpactEvidence(urn=_INCIDENT_URN, kind="incident",
                               title="复盘", text="必须保留接管证据")]
    case = _mining_case(oracle_missing=True)  # 共 2 个步骤
    spine = mine_deterministic(case, None, evidence)
    payload = {"oracles": [
        {"type": "state_assertion", "expectation": "越界序号",
         "source_refs": [_INCIDENT_URN], "step_id": 3},
        {"type": "state_assertion", "expectation": "布尔不是整数",
         "source_refs": [_INCIDENT_URN], "step_id": True},
        {"type": "state_assertion", "expectation": "字符串序号",
         "source_refs": [_INCIDENT_URN], "step_id": "1"},
        {"type": "state_assertion", "expectation": "零也越界",
         "source_refs": [_INCIDENT_URN], "step_id": 0},
    ]}
    result = apply_model_oracles(case, spine, payload, None, evidence, [])
    assert result.model_oracles == []
    assert len(result.rejected) == 4
    assert all(r.reason == "bad_shape" for r in result.rejected)
    assert "越界" in result.rejected[0].detail
    # 幻觉台账同路径：worker 落 dead_letters(source=oracle-mine) 的既有机制不变
    assert result.hallucination_rate == 1.0


def test_schema_and_prompt_carry_step_contract():
    item = oracle_json_schema()["properties"]["oracles"]["items"]
    assert item["properties"]["step_id"] == {
        "type": ["integer", "null"], "minimum": 1}
    # required 不含 step_id：老模型/缺字段载荷保持兼容
    assert "step_id" not in item["required"]
    case = _mining_case(oracle_missing=True)
    messages = build_oracle_messages(case, None, [], mine_deterministic(case, None, []).mined)
    system = messages[0]["content"]
    assert "step_id" in system and "null" in system and "越界" in system


# ── 审批队列载荷与人审透传（docs/16 §4.5/§4.6） ──────────────────────────────

def test_review_queue_carries_steps_and_preconditions():
    case = _mining_case(preconditions=["闹钟权限已授予"],
                        oracle_missing=True)
    view = SuggestionView(
        urn="urn:testpilot:fs-cases:oracle_suggestion:TC-S1#abc",
        case_urn=_CASE_URN, external_id="TC-S1", status="pending",
        oracle=MinedOracle(type=OracleType.STATE_ASSERTION,
                           expectation="账本落库", source_kind="model_proposed",
                           provenance="model", step_id=2),
    )
    queue = build_review_queue([view], [case])
    assert len(queue) == 1
    assert queue[0].steps == ["操作步骤 1", "操作步骤 2"]
    assert queue[0].preconditions == ["闹钟权限已授予"]
    assert queue[0].suggestions[0].oracle.step_id == 2


def test_plan_case_decision_passes_step_id_through_adopt_and_modify():
    case = _mining_case(oracle_missing=True)
    view = SuggestionView(
        urn="urn:testpilot:fs-cases:oracle_suggestion:TC-S1#abc",
        case_urn=_CASE_URN, external_id="TC-S1", status="pending",
        oracle=MinedOracle(type=OracleType.STATE_ASSERTION,
                           expectation="账本新增 stopped 行",
                           source_kind="model_proposed", provenance="model",
                           source_refs=[_DOC_URN], step_id=2),
    )
    adopt = plan_case_decision(
        CaseDecisionRequest(
            case_urn=_CASE_URN, decision=ApprovalDecision.APPROVED,
            reviewer="expert-zhang",
            items=[SuggestionDecision(suggestion_urn=view.urn, action="adopt")],
        ),
        case, [view], now=_NOW,
    )
    assert adopt.final_oracles[0].step_id == 2
    assert "【状态断言｜步骤2】" in adopt.markdown

    modify = plan_case_decision(
        CaseDecisionRequest(
            case_urn=_CASE_URN, decision=ApprovalDecision.APPROVED,
            reviewer="expert-zhang",
            items=[SuggestionDecision(
                suggestion_urn=view.urn, action="modify",
                oracle_type=OracleType.UI_ASSERTION,
                expectation="改写后的界面断言")],
        ),
        case, [view], now=_NOW,
    )
    # 人审可改类型/文本，锚点不在可改字段内，随草稿原样透传
    assert modify.final_oracles[0].step_id == 2
    assert "【UI 断言｜步骤2】改写后的界面断言" in modify.markdown
