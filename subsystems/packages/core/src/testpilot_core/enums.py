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


# ── 混合执行与人工闸门（docs/01，hybrid-plan-v1，schema v15 零 DDL）──────────


class StepActor(StrEnum):
    """混合计划步骤的执行方；闭集，未知 actor 计划校验期响亮失败。"""

    AUTO_XCODEBUILD = "auto:xcodebuild"
    AUTO_DH_AX = "auto:dh-ax"
    AUTO_DEVICECTL = "auto:devicectl"
    HUMAN_ACTION = "human:action"
    HUMAN_VERDICT = "human:verdict"


class StepStatus(StrEnum):
    """单步状态（存会话 JSON 制品，不进数据库表）。"""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"
    SKIPPED = "skipped"


class SessionStatus(StrEnum):
    """混合会话状态：在 run 五终态之外补 awaiting_human/blocked 两个挂起态。

    子 run 仍走既有 RunStatus 状态机；本状态只描述会话层游标，存 raw_json
    制品，因此不改 schema、不碰 RunStatus 迁移闭集。
    """

    PENDING = "pending"
    RUNNING = "running"
    AWAITING_HUMAN = "awaiting_human"
    BLOCKED = "blocked"
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    CANCELED = "canceled"


class GateStatus(StrEnum):
    """人工闸门单条记录的状态（文件态 gate store）。"""

    PENDING = "pending"
    RESPONDED = "responded"
    EXPIRED = "expired"
    CANCELED = "canceled"



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


# ── S1.4 问答工作流（docs/08 FR-QA-001～004）────────────────────────────────


class QaIntent(StrEnum):
    """问答意图分类。误判代价不对称：拿不准时兜底归 code，宁多验不漏验。"""

    CODE = "code"  # 代码/符号/实现行为类：强制 live-verify
    INCIDENT = "incident"  # 事故/复盘/历史变迁：历史证词逐字并列
    HOWTO = "howto"  # 操作/配置/步骤类
    CONCEPT = "concept"  # 概念/术语类
    OUT_OF_SCOPE = "out_of_scope"  # 闲聊/通用问答：本产品不做，直接收口


class QaStatus(StrEnum):
    ANSWERED = "answered"
    UNKNOWN = "unknown"  # 证据不足/引用校验耗尽/模型不可用：显式未知，不编造
    DENIED = "denied"  # 权限策略拒绝
    ERROR = "error"  # 预算耗尽等基础设施级异常


class VersionState(StrEnum):
    """引用三态（docs/08 FR-QA-002 AC3），全系统只允许这三个合法字面量。"""

    CONSISTENT = "consistent"  # 与当前版本一致（verify unchanged / 非代码新鲜块）
    VERIFIED_AT = "verified_at"  # 索引已陈旧，当场读 HEAD 刷新后验证于新版本
    UNKNOWN = "unknown"  # 校验降级/证据消失：不得当成一致输出
