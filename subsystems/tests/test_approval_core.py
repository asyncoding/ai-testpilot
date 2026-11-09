"""FR-TD-004 / FR-WR-001 人审回写纯逻辑 hostless 测试（S2.5）。

不碰数据库/文件系统：队列组装、人审计划、置信度加成、偏好 diff、
Markdown 渲染与 S2.1 解析器的无损 round-trip、幂等键稳定性全部在内存验证。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from testpilot_core.approval import (
    CASE_REPO_TARGET_SYSTEM,
    ApprovalValidationError,
    CaseDecisionRequest,
    SuggestionDecision,
    SuggestionView,
    build_review_queue,
    confirmed_confidence,
    field_diff,
    outbox_idempotency_key,
    plan_case_decision,
    render_case_markdown,
)
from testpilot_core.enums import (
    ActionLevel,
    ApprovalDecision,
    OracleType,
    OutboxStatus,
)
from testpilot_core.models import Oracle, TestCase
from testpilot_core.oracle_mining import MinedOracle
from testpilot_core.testcase import parse_case_markdown, split_explicit_type

_NOW = datetime(2026, 11, 9, 8, 30, tzinfo=timezone.utc)
_CASE_URN = "urn:testpilot:fs-cases:test_case:cases/synthetic-cases.md#TC-N2"
_DOC_URN = "urn:testpilot:fs-cases:document:cases/synthetic-cases.md"
_FR1_URN = "urn:testpilot:fs-reqs:requirement:req.md#FR-1"


def _case(**overrides) -> TestCase:
    base = dict(
        urn=_CASE_URN,
        external_id="TC-N2",
        title="只有步骤没有预期",
        steps=["打开 App", "触发闹钟"],
        oracles=[],
        oracle_missing=True,
        tags=["FR-1"],
        module="模块 A 叙事体",
        priority="P1",
    )
    base.update(overrides)
    return TestCase(**base)


def _mined(oid: str, *, otype=OracleType.STATE_ASSERTION,
           expectation="落库一条 stopped 记录", confidence=0.5,
           source_kind="model_proposed", provenance="model",
           refs=None) -> MinedOracle:
    return MinedOracle(
        type=otype, expectation=expectation, confidence=confidence,
        source_kind=source_kind, provenance=provenance,  # type: ignore[arg-type]
        source_refs=refs if refs is not None else [_FR1_URN],
    )


def _view(oid: str, mined: MinedOracle | None = None, *,
          status="pending", case_urn=_CASE_URN, exploratory=False) -> SuggestionView:
    mined = mined or _mined(oid)
    return SuggestionView(
        urn=f"urn:testpilot:fs-cases:oracle_suggestion:TC-N2#{oid}",
        case_urn=case_urn, external_id="TC-N2", status=status,
        exploratory=exploratory, oracle=mined,
    )


def _request(items, *, decision=ApprovalDecision.APPROVED,
             reviewer="expert-zhang", reason="评审通过", case_urn=_CASE_URN):
    return CaseDecisionRequest(
        case_urn=case_urn, decision=decision, reviewer=reviewer,
        reason=reason, items=items,
    )


# ── 显式类型标签 ────────────────────────────────────────────────────────────

def test_split_explicit_type_all_labels():
    assert split_explicit_type("【UI 断言】横幅出现") == (OracleType.UI_ASSERTION, "横幅出现")
    assert split_explicit_type("【日志断言】`os_log` 有记录") == (
        OracleType.LOG_ASSERTION, "`os_log` 有记录")
    assert split_explicit_type("【状态断言】账本落库") == (
        OracleType.STATE_ASSERTION, "账本落库")
    assert split_explicit_type("【人工确认】主观手感顺滑") == (
        OracleType.MANUAL, "主观手感顺滑")


def test_split_explicit_type_fallback():
    assert split_explicit_type("没有标签的普通预期") == (None, "没有标签的普通预期")
    # 未知标签不猜类型，原文交回 classify_oracle
    assert split_explicit_type("【随便写的】横幅出现") == (
        None, "【随便写的】横幅出现")


def test_explicit_tag_overrides_keyword_classification():
    # 文本含状态关键词，但标签权威指定 UI：解析必须信标签（round-trip 无损的关键）
    text = "【UI 断言】状态账本落库一条记录"
    parsed = parse_case_markdown(
        f"### TC-T1 标签优先 P1\n1. 步骤一\n**预期**：\n- {text}\n",
        doc_external_id="d.md",
    )
    assert len(parsed.cases) == 1
    oracle = parsed.cases[0].oracles[0]
    assert oracle.type is OracleType.UI_ASSERTION
    assert oracle.expectation == "状态账本落库一条记录"


def test_untagged_bullet_keeps_legacy_classification():
    parsed = parse_case_markdown(
        "### TC-T2 旧文档 P1\n1. 步骤一\n**预期**：\n- 页面展示全屏横幅\n",
        doc_external_id="d.md",
    )
    assert parsed.cases[0].oracles[0].type is OracleType.UI_ASSERTION
    assert parsed.cases[0].oracles[0].expectation == "页面展示全屏横幅"


# ── 审批队列 ────────────────────────────────────────────────────────────────

def test_queue_groups_only_pending_suggestions():
    case = _case()
    views = [
        _view("a"),
        _view("b", status="approved"),
        _view("c", status="rejected"),
    ]
    queue = build_review_queue(views, [case])
    assert len(queue) == 1
    item = queue[0]
    assert item.external_id == "TC-N2"
    assert [s.urn.rsplit("#", 1)[1] for s in item.suggestions] == ["a"]
    assert item.oracle_missing is True


def test_queue_exploratory_flag_and_sort():
    case_a = _case(urn=_CASE_URN, external_id="TC-A1")
    other_urn = "urn:testpilot:fs-cases:test_case:cases/x.md#TC-B2"
    case_b = _case(urn=other_urn, external_id="TC-B2", oracle_missing=False)
    queue = build_review_queue(
        [_view("a", case_urn=case_a.urn, exploratory=True),
         _view("b", case_urn=other_urn)],
        [case_a, case_b],
    )
    assert [i.external_id for i in queue] == ["TC-A1", "TC-B2"]
    assert queue[0].exploratory is True and queue[1].exploratory is False


def test_queue_unknown_case_is_loud_failure():
    with pytest.raises(ApprovalValidationError, match="不存在的用例"):
        build_review_queue([_view("a", case_urn="urn:ghost")], [_case()])


# ── 人审计划：采纳 ──────────────────────────────────────────────────────────

def test_plan_adopt_confidence_bump_and_full_plan():
    case = _case()
    view = _view("a", _mined("a", confidence=0.5))
    plan = plan_case_decision(
        _request([SuggestionDecision(suggestion_urn=view.urn, action="adopt")]),
        case, [view], now=_NOW,
    )
    assert plan.approval.action_level is ActionLevel.R2_REVIEWED_WRITE
    assert plan.approval.target_system == CASE_REPO_TARGET_SYSTEM
    assert plan.approval.decision is ApprovalDecision.APPROVED
    assert plan.approval.reviewer == "expert-zhang"
    assert plan.approval.decided_at == _NOW
    # +0.3：0.5 → 0.8
    assert plan.final_oracles[0].confidence == 0.8
    assert plan.final_oracles[0].source_refs == [_FR1_URN]
    assert plan.suggestion_updates[0].status == "approved"
    assert plan.preference_samples == []
    assert plan.outbox is not None
    assert plan.outbox.status is OutboxStatus.PENDING
    assert plan.outbox.approval_id == plan.approval.approval_id
    assert plan.outbox.idempotency_key == plan.idempotency_key
    assert plan.outbox.rollback_note  # FR-WR-001 AC1 回滚说明非空
    body = plan.outbox.request_body
    assert body["case_external_id"] == "TC-N2"
    assert body["doc_external_id"] == "cases/synthetic-cases.md"
    assert body["markdown"] == plan.markdown


def test_confidence_bump_is_capped_at_one():
    assert confirmed_confidence(0.7) == 1.0
    assert confirmed_confidence(0.5) == 0.8
    view = _view("a", _mined("a", confidence=0.7, source_kind="requirement_ac",
                             provenance="deterministic"))
    plan = plan_case_decision(
        _request([SuggestionDecision(suggestion_urn=view.urn, action="adopt")]),
        _case(), [view], now=_NOW,
    )
    assert plan.final_oracles[0].confidence == 1.0


# ── 人审计划：修改与偏好样本 ────────────────────────────────────────────────

def test_plan_modify_records_preference_sample_and_diff():
    view = _view("a", _mined(
        "a", otype=OracleType.LOG_ASSERTION, expectation="日志出现一行",
        confidence=0.5))
    req = _request([SuggestionDecision(
        suggestion_urn=view.urn, action="modify",
        oracle_type=OracleType.STATE_ASSERTION,
        expectation="落库 stopped 账本且字段齐全",
    )])
    plan = plan_case_decision(req, _case(), [view], now=_NOW)
    assert len(plan.preference_samples) == 1
    sample = plan.preference_samples[0]
    assert sample.case_urn == _CASE_URN
    assert sample.suggestion_urn == view.urn
    assert sample.reviewer == "expert-zhang"
    assert sample.diff["type"] == {"from": "log_assertion", "to": "state_assertion"}
    assert sample.diff["expectation"]["to"] == "落库 stopped 账本且字段齐全"
    assert sample.revised["expectation"] == "落库 stopped 账本且字段齐全"
    assert sample.original["expectation"] == "日志出现一行"
    # 修改接受同样享受 +0.3
    assert plan.final_oracles[0].confidence == 0.8
    assert plan.suggestion_updates[0].status == "approved"
    # 来源 URN 人审不可改
    assert plan.final_oracles[0].source_refs == [_FR1_URN]


def test_field_diff_only_flags_real_changes():
    mined = _mined("a", expectation="原文")
    assert field_diff(mined, mined.type, mined.expectation) == {}
    diff = field_diff(mined, OracleType.MANUAL, "新文")
    assert set(diff) == {"type", "expectation"}


# ── 人审计划：整案拒绝 ──────────────────────────────────────────────────────

def test_plan_reject_all_has_no_outbox():
    view_a = _view("a")
    view_b = _view("b", _mined("b", expectation="另一条"))
    req = _request(
        [SuggestionDecision(suggestion_urn=view_a.urn, action="reject"),
         SuggestionDecision(suggestion_urn=view_b.urn, action="reject")],
        decision=ApprovalDecision.REJECTED, reason="预期方向不对",
    )
    plan = plan_case_decision(req, _case(), [view_a, view_b], now=_NOW)
    assert plan.outbox is None
    assert plan.final_oracles == []
    assert plan.preference_samples == []
    assert plan.approval.decision is ApprovalDecision.REJECTED
    assert {u.status for u in plan.suggestion_updates} == {"rejected"}
    assert all(u.reason == "预期方向不对" for u in plan.suggestion_updates)


# ── fail-closed 校验 ────────────────────────────────────────────────────────

def test_plan_validation_errors():
    view = _view("a")
    # 第二条「待审」草稿：覆盖完整性校验要求一次评审交代全部 pending
    other_pending = _view("b", _mined("b", expectation="另一条待审"))
    reviewed = _view("c", status="approved")

    with pytest.raises(ApprovalValidationError, match="不存在或已评审"):
        plan_case_decision(
            _request([SuggestionDecision(suggestion_urn="urn:ghost", action="adopt")]),
            _case(), [view], now=_NOW)

    with pytest.raises(ApprovalValidationError, match="重复决策"):
        plan_case_decision(
            _request([SuggestionDecision(suggestion_urn=view.urn, action="adopt"),
                      SuggestionDecision(suggestion_urn=view.urn, action="reject")]),
            _case(), [view], now=_NOW)

    with pytest.raises(ApprovalValidationError, match="未给出决策"):
        plan_case_decision(
            _request([SuggestionDecision(suggestion_urn=view.urn, action="adopt")]),
            _case(), [view, other_pending], now=_NOW)

    # 已评审（非 pending）草稿不在覆盖集合内，交代全部 pending 即通过覆盖校验
    plan_case_decision(
        _request([SuggestionDecision(suggestion_urn=view.urn, action="adopt")]),
        _case(), [view, reviewed], now=_NOW)

    with pytest.raises(ApprovalValidationError, match="都是 reject"):
        plan_case_decision(
            _request([SuggestionDecision(suggestion_urn=view.urn, action="adopt")],
                     decision=ApprovalDecision.REJECTED),
            _case(), [view], now=_NOW)

    with pytest.raises(ApprovalValidationError, match="应为 rejected"):
        plan_case_decision(
            _request([SuggestionDecision(suggestion_urn=view.urn, action="reject")]),
            _case(), [view], now=_NOW)

    with pytest.raises(ApprovalValidationError, match="不允许携带修改字段"):
        plan_case_decision(
            _request([SuggestionDecision(suggestion_urn=view.urn, action="adopt",
                                         expectation="偷改")]),
            _case(), [view], now=_NOW)

    with pytest.raises(ApprovalValidationError, match="非空 expectation"):
        plan_case_decision(
            _request([SuggestionDecision(suggestion_urn=view.urn, action="modify",
                                         expectation="   ")]),
            _case(), [view], now=_NOW)


def test_plan_rejects_pending_decision_and_case_mismatch():
    view = _view("a")
    with pytest.raises(ApprovalValidationError, match="只允许 approved/rejected"):
        plan_case_decision(
            _request([SuggestionDecision(suggestion_urn=view.urn, action="adopt")],
                     decision=ApprovalDecision.PENDING),
            _case(), [view], now=_NOW)
    with pytest.raises(ApprovalValidationError, match="不一致"):
        plan_case_decision(
            _request([SuggestionDecision(suggestion_urn=view.urn, action="adopt")],
                     case_urn="urn:other:case:x#TC-X"),
            _case(), [view], now=_NOW)
    with pytest.raises(ApprovalValidationError, match="没有待审草稿"):
        plan_case_decision(
            _request([SuggestionDecision(suggestion_urn=view.urn, action="adopt")]),
            _case(), [], now=_NOW)


# ── Markdown 渲染与 round-trip ──────────────────────────────────────────────

def test_render_contains_all_contract_sections():
    case = _case(preconditions=["闹钟权限已授予"])
    oracles = [
        Oracle(type=OracleType.UI_ASSERTION, expectation="全屏横幅接管",
               confidence=1.0),
        Oracle(type=OracleType.LOG_ASSERTION, expectation="`os_log` 记录响铃",
               confidence=0.8),
        Oracle(type=OracleType.STATE_ASSERTION, expectation="响铃账本新增一行",
               confidence=0.8),
        Oracle(type=OracleType.MANUAL, expectation="主观听感连续",
               confidence=0.8),
    ]
    md = render_case_markdown(case, oracles, reviewed_by="expert-zhang",
                              decided_at=_NOW)
    assert "### TC-N2 只有步骤没有预期（FR-1） P1" in md
    assert "**前置**：" in md and "- 闹钟权限已授予" in md
    assert "1. 打开 App" in md and "2. 触发闹钟" in md
    assert "reviewer=expert-zhang" in md
    for tag, text in [
        ("【UI 断言】", "全屏横幅接管"),
        ("【日志断言】", "`os_log` 记录响铃"),
        ("【状态断言】", "响铃账本新增一行"),
        ("【人工确认】", "主观听感连续"),
    ]:
        assert f"- {tag}{text}" in md


def test_render_round_trips_through_parser_losslessly():
    case = _case()
    oracles = [
        Oracle(type=OracleType.UI_ASSERTION, expectation="全屏横幅接管",
               source_refs=[_FR1_URN], confidence=1.0),
        Oracle(type=OracleType.STATE_ASSERTION, expectation="响铃账本新增一行",
               source_refs=[_FR1_URN], confidence=0.8),
    ]
    md = render_case_markdown(case, oracles, reviewed_by="expert-zhang",
                              decided_at=_NOW)
    parsed = parse_case_markdown(md, doc_external_id="cases/writeback/TC-N2.md")
    assert len(parsed.cases) == 1
    rebuilt = parsed.cases[0]
    assert rebuilt.external_id == "TC-N2"
    assert rebuilt.priority == "P1"
    assert rebuilt.tags == ["FR-1"]
    assert rebuilt.steps == ["打开 App", "触发闹钟"]
    assert rebuilt.oracle_missing is False
    assert [(o.type, o.expectation) for o in rebuilt.oracles] == [
        (OracleType.UI_ASSERTION, "全屏横幅接管"),
        (OracleType.STATE_ASSERTION, "响铃账本新增一行"),
    ]


# ── 幂等键 ──────────────────────────────────────────────────────────────────

def test_idempotency_key_content_addressed():
    md1 = render_case_markdown(
        _case(), [Oracle(type=OracleType.MANUAL, expectation="a")],
        reviewed_by="r", decided_at=_NOW)
    md2 = render_case_markdown(
        _case(), [Oracle(type=OracleType.MANUAL, expectation="a")],
        reviewed_by="r", decided_at=_NOW)
    md3 = render_case_markdown(
        _case(), [Oracle(type=OracleType.MANUAL, expectation="b")],
        reviewed_by="r", decided_at=_NOW)
    k1 = outbox_idempotency_key(_CASE_URN, md1)
    assert k1 == outbox_idempotency_key(_CASE_URN, md2)
    assert k1 != outbox_idempotency_key(_CASE_URN, md3)
    assert k1.startswith("case-writeback:cases/synthetic-cases.md#TC-N2#")
