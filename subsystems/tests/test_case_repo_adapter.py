"""MarkdownCaseRepository 测试（S2.5 FR-TD-004 / FR-WR-001 适配器层）。

策略守卫、内容寻址幂等、原子落盘、写根隔离与 S2.1 解析器 round-trip 全部在
临时目录内验证；审批对象走内存 provider，不依赖数据库。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import pytest

from testpilot_adapters.case_repo import MANIFEST_NAME, MarkdownCaseRepository
from testpilot_core.approval import (
    CASE_REPO_TARGET_SYSTEM,
    CaseDecisionRequest,
    SuggestionDecision,
    SuggestionView,
    outbox_idempotency_key,
    plan_case_decision,
)
from testpilot_core.enums import (
    ActionLevel,
    ApprovalDecision,
    OracleType,
)
from testpilot_core.errors import AdapterUnavailableError, PolicyDeniedError
from testpilot_core.models import (
    Approval,
    CaseSelector,
    OutboxRecord,
    TestCase,
)
from testpilot_core.oracle_mining import MinedOracle
from testpilot_core.ports import TestCaseRepositoryPort

_NOW = datetime(2026, 11, 9, 8, 30, tzinfo=timezone.utc)
_CASE_URN = "urn:testpilot:fs-cases:test_case:cases/synthetic-cases.md#TC-N2"
_FR1_URN = "urn:testpilot:fs-reqs:requirement:req.md#FR-1"


def _case(**overrides) -> TestCase:
    base = dict(
        urn=_CASE_URN, external_id="TC-N2", title="只有步骤没有预期",
        steps=["打开 App", "触发闹钟"], tags=["FR-1"],
        module="模块 A 叙事体", priority="P1",
    )
    base.update(overrides)
    return TestCase(**base)


def _plan(*, expectation: str = "落库一条 stopped 记录",
          reviewer: str = "expert-zhang"):
    case = _case()
    mined = MinedOracle(
        type=OracleType.STATE_ASSERTION, expectation=expectation,
        source_kind="requirement_ac", provenance="deterministic",
        source_refs=[_FR1_URN], confidence=0.5)
    view = SuggestionView(
        urn="urn:testpilot:fs-cases:oracle_suggestion:TC-N2#s1",
        case_urn=_CASE_URN, external_id="TC-N2",
        status="pending", oracle=mined)
    req = CaseDecisionRequest(
        case_urn=_CASE_URN, decision=ApprovalDecision.APPROVED,
        reviewer=reviewer, reason="评审通过",
        items=[SuggestionDecision(suggestion_urn=view.urn, action="adopt")])
    return case, view, plan_case_decision(req, case, [view], now=_NOW)


def _approved(approval_id: str) -> Approval:
    return Approval(
        approval_id=approval_id,
        action_level=ActionLevel.R2_REVIEWED_WRITE,
        target_system=CASE_REPO_TARGET_SYSTEM,
        request_summary="用例回写",
        decision=ApprovalDecision.APPROVED,
        reviewer="expert-zhang",
        decided_at=_NOW,
    )


def _repo(tmp_path, *, enabled: bool, approvals: dict[str, Approval] | None = None):
    approvals = approvals or {}
    return MarkdownCaseRepository(
        tmp_path / "case-writeback",
        approval_provider=lambda aid: approvals.get(aid),
        kill_switch=lambda: not enabled,
    )


# ── 策略守卫：未批/拒批/停用一律不产生文件 ──────────────────────────────────

def test_kill_switch_blocks_write_and_leaves_empty_root(tmp_path):
    _, _, plan = _plan()
    repo = _repo(tmp_path, enabled=False, approvals={plan.approval.approval_id: plan.approval})
    with pytest.raises(PolicyDeniedError, match="kill switch"):
        asyncio.run(repo.write_approved_record(plan.outbox))
    assert list((tmp_path / "case-writeback").rglob("*")) == []


def test_missing_and_rejected_approval_denied(tmp_path):
    _, _, plan = _plan()
    repo = _repo(tmp_path, enabled=True)  # provider 有库但无该审批
    with pytest.raises(PolicyDeniedError, match="未经人审批准"):
        asyncio.run(repo.write_approved_record(plan.outbox))

    rejected = plan.approval.model_copy(
        update={"decision": ApprovalDecision.REJECTED})
    repo2 = _repo(tmp_path, enabled=True,
                  approvals={rejected.approval_id: rejected})
    with pytest.raises(PolicyDeniedError):
        asyncio.run(repo2.write_approved_record(plan.outbox))
    assert list((tmp_path / "case-writeback").rglob("*")) == []


def test_target_system_mismatch_rejects_before_policy(tmp_path):
    _, _, plan = _plan()
    repo = _repo(tmp_path, enabled=False,
                 approvals={plan.approval.approval_id: plan.approval})
    record = plan.outbox.model_copy(update={"target_system": "json-tickets"})
    with pytest.raises(AdapterUnavailableError, match="目标系统"):
        asyncio.run(repo.write_approved_record(record))
    assert list((tmp_path / "case-writeback").rglob("*")) == []


# ──  happy path：落盘内容即审批字节，可无损回读 ─────────────────────────────

def test_write_happy_path_manifest_and_roundtrip(tmp_path):
    _, _, plan = _plan()
    repo = _repo(tmp_path, enabled=True,
                 approvals={plan.approval.approval_id: plan.approval})

    ref = asyncio.run(repo.write_approved_record(plan.outbox))
    rel = "cases/synthetic-cases/TC-N2.md"
    assert ref == f"{CASE_REPO_TARGET_SYSTEM}:{rel}"

    target = tmp_path / "case-writeback" / rel
    assert target.read_text(encoding="utf-8") == plan.markdown  # 批准的字节即落盘字节

    manifest = json.loads(
        (tmp_path / "case-writeback" / MANIFEST_NAME).read_text(encoding="utf-8"))
    entry = manifest["entries"][plan.idempotency_key]
    assert entry["external_ref"] == ref
    assert entry["path"] == rel
    assert entry["approval_id"] == plan.approval.approval_id
    assert entry["case_external_id"] == "TC-N2"
    assert entry["doc_external_id"] == "cases/synthetic-cases.md"
    assert entry["written_at"]

    # S2.1 解析器无损回读：显式类型标签保住类型，不再走关键词猜测
    cases = asyncio.run(repo.list_cases(CaseSelector()))
    assert len(cases) == 1
    rebuilt = cases[0]
    assert rebuilt.external_id == "TC-N2"
    assert [(o.type, o.expectation) for o in rebuilt.oracles] == [
        (OracleType.STATE_ASSERTION, "落库一条 stopped 记录")]
    assert rebuilt.steps == ["打开 App", "触发闹钟"]
    # 原子落盘不留 tmp 残骸
    assert list((tmp_path / "case-writeback").rglob("*.tmp")) == []


# ── 幂等：同键重放不重复写；异键新写不合并 ─────────────────────────────────

def test_replay_same_key_returns_same_ref_without_duplicate(tmp_path):
    _, _, plan = _plan()
    repo = _repo(tmp_path, enabled=True,
                 approvals={plan.approval.approval_id: plan.approval})

    first = asyncio.run(repo.write_approved_record(plan.outbox))
    second = asyncio.run(repo.write_approved_record(plan.outbox))
    assert first == second

    root = tmp_path / "case-writeback"
    assert list(root.glob("**/*.md")) == [root / "cases/synthetic-cases/TC-N2.md"]
    manifest = json.loads((root / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert len(manifest["entries"]) == 1


def test_distinct_content_produces_distinct_files(tmp_path):
    _, _, plan1 = _plan(expectation="落库一条 stopped 记录")
    _, _, plan2 = _plan(expectation="落库两条 stopped 记录")
    approvals = {
        plan1.approval.approval_id: plan1.approval,
        plan2.approval.approval_id: plan2.approval,
    }
    repo = _repo(tmp_path, enabled=True, approvals=approvals)

    ref1 = asyncio.run(repo.write_approved_record(plan1.outbox))
    ref2 = asyncio.run(repo.write_approved_record(plan2.outbox))
    # external_ref 路径寻址：同用例两版共享同一制品位置；区分写的是幂等键
    assert ref1 == ref2
    assert plan1.idempotency_key != plan2.idempotency_key

    root = tmp_path / "case-writeback"
    # 同用例同路径：后写覆盖文件（最新版本生效），但两次写都在 manifest 留痕
    assert len(list(root.glob("**/*.md"))) == 1
    assert (root / "cases/synthetic-cases/TC-N2.md").read_text(
        encoding="utf-8") == plan2.markdown
    manifest = json.loads((root / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert set(manifest["entries"]) == {
        plan1.idempotency_key, plan2.idempotency_key}


# ── 写根隔离：穿越路径响亮失败 ──────────────────────────────────────────────

def test_path_traversal_is_rejected(tmp_path):
    repo = _repo(tmp_path, enabled=True, approvals={"ap-ok": _approved("ap-ok")})
    evil = OutboxRecord(
        idempotency_key="case-writeback:evil#TC-E#deadbeef0000",
        action_level=ActionLevel.R2_REVIEWED_WRITE,
        target_system=CASE_REPO_TARGET_SYSTEM,
        request_body={
            "doc_external_id": "../escape.md",
            "case_external_id": "TC-E",
            "markdown": "# 越界",
        },
        approval_id="ap-ok",
    )
    with pytest.raises(AdapterUnavailableError, match="越出写根"):
        asyncio.run(repo.write_approved_record(evil))
    assert not (tmp_path / "escape.md").exists()
    assert not (tmp_path / "case-writeback" / MANIFEST_NAME).exists()


# ── 端口 canonical 方法与选择器过滤 ─────────────────────────────────────────

def test_port_method_write_approved_case_and_protocol_check(tmp_path):
    repo = _repo(tmp_path, enabled=True, approvals={"ap-1": _approved("ap-1")})
    assert isinstance(repo, TestCaseRepositoryPort)  # runtime_checkable 结构契约

    case = _case()
    ref = asyncio.run(repo.write_approved_case(case, "ap-1"))
    root = tmp_path / "case-writeback"
    target = root / "cases/synthetic-cases/TC-N2.md"
    assert target.exists() and ref.endswith("cases/synthetic-cases/TC-N2.md")

    # 幂等键与 core 内容寻址一致，重放不翻倍
    text = target.read_text(encoding="utf-8")
    key = outbox_idempotency_key(case.urn, text)
    manifest = json.loads((root / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert key in manifest["entries"]
    # 同 case 重渲染文本确定性 → 同键，重放走 manifest 命中不翻倍
    assert asyncio.run(repo.write_approved_case(case, "ap-1")) == ref
    manifest2 = json.loads((root / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert len(manifest2["entries"]) == 1


def test_list_cases_selector_filters(tmp_path):
    approvals = {"ap-1": _approved("ap-1")}
    repo = _repo(tmp_path, enabled=True, approvals=approvals)
    case_a = _case()
    case_b = _case(
        urn="urn:testpilot:fs-cases:test_case:cases/other.md#TC-B2",
        external_id="TC-B2", title="另案", tags=["FR-2"], priority="P2")
    asyncio.run(repo.write_approved_case(case_a, "ap-1"))
    asyncio.run(repo.write_approved_case(case_b, "ap-1"))

    all_cases = asyncio.run(repo.list_cases(CaseSelector()))
    assert {c.external_id for c in all_cases} == {"TC-N2", "TC-B2"}

    by_id = asyncio.run(repo.list_cases(CaseSelector(test_ids=["TC-B2"])))
    assert [c.external_id for c in by_id] == ["TC-B2"]

    by_tag = asyncio.run(repo.list_cases(CaseSelector(tags=["FR-1"])))
    assert [c.external_id for c in by_tag] == ["TC-N2"]
