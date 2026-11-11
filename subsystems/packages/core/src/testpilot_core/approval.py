"""用例草稿人审与回写纯逻辑（docs/08 FR-TD-004 / FR-WR-001，S2.5）。

零 IO、零三方：吃 S2.4 的 ``oracle_suggestions`` 草稿与 S2.1 的 ``TestCase``，
产出审批队列视图与「一次人审决定」的全部落库计划。纪律与 S2.1~S2.4 一致——
纯函数先把账算清楚，持久化由 workers 在单事务内完成：

- ``build_review_queue``：pending 草稿按用例分组，已评审（approved/rejected）
  的草稿不再出现在队列；
- ``plan_case_decision``：采纳/修改/拒绝三种动作的 fail-closed 校验，产出
  Approval + OutboxRecord 草稿 + 偏好样本 + 草稿状态更新四件套；
- confirmed（采纳或修改后接受）的 oracle 置信度 +0.3 且封顶 1.0——模型自己
  不能标高置信，只有人审能抬（FR-TD-004 原文「quality +0.3 仅对 confirmed」）；
- ``render_case_markdown``：回写 Markdown 与 S2.1 解析器同构，显式类型标签
  （【UI 断言】等）保证重新摄入时类型无损 round-trip；
- 回写幂等键内容寻址：同用例同渲染文本 → 同键，outbox 重放不产生重复文件。

AC1 守卫不在本模块（本模块不碰库），但计划里 **只有 outbox/审批/建议状态**，
正式 ``test_cases`` 表的零触碰由 workers 层测试机器断言。
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from .enums import ActionLevel, ApprovalDecision, OracleType, OutboxStatus
from .models import (
    Approval,
    Oracle,
    OutboxRecord,
    PreferenceSample,
    TestCase,
    utcnow,
)
from .oracle_mining import MinedOracle
from .testcase import EXPLICIT_TYPE_LABELS
from .urn import make_urn, parse_urn

# 回写目标系统标识：Approval/Outbox 的 target_system 都用它，派发器据此选适配器
CASE_REPO_TARGET_SYSTEM = "markdown-case-repo"
# S4.4 FR-EX-008：CI 闭环草稿（建工单/评论）的目标系统，对齐 json-tickets URN 命名空间。
# 派发器尚未接 TicketPort 写路由：pending 记录在 dispatch_pending_outbox 计
# skipped_unsupported 并保留 pending，即 FR-WR-003 的 dry-run 默认姿态。
TICKET_TARGET_SYSTEM = "json-tickets"
# FR-TD-004：人审 confirmed 后置信度加成，仅对被采纳/修改接受的条目生效
CONFIRMED_CONFIDENCE_BUMP = 0.3
_CONFIDENCE_CEILING = 1.0

SUGGESTION_PENDING = "pending"
SUGGESTION_APPROVED = "approved"
SUGGESTION_REJECTED = "rejected"

ReviewAction = Literal["adopt", "modify", "reject"]
# 一次评审允许覆盖的建议状态闭集（防御拼写漂移，状态串同时落 SQL 列）
_REVIEWED_STATUSES = frozenset({SUGGESTION_APPROVED, SUGGESTION_REJECTED})


class ApprovalValidationError(ValueError):
    """人审请求语义非法（幽灵 URN、覆盖不全、动作与结论矛盾等），HTTP 层映射 422。"""


# ── 队列视图 ────────────────────────────────────────────────────────────────

class SuggestionView(BaseModel):
    """oracle_suggestions 一行的 core 视图（store 行在 workers 层转这个模型）。"""

    urn: str
    case_urn: str
    external_id: str
    status: str
    exploratory: bool = False
    oracle: MinedOracle


class ReviewQueueItem(BaseModel):
    """一条用例的待审卡片：用例元信息 + 其全部 pending 草稿。"""

    case_urn: str
    external_id: str
    title: str
    priority: str
    module: str
    exploratory: bool
    oracle_missing: bool
    suggestions: list[SuggestionView] = Field(default_factory=list)


def build_review_queue(
    suggestions: list[SuggestionView], cases: list[TestCase]
) -> list[ReviewQueueItem]:
    """pending 草稿按用例分组；引用了不存在用例的草稿是账本不一致，响亮失败。"""
    case_map = {case.urn: case for case in cases}
    grouped: dict[str, list[SuggestionView]] = {}
    for suggestion in suggestions:
        if suggestion.status != SUGGESTION_PENDING:
            continue
        if suggestion.case_urn not in case_map:
            raise ApprovalValidationError(
                f"草稿引用了不存在的用例: {suggestion.urn} → {suggestion.case_urn}"
            )
        grouped.setdefault(suggestion.case_urn, []).append(suggestion)

    items: list[ReviewQueueItem] = []
    for case_urn, pending in grouped.items():
        case = case_map[case_urn]
        items.append(
            ReviewQueueItem(
                case_urn=case.urn,
                external_id=case.external_id,
                title=case.title,
                priority=case.priority,
                module=case.module,
                exploratory=any(s.exploratory for s in pending),
                oracle_missing=case.oracle_missing,
                suggestions=sorted(pending, key=lambda s: s.urn),
            )
        )
    return sorted(items, key=lambda item: item.external_id)


# ── 人审决定 ────────────────────────────────────────────────────────────────

class SuggestionDecision(BaseModel):
    suggestion_urn: str
    action: ReviewAction
    # modify 时可改类型/期望；adopt/reject 必须留空（携带即报错，防客户端静默改写）
    oracle_type: OracleType | None = None
    expectation: str | None = None


class CaseDecisionRequest(BaseModel):
    case_urn: str
    decision: ApprovalDecision
    reviewer: str = Field(min_length=1)
    reason: str = ""
    items: list[SuggestionDecision] = Field(min_length=1)


class SuggestionUpdate(BaseModel):
    urn: str
    status: str
    reason: str = ""


class CaseDecisionPlan(BaseModel):
    """一次人审的完整落库计划；workers 在单事务内按此写四件套。"""

    approval: Approval
    outbox: OutboxRecord | None
    preference_samples: list[PreferenceSample] = Field(default_factory=list)
    suggestion_updates: list[SuggestionUpdate] = Field(default_factory=list)
    final_oracles: list[Oracle] = Field(default_factory=list)
    markdown: str
    idempotency_key: str


def confirmed_confidence(original: float) -> float:
    """人审 confirmed 加成：+0.3 且封顶 1.0（FR-TD-004）。"""
    return min(_CONFIDENCE_CEILING, round(original + CONFIRMED_CONFIDENCE_BUMP, 6))


def field_diff(original: MinedOracle, revised_type: OracleType,
               revised_expectation: str) -> dict[str, dict[str, str]]:
    """字段级 diff（零三方）：只比较类型与期望文本，来源 URN 不允许人审改写。"""
    diff: dict[str, dict[str, str]] = {}
    if revised_type != original.type:
        diff["type"] = {"from": original.type.value, "to": revised_type.value}
    if revised_expectation != original.expectation:
        diff["expectation"] = {
            "from": original.expectation,
            "to": revised_expectation,
        }
    return diff


def _external_body(urn: str) -> str:
    """URN 反解出 external_id 段（如 cases/synthetic-cases.md#TC-N2）。"""
    return parse_urn(urn)[2]


