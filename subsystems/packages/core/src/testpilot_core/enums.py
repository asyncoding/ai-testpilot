"""核心枚举。"""

from __future__ import annotations

from enum import StrEnum


class AuthorityLevel(StrEnum):
    """知识源权威等级（docs/06 质量分排序用）。"""

    L0_REALTIME = "L0_realtime"  # 实时联邦，不缓存结论
    L1_AUTHORITATIVE = "L1_authoritative"  # 规范/官方文档
    L2_EXPERIENCE = "L2_experience"  # 事故/缺陷/个人经验（历史证词）
    L3_RULE = "L3_rule"  # 术语表/prompt/golden/few-shot


class OracleType(StrEnum):
    UI_ASSERTION = "ui_assertion"
    LOG_ASSERTION = "log_assertion"
    STATE_ASSERTION = "state_assertion"
    MANUAL = "manual"


class ChangeType(StrEnum):
    ADDED = "added"
    REMOVED = "removed"
    SIGNATURE_CHANGED = "signature_changed"
    MODIFIED = "modified"


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    CANCELED = "canceled"
    TIMEOUT = "timeout"


class CaseStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    ERROR = "error"


class FailureCategory(StrEnum):
    """失败归因候选类别（docs/08 FR-EX-007）。"""

    REAL_DEFECT = "real_defect"
    CASE_OUTDATED = "case_outdated"
    FLAKY = "flaky"
    ENVIRONMENT = "environment"
    PRODUCT_CHANGE = "product_change"
    NEEDS_HUMAN = "needs_human"


class ActionLevel(StrEnum):
    """外部副作用分级（docs/04 §7.2 / docs/08 FR-WR-001）。"""

    R0_READ = "R0"
    R1_INTERNAL_DRAFT = "R1"
    R2_REVIEWED_WRITE = "R2"
    R3_APPROVED_EXTERNAL = "R3"
    R4_FORBIDDEN = "R4"


class OutboxStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    CONFIRMED = "confirmed"
    FAILED = "failed"


class ApprovalDecision(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class FeedbackKind(StrEnum):
    USEFUL = "useful"
    NOT_USEFUL = "not_useful"


class FeedbackReason(StrEnum):
    NOT_RETRIEVED = "not_retrieved"
    STALE_REFERENCE = "stale_reference"
    OFF_TOPIC = "off_topic"
    FACTUAL_ERROR = "factual_error"
    OTHER = "other"


class ArtifactType(StrEnum):
    XCRESULT = "xcresult"
    JUNIT_XML = "junit_xml"
    SCREENSHOT = "screenshot"
    VIDEO = "video"
    LOG_BUNDLE = "log_bundle"
    OTHER = "other"


class StaleStatus(StrEnum):
    FRESH = "fresh"
    STALE = "stale"
    REINDEXED = "reindexed"
    SUPERSEDED = "superseded"
