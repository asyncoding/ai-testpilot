"""事件信封契约纯逻辑（docs/04 §8 标准化事件清单，FR-CON-005）。

零 IO、零三方依赖（import-lint 守门）：事件类型闭集、信封校验与死信原因码。
``EventEnvelope`` 的字段类型由 pydantic 模型兜底，本模块补的是业务契约——
类型白名单与必填非空，让 event_dispatch 在落库前就能把坏事件分类进死信，
而不是静默丢弃（FR-CON-005 AC2）。
"""

from __future__ import annotations

from .errors import TestPilotError

# docs/04 §8 清单 11 行，斜杠合并项展开后共 14 个独立 event_type；
# 顺序即文档表格顺序，冻结为闭集，新类型必须先改文档再改这里。
EVENT_TYPES: frozenset[str] = frozenset({
    "CommitPushed",
    "MergeRequestOpened",
    "MergeRequestUpdated",
    "MergeRequestMerged",
    "PipelineFinished",
    "TestCaseCreated",
    "TestCaseUpdated",
    "RequirementPublished",
    "TicketCreated",
    "TicketClosed",
    "IncidentClosed",
    "DeploymentFinished",
    "TestRunFinished",
    "ModelFeedbackSubmitted",
})

# 死信原因码闭集（dead_letters.source 统一加 event-dispatch 前缀）
DEAD_REASON_UNKNOWN_TYPE = "unknown_event_type"
DEAD_REASON_MISSING_FIELD = "missing_field"
DEAD_REASON_PARSE_ERROR = "parse_error"
DEAD_REASON_HANDLER_ERROR = "handler_error"


class EventContractError(TestPilotError):
    """事件信封不满足 docs/04 §8 契约；``reason`` 取死信原因码闭集。"""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


# 信封十字段中的业务必填项。version 有默认值；actor 文档允许空；
# payload_uri/payload 本身就可为空（docs/04 §8 信封定义）。
_REQUIRED_NONEMPTY: tuple[str, ...] = (
    "event_id", "source_system", "project", "subject_ref", "idempotency_key",
)


def validate_event(event) -> None:
    """校验已解析的 EventEnvelope；不合法抛 :class:`EventContractError`。

    两条规则：event_type 必须在 docs/04 §8 闭集内；五个业务必填字段必须是
    非空白字符串。信封的时间戳/结构合法性由 pydantic 在解析阶段保证。
    """
    if event.event_type not in EVENT_TYPES:
        raise EventContractError(
            DEAD_REASON_UNKNOWN_TYPE,
            f"event_type={event.event_type!r} 不在 docs/04 §8 闭集"
            f"（共 {len(EVENT_TYPES)} 类）",
        )
    for field_name in _REQUIRED_NONEMPTY:
        value = getattr(event, field_name)
        if not isinstance(value, str) or not value.strip():
            raise EventContractError(
                DEAD_REASON_MISSING_FIELD,
                f"必填字段 {field_name} 为空或非字符串"
                f"（event_id={getattr(event, 'event_id', '?')!r}）",
            )