def outbox_idempotency_key(case_urn: str, markdown: str) -> str:
    """内容寻址幂等键：同用例同回写文本 → 同键，重放不重复写文件。"""
    digest = hashlib.sha1(markdown.encode("utf-8")).hexdigest()[:12]
    return f"case-writeback:{_external_body(case_urn)}#{digest}"


def render_case_markdown(
    case: TestCase,
    oracles: list[Oracle],
    *,
    reviewed_by: str,
    decided_at: datetime | None = None,
) -> str:
    """渲染回写 Markdown（叙事体，与 S2.1 解析器同构，可无损重新摄入）。

    - 标题复刻 ``### TC-x 标题（FR 引用） P0`` 形态，FR tag/优先级可被解析还原；
    - 预期子弹带显式类型标签，类型不依赖关键词猜测；
    - 来源 URN 不进 Markdown（追溯关系留在 oracle_suggestions/outbox 请求体），
      保持文档对人类可读。
    """
    decided_at = decided_at or utcnow()
    lines: list[str] = [f"## {case.module or '人审回写用例'}", ""]

    heading = f"### {case.external_id} {case.title}".rstrip()
    if case.tags:
        heading += f"（{'，'.join(case.tags)}）"
    if case.priority:
        heading += f" {case.priority}"
    lines += [heading, ""]

    if case.preconditions:
        lines.append("**前置**：")
        lines.extend(f"- {item}" for item in case.preconditions)
        lines.append("")

    lines.append("**步骤**：")
    lines.extend(f"{idx}. {step}" for idx, step in enumerate(case.steps, start=1))
    lines.append("")

    # 夹注内禁用冒号：S2.1 标记正则以第一个 :/： 分段，夹注里出现冒号会把
    # 夹注正文误当成一条内联预期（红→绿取证于 test_render_round_trips…）
    date_part = decided_at.date().isoformat()
    lines.append(f"**预期**（人审回写 reviewer={reviewed_by} date={date_part}）：")
    for oracle in oracles:
        tag = f"【{EXPLICIT_TYPE_LABELS[oracle.type]}】"
        lines.append(f"- {tag}{oracle.expectation}")
    lines.append("")
    return "\n".join(lines)


