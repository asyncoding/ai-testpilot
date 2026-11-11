"""事件信封契约纯逻辑测试（FR-CON-005，core 零 IO）。"""

from datetime import datetime, timezone

import pytest

from testpilot_core.errors import TestPilotError
from testpilot_core.event_contract import (
    DEAD_REASON_MISSING_FIELD,
    DEAD_REASON_UNKNOWN_TYPE,
    EVENT_TYPES,
    EventContractError,
    validate_event,
)
from testpilot_core.models import EventEnvelope


def _envelope(**overrides) -> EventEnvelope:
    base = dict(
        event_id="evt-1",
        event_type="CommitPushed",
        source_system="json-git",
        occurred_at=datetime(2026, 11, 11, 8, 0, tzinfo=timezone.utc),
        project="alarmkit",
        subject_ref="deadbeef",
        idempotency_key="idem-1",
    )
    base.update(overrides)
    return EventEnvelope(**base)


def test_event_type_closed_set_matches_docs_04_section_8():
    # docs/04 §8 清单 11 行展开为 14 个独立类型；闭集冻结，新增必须先改文档
    assert len(EVENT_TYPES) == 14
    assert {
        "CommitPushed", "MergeRequestOpened", "MergeRequestUpdated",
        "MergeRequestMerged", "PipelineFinished", "TestCaseCreated",
        "TestCaseUpdated", "RequirementPublished", "TicketCreated",
        "TicketClosed", "IncidentClosed", "DeploymentFinished",
        "TestRunFinished", "ModelFeedbackSubmitted",
    } == EVENT_TYPES


def test_all_known_event_types_pass():
    for event_type in sorted(EVENT_TYPES):
        validate_event(_envelope(event_id=f"evt-{event_type}", event_type=event_type))


def test_unknown_event_type_is_contract_error_with_closed_reason():
    with pytest.raises(EventContractError) as exc_info:
        validate_event(_envelope(event_type="SomethingHappened"))
    assert exc_info.value.reason == DEAD_REASON_UNKNOWN_TYPE
    assert isinstance(exc_info.value, TestPilotError)


@pytest.mark.parametrize(
    "field_name",
    ["event_id", "source_system", "project", "subject_ref", "idempotency_key"],
)
def test_required_fields_must_be_nonblank(field_name):
    with pytest.raises(EventContractError) as exc_info:
        validate_event(_envelope(**{field_name: "   "}))
    assert exc_info.value.reason == DEAD_REASON_MISSING_FIELD
    assert field_name in str(exc_info.value)


def test_optional_fields_allowed_empty():
    # actor 文档允许空、payload/payload_uri 本身可空：不得误杀
    validate_event(_envelope(actor="", payload={}, payload_uri=None))
