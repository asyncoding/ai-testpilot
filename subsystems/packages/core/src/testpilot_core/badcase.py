"""bad case 闭环状态机与纯统计（docs/08 FR-EV-005，S2.6c）。

零 IO、零三方：把在线负反馈/评测失败/人工上报统一成 bad case 台账，状态迁移、
反馈幂等入账、转 golden 评测题、闭环周期统计全部在本模块算完，worker 只负责
取数与落账，store 只做持久化。

状态机（合法迁移闭集，非法迁移 fail-closed 抛 ``BadCaseError``）：

```
open ──triage──▶ triaged ──accept──▶ accepted ──promote──▶ promoted
                     │                    │                    ├── pass ──▶ fixed（闭环）
                     └──reject──▶ rejected（闭环）             └── fail ──▶ regression_failed
                                                                  └── 复测 pass ──▶ fixed
```

- 分诊允许从 ``open`` 一步到位：内部补记 open→triaged 事件，历史不留断点；
- ``rejected``/``fixed`` 是终态（``fixed`` 不可再迁移，回归失败只发生在 promoted 后）；
- ``regression_failed`` 不是闭环：``closed_at`` 留空，复测通过才置 fixed；
- 内容寻址：``bad_case_id = bc-<sha1(source|source_ref)[:12]>``，同源同引用
  重放恒等，配合 store 的 UNIQUE(source, source_ref) 与 feedback_id 部分唯一索引
  实现负反馈幂等；
- 转 golden 产出的 :class:`EvalCase` 带 ``kind="bad_case"`` 与完整溯源
  （bad_case_id/源引用/四元组快照），下一轮评测自动把坏例当回归题。
"""

from __future__ import annotations

import hashlib
import statistics
from datetime import datetime
from typing import Literal, Sequence

from pydantic import BaseModel, Field

from .enums import FeedbackKind
from .models import EvalCase, utcnow

BadCaseStatus = Literal[
    "open", "triaged", "accepted", "rejected",
    "promoted", "fixed", "regression_failed",
]
BadCaseSource = Literal["qa_feedback", "eval_failure", "manual_review"]
TriageDecision = Literal["accept", "reject"]
RegressionOutcome = Literal["pass", "fail"]

# 状态迁移闭集：不在表内的迁移一律拒绝，绝不静默落非法状态
TRANSITIONS: dict[str, frozenset[str]] = {
    "open": frozenset({"triaged"}),
    "triaged": frozenset({"accepted", "rejected"}),
    "accepted": frozenset({"promoted"}),
    "rejected": frozenset(),
    "promoted": frozenset({"fixed", "regression_failed"}),
    "regression_failed": frozenset({"fixed"}),
    "fixed": frozenset(),
}
CLOSED_STATUSES = frozenset({"rejected", "fixed"})
_ALL_STATUSES: tuple[str, ...] = (
    "open", "triaged", "accepted", "rejected",
    "promoted", "fixed", "regression_failed",
)

# 转 golden 后的题集归属与题 ID 前缀（locked_version 口径，供 store.upsert_eval_case）
GOLDEN_LOCKED_VERSION = "bad-case-promoted"
_GOLDEN_CASE_PREFIX = "bad-case:"
_ID_PREFIX = "bc-"


class BadCaseError(ValueError):
    """bad case 输入/迁移非法（空引用、非法迁移、重复闭环等），HTTP 层映射 422。"""


class BadCaseSnapshot(BaseModel):
    """发现坏例时的四元组版本快照（docs/06 §6.1：每个数字可复算的基础）。"""

    data_version: str = ""
    model_version: str = ""
    prompt_version: str = ""
    params_version: str = ""


class BadCaseEvent(BaseModel):
    """状态机审计事件：谁在什么时候把坏例从什么状态迁到什么状态。"""

    from_status: str
    to_status: str
    actor: str
    note: str = ""
    at: datetime = Field(default_factory=utcnow)