def writeback_rel_path(doc_external_id: str, case_external_id: str) -> str:
    """回写文件相对写根的 POSIX 路径：文档去 ``.md`` 后缀成目录、每用例一文件。

    纯字符串函数（core 零 IO 红线）：适配器决定物理写根，本函数只定路径约定，
    回滚说明与 MarkdownCaseRepository 共用同一实现，防止两处路径漂移。
    """
    doc_dir = doc_external_id[:-3] if doc_external_id.endswith(".md") else doc_external_id
    return f"{doc_dir}/{case_external_id}.md"


def _rollback_note(doc_external_id: str, case_external_id: str,
                   idempotency_key: str) -> str:
    """FR-WR-001 AC1：每条 R2 记录必须带可执行的回滚说明。"""
    rel_path = writeback_rel_path(doc_external_id, case_external_id)
    return (
        f"回滚步骤：1) 删除写根目录 {rel_path} 及 manifest 中"
        f"幂等键 {idempotency_key} 的条目；2) 正式用例库 test_cases 不被审批流修改，"
        "无需回滚；3) 在审批页将对应草稿重置为 pending 可重新评审。"
    )


def plan_case_decision(
    request: CaseDecisionRequest,
    case: TestCase,
    suggestions: list[SuggestionView],
    *,
    now: datetime | None = None,
) -> CaseDecisionPlan:
    """把一次人审请求算成落库计划；任何语义疑问都 fail-closed 抛错。"""
    now = now or utcnow()
    if request.decision not in (ApprovalDecision.APPROVED, ApprovalDecision.REJECTED):
        raise ApprovalValidationError("decision 只允许 approved/rejected")
    if case.urn != request.case_urn:
        raise ApprovalValidationError(
            f"请求用例 {request.case_urn} 与实际用例 {case.urn} 不一致"
        )

    pending = {s.urn: s for s in suggestions if s.status == SUGGESTION_PENDING}
    if not pending:
        raise ApprovalValidationError(f"用例 {case.external_id} 没有待审草稿")

    item_map: dict[str, SuggestionDecision] = {}
    for item in request.items:
        if item.suggestion_urn in item_map:
            raise ApprovalValidationError(f"草稿被重复决策: {item.suggestion_urn}")
        if item.suggestion_urn not in pending:
            raise ApprovalValidationError(
                f"草稿不存在或已评审: {item.suggestion_urn}"
            )
        item_map[item.suggestion_urn] = item

    # 覆盖完整性：一次用例评审必须交代全部 pending 草稿，防静默遗漏
    missing = sorted(set(pending) - set(item_map))
    if missing:
        raise ApprovalValidationError(f"以下待审草稿未给出决策: {missing}")

    actions = {item.action for item in request.items}
    if request.decision == ApprovalDecision.REJECTED and actions != {"reject"}:
        raise ApprovalValidationError("整案拒绝时所有草稿动作必须都是 reject")
    if request.decision == ApprovalDecision.APPROVED and actions == {"reject"}:
        raise ApprovalValidationError("全部草稿被拒绝时决策应为 rejected 而非 approved")

    final_oracles: list[Oracle] = []
    updates: list[SuggestionUpdate] = []
    samples: list[PreferenceSample] = []
    action_log: list[dict[str, str]] = []

    for item in request.items:
        suggestion = pending[item.suggestion_urn]
        mined = suggestion.oracle
        action_log.append(
            {"urn": item.suggestion_urn, "action": item.action,
             "source_kind": mined.source_kind}
        )

        if item.action == "reject":
            updates.append(SuggestionUpdate(
                urn=item.suggestion_urn, status=SUGGESTION_REJECTED,
                reason=request.reason))
            continue

        if item.action == "adopt":
            if item.oracle_type is not None or item.expectation is not None:
                raise ApprovalValidationError(
                    f"adopt 动作不允许携带修改字段: {item.suggestion_urn}"
                )
            revised_type, revised_expectation = mined.type, mined.expectation
        else:  # modify
            if not item.expectation or not item.expectation.strip():
                raise ApprovalValidationError(
                    f"modify 动作必须提供非空 expectation: {item.suggestion_urn}"
                )
            revised_type = item.oracle_type or mined.type
            revised_expectation = item.expectation.strip()

        final_oracles.append(Oracle(
            type=revised_type,
            expectation=revised_expectation,
            source_refs=list(mined.source_refs),  # 来源 URN 人审不可改，追溯链不断
            confidence=confirmed_confidence(mined.confidence),
        ))
        updates.append(SuggestionUpdate(
            urn=item.suggestion_urn, status=SUGGESTION_APPROVED,
            reason=request.reason))

        if item.action == "modify":
            diff = field_diff(mined, revised_type, revised_expectation)
            revised = Oracle(
                type=revised_type, expectation=revised_expectation,
                source_refs=list(mined.source_refs),
                confidence=confirmed_confidence(mined.confidence),
            )
            sample_digest = hashlib.sha1(
                f"{item.suggestion_urn}|{revised_expectation}".encode("utf-8")
            ).hexdigest()[:10]
            samples.append(PreferenceSample(
                sample_id=make_urn(
                    "hitl", "preference_sample",
                    f"{case.external_id}#{sample_digest}"),
                case_urn=case.urn,
                suggestion_urn=item.suggestion_urn,
                reviewer=request.reviewer,
                reason=request.reason,
                original=mined.model_dump(mode="json"),
                revised=revised.model_dump(mode="json"),
                diff=diff,
                created_at=now,
            ))

    # Approval.based_on_versions 契约是 dict[str, str]（S0 定型，store 直接
    # json 落库）：每条草稿的版本信息拍平成带 URN 的字符串键，不放嵌套对象
    based_on_versions: dict[str, str] = {"case_version": case.version or ""}
    for item in request.items:
        view = pending[item.suggestion_urn]
        based_on_versions[f"suggestion:{item.suggestion_urn}:confidence"] = (
            str(view.oracle.confidence)
        )
        based_on_versions[f"suggestion:{item.suggestion_urn}:provenance"] = (
            view.oracle.provenance
        )

    markdown = render_case_markdown(
        case, final_oracles, reviewed_by=request.reviewer, decided_at=now
    )
    idempotency_key = outbox_idempotency_key(case.urn, markdown)
    approval_id = make_urn(
        "hitl", "case_review",
        f"{case.external_id}#{uuid.uuid4().hex[:12]}",
    )
    doc_external_id = _external_body(case.urn).split("#", 1)[0]

    approval = Approval(
        approval_id=approval_id,
        action_level=ActionLevel.R2_REVIEWED_WRITE,
        target_system=CASE_REPO_TARGET_SYSTEM,
        request_summary=(
            f"用例 {case.external_id} 人审{request.decision.value}："
            f"{len(final_oracles)} 条 oracle 待回写"
        ),
        request_diff={
            "case_urn": case.urn,
            "before": [o.model_dump(mode="json") for o in case.oracles],
            "after": [o.model_dump(mode="json") for o in final_oracles],
            "actions": action_log,
        },
        decision=request.decision,
        reviewer=request.reviewer,
        reason=request.reason,
        based_on_versions=based_on_versions,
        created_at=now,
        decided_at=now,
    )

    outbox: OutboxRecord | None = None
    if final_oracles:
        outbox = OutboxRecord(
            idempotency_key=idempotency_key,
            action_level=ActionLevel.R2_REVIEWED_WRITE,
            target_system=CASE_REPO_TARGET_SYSTEM,
            request_body={
                "case_urn": case.urn,
                "doc_external_id": doc_external_id,
                "case_external_id": case.external_id,
                "title": case.title,
                "markdown": markdown,
                "oracles": [o.model_dump(mode="json") for o in final_oracles],
                "reviewer": request.reviewer,
                "reason": request.reason,
            },
            status=OutboxStatus.PENDING,
            approval_id=approval_id,
            rollback_note=_rollback_note(
                doc_external_id, case.external_id, idempotency_key),
            created_at=now,
            updated_at=now,
        )

    return CaseDecisionPlan(
        approval=approval,
        outbox=outbox,
        preference_samples=samples,
        suggestion_updates=updates,
        final_oracles=final_oracles,
        markdown=markdown,
        idempotency_key=idempotency_key,
    )
