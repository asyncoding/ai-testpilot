"""核心模型/URN/写策略的无宿主单测（docs/08 FR-CFG-003、FR-WR-001）。"""

import pytest

from testpilot_core.enums import ActionLevel, ApprovalDecision
from testpilot_core.errors import PolicyDeniedError
from testpilot_core.models import Approval
from testpilot_core.policy import assert_writable
from testpilot_core.urn import make_urn, parse_urn


def test_urn_roundtrip_with_and_without_version():
    urn = make_urn("jira", "issue", "TKT-1", "42")
    assert urn == "urn:testpilot:jira:issue:TKT-1@42"
    assert parse_urn(urn) == ("jira", "issue", "TKT-1", "42")

    bare = make_urn("git", "commit", "abc")
    assert parse_urn(bare) == ("git", "commit", "abc", None)


def test_urn_rejects_garbage():
    with pytest.raises(ValueError):
        parse_urn("http://not-a-urn")


def _approval(level: ActionLevel, decision: ApprovalDecision = ApprovalDecision.APPROVED) -> Approval:
    return Approval(
        approval_id="ap-1",
        action_level=level,
        target_system="tickets",
        request_summary="create issue",
        decision=decision,
    )


def test_policy_read_and_internal_draft_pass_without_approval():
    assert_writable(ActionLevel.R0_READ, None)
    assert_writable(ActionLevel.R1_INTERNAL_DRAFT, None)


def test_policy_r2_requires_matching_approval():
    with pytest.raises(PolicyDeniedError):
        assert_writable(ActionLevel.R2_REVIEWED_WRITE, None)
    with pytest.raises(PolicyDeniedError):
        assert_writable(ActionLevel.R2_REVIEWED_WRITE, _approval(ActionLevel.R3_APPROVED_EXTERNAL))
    assert_writable(ActionLevel.R2_REVIEWED_WRITE, _approval(ActionLevel.R2_REVIEWED_WRITE))


def test_policy_r4_always_forbidden_and_kill_switch_blocks_r3():
    with pytest.raises(PolicyDeniedError):
        assert_writable(ActionLevel.R4_FORBIDDEN, _approval(ActionLevel.R4_FORBIDDEN))
    with pytest.raises(PolicyDeniedError):
        assert_writable(ActionLevel.R3_APPROVED_EXTERNAL, _approval(ActionLevel.R3_APPROVED_EXTERNAL), global_kill_switch=True)