class BadCase(BaseModel):
    """一条 bad case 台账记录；完整模型以 raw_json 为唯一还原来源，投影列供 SQL 盘点。"""

    bad_case_id: str
    source: BadCaseSource
    source_ref: str
    # qa_feedback 源写 feedback 行 id（幂等键）；其余源为空串
    feedback_id: str = ""
    status: BadCaseStatus = "open"
    title: str
    question: str = ""
    detail: str = ""
    reason: str = ""
    snapshot: BadCaseSnapshot = Field(default_factory=BadCaseSnapshot)
    triaged_by: str = ""
    triaged_at: datetime | None = None
    decision_note: str = ""
    golden_case_id: str = ""
    regression_eval_run_id: str = ""
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    closed_at: datetime | None = None
    history: list[BadCaseEvent] = Field(default_factory=list)

    @property
    def is_closed(self) -> bool:
        return self.status in CLOSED_STATUSES

    @property
    def cycle_hours(self) -> float | None:
        """创建→闭环耗时（小时）；未闭环返回 None。"""
        if self.closed_at is None:
            return None
        return round((self.closed_at - self.created_at).total_seconds() / 3600.0, 3)


class CycleStats(BaseModel):
    """闭环周期统计（FR-EV-005：闭环周期可统计；空样本不除零）。"""

    total: int = 0
    by_status: dict[str, int] = Field(default_factory=dict)
    open_count: int = 0
    closed_count: int = 0
    closure_rate: float = 0.0
    median_cycle_hours: float | None = None


def bad_case_id(source: str, source_ref: str) -> str:
    """内容寻址 ID：同 source/source_ref 恒等（重放幂等的基础）。"""
    digest = hashlib.sha1(
        f"{source}|{source_ref}".encode("utf-8")
    ).hexdigest()[:12]
    return f"{_ID_PREFIX}{digest}"


def golden_case_id_for(bad_case_id: str) -> str:
    """转 golden 后的评测题 ID（确定性，重放不造新题）。"""
    return f"{_GOLDEN_CASE_PREFIX}{bad_case_id}"


def new_bad_case(
    *,
    source: BadCaseSource,
    source_ref: str,
    title: str,
    question: str = "",
    detail: str = "",
    reason: str = "",
    feedback_id: str = "",
    snapshot: BadCaseSnapshot | None = None,
    at: datetime | None = None,
) -> BadCase:
    """建台账新条目（open 态，带创世审计事件）；空引用/空标题 fail-closed。"""
    if not source_ref or not source_ref.strip():
        raise BadCaseError("source_ref 不能为空：坏例必须可回溯到源记录")
    if not title or not title.strip():
        raise BadCaseError("title 不能为空：坏例必须有人类可读标题")
    if source == "qa_feedback" and not feedback_id:
        raise BadCaseError("qa_feedback 源必须带 feedback_id（负反馈幂等键）")
    now = at or utcnow()
    return BadCase(
        bad_case_id=bad_case_id(source, source_ref),
        source=source,
        source_ref=source_ref,
        feedback_id=feedback_id,
        status="open",
        title=title.strip(),
        question=question,
        detail=detail,
        reason=reason,
        snapshot=snapshot or BadCaseSnapshot(),
        created_at=now,
        updated_at=now,
        history=[BadCaseEvent(from_status="", to_status="open",
                              actor="system", note="坏例入账", at=now)],
    )


def ingest_feedback_as_bad_case(
    *,
    feedback_row_id: int,
    run_id: str,
    kind: FeedbackKind,
    reason: str = "",
    detail: str = "",
    question: str = "",
    model_version: str = "",
    snapshot: BadCaseSnapshot | None = None,
    at: datetime | None = None,
) -> BadCase:
    """把一条 QA 反馈投影成 bad case；只有 👎 not_useful 是坏例，👍 拒入。

    source_ref 带 feedback 行 id 区分同一 run 的多条反馈；feedback_id 是幂等键，
    store 侧再叠部分唯一索引双保险。
    """
    if kind != FeedbackKind.NOT_USEFUL:
        raise BadCaseError("useful 反馈不是 bad case，拒入台账")
    source_ref = f"{run_id}#feedback-{feedback_row_id}"
    title = f"负反馈 {run_id}" + (f"：{reason}" if reason else "")
    snap = snapshot or BadCaseSnapshot(model_version=model_version)
    if model_version and not snap.model_version:
        snap = snap.model_copy(update={"model_version": model_version})
    return new_bad_case(
        source="qa_feedback",
        source_ref=source_ref,
        title=title,
        question=question,
        detail=detail,
        reason=reason,
        feedback_id=str(feedback_row_id),
        snapshot=snap,
        at=at,
    )


