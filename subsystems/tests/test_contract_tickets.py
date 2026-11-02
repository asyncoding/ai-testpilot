"""json 工单适配器契约测试（TicketPort，docs/08 FR-CON-004）。"""

import json
from pathlib import Path

import pytest

from testpilot_adapters.json_tickets import JsonTicketAdapter
from testpilot_core.enums import ActionLevel, ApprovalDecision
from testpilot_core.errors import PolicyDeniedError
from testpilot_core.models import Approval, CommentDraft, IssueDraft
from testpilot_core.urn import parse_urn

FIXTURES = Path(__file__).parent / "fixtures"


def _adapter(tmp_path, *, approvals=None, kill_switch=lambda: False):
    return JsonTicketAdapter(
        read_dirs=[FIXTURES / "tickets"],
        write_dir=tmp_path / "ticket-store",
        approval_provider=approvals,
        kill_switch=kill_switch,
    )


async def test_search_returns_canonical_issues(tmp_path):
    adapter = _adapter(tmp_path)
    issues = await adapter.search("侧边键", project="demo")
    assert len(issues) == 1
    issue = issues[0]
    assert issue.external_id == "TKT-1001"
    system, _, ext, version = parse_urn(issue.urn)
    assert (system, ext, version) == ("json-tickets", "TKT-1001", "7")

    all_issues = await adapter.search("", project="demo")
    assert {i.external_id for i in all_issues} == {"TKT-1001", "TKT-1002"}


async def test_create_requires_approval_and_is_idempotent(tmp_path):
    no_approval = _adapter(tmp_path)
    draft = IssueDraft(title="t", symptom="s")
    with pytest.raises(PolicyDeniedError):
        await no_approval.create(draft, idempotency_key="k1", approval_id="ap-missing")

    approved = Approval(
        approval_id="ap-1",
        action_level=ActionLevel.R3_APPROVED_EXTERNAL,
        target_system="json-tickets",
        request_summary="create",
        decision=ApprovalDecision.APPROVED,
    )
    adapter = _adapter(
        tmp_path,
        approvals={"ap-1": approved}.get,
    )
    urn1 = await adapter.create(draft, idempotency_key="k1", approval_id="ap-1")
    urn2 = await adapter.create(draft, idempotency_key="k1", approval_id="ap-1")
    assert urn1 == urn2
    written = list((tmp_path / "ticket-store").glob("k1.json"))
    assert len(written) == 1
    assert json.loads(written[0].read_text())["approval_id"] == "ap-1"


async def test_comment_needs_r2_and_kill_switch_blocks(tmp_path):
    approved_r2 = Approval(
        approval_id="ap-2",
        action_level=ActionLevel.R2_REVIEWED_WRITE,
        target_system="json-tickets",
        request_summary="comment",
        decision=ApprovalDecision.APPROVED,
    )
    adapter = _adapter(tmp_path, approvals={"ap-2": approved_r2}.get)
    urn = await adapter.add_comment(
        "urn:testpilot:json-tickets:issue:TKT-1001@7",
        CommentDraft(body="归因：用例过期"),
        idempotency_key="c1",
        approval_id="ap-2",
    )
    assert "comment" in urn

    blocked = _adapter(tmp_path, approvals={"ap-2": approved_r2}.get, kill_switch=lambda: True)
    with pytest.raises(PolicyDeniedError):
        await blocked.add_comment(
            "urn:testpilot:json-tickets:issue:TKT-1001@7",
            CommentDraft(body="x"),
            idempotency_key="c2",
            approval_id="ap-2",
        )