def apply_transition(
    case: BadCase,
    to_status: BadCaseStatus,
    *,
    actor: str,
    note: str = "",
    at: datetime | None = None,
) -> BadCase:
    """按迁移闭集返回**新状态副本**（不就地改入参）；非法迁移 fail-closed。"""
    if to_status not in TRANSITIONS.get(case.status, frozenset()):
        raise BadCaseError(
            f"非法状态迁移：{case.status} → {to_status}"
            f"（合法目标：{sorted(TRANSITIONS.get(case.status, frozenset())) or '终态不可迁移'}）"
        )
    if not actor or not actor.strip():
        raise BadCaseError("状态迁移必须带 actor（审计四要素缺一不可）")
    now = at or utcnow()
    updated = case.model_copy(deep=True)
    updated.status = to_status
    updated.updated_at = now
    updated.history.append(BadCaseEvent(
        from_status=case.status, to_status=to_status,
        actor=actor.strip(), note=note, at=now))
    if to_status in CLOSED_STATUSES:
        updated.closed_at = now
    return updated


def triage(
    case: BadCase,
    *,
    decision: TriageDecision,
    actor: str,
    reason: str = "",
    note: str = "",
    at: datetime | None = None,
) -> BadCase:
    """分诊：accept→accepted / reject→rejected；open 态自动补 triaged 中转事件。"""
    now = at or utcnow()
    current = case
    if current.status == "open":
        current = apply_transition(
            current, "triaged", actor=actor, note="分诊受理", at=now)
    if current.status != "triaged":
        raise BadCaseError(
            f"只有 triaged/open 态可分诊，当前状态：{current.status}")
    target = "accepted" if decision == "accept" else "rejected"
    updated = apply_transition(current, target, actor=actor, note=note, at=now)
    updated.triaged_by = actor.strip()
    updated.triaged_at = now
    if reason:
        updated.reason = reason
    if note:
        updated.decision_note = note
    return updated


def promote_to_eval_case(case: BadCase) -> EvalCase:
    """accepted 坏例 → golden 回归题（kind=bad_case，ground_truth 带完整溯源）。

    只映射不改状态：调用方随后 apply_transition(→promoted) 并回写 golden_case_id，
    两步在 worker 单事务内提交，失败整体回滚不留「题已建状态未翻」的交错态。
    """
    if case.status != "accepted":
        raise BadCaseError(
            f"只有 accepted 坏例可转 golden，当前状态：{case.status}")
    golden_id = golden_case_id_for(case.bad_case_id)
    return EvalCase(
        case_id=golden_id,
        kind="bad_case",
        question=case.question or case.title,
        ground_truth={
            "provenance": {
                "bad_case_id": case.bad_case_id,
                "source": case.source,
                "source_ref": case.source_ref,
                "feedback_id": case.feedback_id,
            },
            "origin_reason": case.reason,
            "detail": case.detail,
            "snapshot": case.snapshot.model_dump(),
            "expected": "regression: 分诊确认的坏例场景不得复现",
        },
        required_evidence=[],
    )


def associate_regression(
    case: BadCase,
    *,
    eval_run_id: str,
    outcome: RegressionOutcome,
    actor: str,
    note: str = "",
    at: datetime | None = None,
) -> BadCase:
    """关联回归评测结果：pass→fixed 闭环，fail→regression_failed 继续跟踪。"""
    if not eval_run_id or not eval_run_id.strip():
        raise BadCaseError("eval_run_id 不能为空：回归关联必须可追溯到评测运行")
    now = at or utcnow()
    target = "fixed" if outcome == "pass" else "regression_failed"
    updated = apply_transition(
        case, target, actor=actor,
        note=note or f"回归评测 {eval_run_id} 判定 {outcome}", at=now)
    updated.regression_eval_run_id = eval_run_id.strip()
    return updated


def cycle_time_stats(cases: Sequence[BadCase]) -> CycleStats:
    """纯统计：各态计数、闭环率、闭环周期中位数（小时）；空样本安全不除零。"""
    by_status = {status: 0 for status in _ALL_STATUSES}
    for case in cases:
        by_status[case.status] += 1
    closed = [c for c in cases if c.is_closed]
    cycles = [c.cycle_hours for c in closed if c.cycle_hours is not None]
    total = len(cases)
    return CycleStats(
        total=total,
        by_status=by_status,
        open_count=total - len(closed),
        closed_count=len(closed),
        closure_rate=round(len(closed) / total, 6) if total else 0.0,
        median_cycle_hours=round(statistics.median(cycles), 3) if cycles else None,
    )
