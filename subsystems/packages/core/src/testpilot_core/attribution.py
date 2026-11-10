"""失败归因纯逻辑（docs/08 FR-EX-007，S3.4）。

零 IO、零三方：只吃归一化证据包 :class:`FailureEvidence`，按"先列多候选、再以
决定性证据区分"的顺序产出五候选与最终裁决。证据采集（重跑历史、os_log、diff、
断言对照）在 workers 服务层完成，本模块不碰任何端口。

候选类别（:class:`FailureCategory`）：真缺陷 / 用例过期 / flaky / 环境失败 /
产品改动；证据不足或冲突拉不开时裁决 ``needs_human``（AC4，绝不硬归因）。

关键口径（与 S3.4 handoff 钉版一致，改动先改文档）：

- flaky 的唯一充分证据：**同一 commit** 的重跑观察里同时出现 passed 与 failed；
  全失败不是 flaky。
- 断言期望与当前代码常量失配 → 用例过期；若同时有覆盖该符号的产品变更记录 →
  产品改动（有意变更，用例需随产品改）。
- 真缺陷不允许排除法：必须有正向证据（stack 符号命中近期 diff，或缺陷族消息 +
  同代码重复复现）。
- 不对称护栏：真缺陷高置信候选在场时，非缺陷类只有凭决定性证据且置信明显更高
  才能赢，否则 needs_human——把真缺陷收口成 flaky/过期是代价最高的错误。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from .enums import CaseStatus, FailureCategory
from .errors import SchemaValidationError

#: 规则版本号：进每个 verdict 与评测报告，规则调整必须 bump，保证结论可回放。
ATTRIBUTION_RULE_VERSION = "attribution-rules-1"

#: 置信地板：低于此值的候选不参与裁决。
CONFIDENCE_FLOOR = 0.6
#: 冲突区间：前两名置信差 ≤ 此值视为并列冲突，交人审。
CONFLICT_MARGIN = 0.1

_FLAKY_CONFIDENCE = 0.9
_OUTDATED_CONFIDENCE = 0.85
_PRODUCT_CHANGE_CONFIDENCE = 0.85
_ENV_BASE_CONFIDENCE = 0.7
_ENV_PER_EXTRA = 0.07
_ENV_CONFIDENCE_CAP = 0.85
_DEFECT_DIFF_CONFIDENCE = 0.9
_DEFECT_REPLICATED_CONFIDENCE = 0.75
#: 真缺陷不对称护栏生效线：达到此置信的缺陷候选会拦截非缺陷收口。
_DEFECT_GUARD_CONFIDENCE = 0.7

#: 缺陷族消息标记（合成夹具与 xcresult failureText 均为英文 XCTAssert 风格）。
_DEFECT_MESSAGE_PATTERNS = (
    "xctassert",
    "assertion",
    "fatal error",
    "exception",
    "uncaught",
    "crash",
)


class EvidenceKind:
    """证据账本类型常量（不用 StrEnum：data 字典自由，类型仅作分类键）。"""

    RERUN = "rerun"
    ASSERTION = "assertion"
    LOG = "log"
    DIFF = "diff"
    ENVIRONMENT = "environment"
    CHANGE_RECORD = "change_record"


class EvidenceItem(BaseModel):
    """证据账本条目：verdict 的 evidence_refs 只能引用这里存在的 ref。"""

    ref: str
    kind: str
    summary: str
    data: dict[str, Any] = Field(default_factory=dict)


class RerunRecord(BaseModel):
    """一次同用例执行观察（含历史重跑；当前失败触发轮由 workers 决定是否计入）。"""

    run_id: str
    commit_sha: str
    status: CaseStatus


class AssertionCheck(BaseModel):
    """断言期望 vs 当前代码常量的对照结果（用例过期/产品改动的决定性证据）。"""

    expected: str
    actual_constant: str
    matches: bool
    symbol_id: str = ""


class LogSignal(BaseModel):
    """os_log 归一化信号：查询条件本身可复现（FR-EX-006），families 为信号族。"""

    query_ref: str
    predicate: str
    matched_count: int = 0
    families: list[str] = Field(default_factory=list)


class DiffSignal(BaseModel):
    """近期 diff 与失败堆栈的对照。"""

    base: str
    head: str
    changed_symbols: list[str] = Field(default_factory=list)
    stack_symbol: str = ""
    stack_symbol_changed: bool = False


class EnvironmentSignal(BaseModel):
    """环境侧信号：授权竞态/能力缺口/真机-only 能力未覆盖等。"""

    family: str
    detail: str
    uncovered_layers: list[str] = Field(default_factory=list)


class ChangeRecord(BaseModel):
    """产品变更记录（需求/变更单引用）：证明行为变化是有意为之。"""

    ref: str
    summary: str
    symbols: list[str] = Field(default_factory=list)


class FailureEvidence(BaseModel):
    """一次失败用例的归一化证据包（workers 组包，core 只判定）。"""

    case_id: str
    commit_sha: str
    message: str = ""
    stack_top: str = ""
    reruns: list[RerunRecord] = Field(default_factory=list)
    assertion: AssertionCheck | None = None
    log_signals: list[LogSignal] = Field(default_factory=list)
    diff: DiffSignal | None = None
    environment: list[EnvironmentSignal] = Field(default_factory=list)
    change_records: list[ChangeRecord] = Field(default_factory=list)
    uncovered_layers: list[str] = Field(default_factory=list)
    ledger: list[EvidenceItem] = Field(default_factory=list)


class AttributionCandidate(BaseModel):
    category: FailureCategory
    confidence: float = Field(ge=0.0, le=1.0)
    reasons: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)


class AttributionVerdict(BaseModel):
    case_id: str
    category: FailureCategory
    confidence: float
    candidates: list[AttributionCandidate] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    uncovered_layers: list[str] = Field(default_factory=list)
    needs_human_reason: str = ""
    rule_version: str = ATTRIBUTION_RULE_VERSION


# ── 内部工具 ────────────────────────────────────────────────────────────────


def _refs(ledger: list[EvidenceItem], kind: str) -> list[str]:
    return [item.ref for item in ledger if item.kind == kind]


def _has_ledger_kind(ledger: list[EvidenceItem], kind: str) -> bool:
    return any(item.kind == kind for item in ledger)


def _validate_packet(ev: FailureEvidence) -> None:
    """组包纪律：结构化信号必须有同源账本条目，裁决引用必须可回溯。"""
    if not ev.case_id.strip():
        raise SchemaValidationError("FailureEvidence.case_id 为空")
    if not ev.commit_sha.strip():
        raise SchemaValidationError("FailureEvidence.commit_sha 为空")
    refs = [item.ref for item in ev.ledger]
    if len(refs) != len(set(refs)):
        raise SchemaValidationError("证据账本 ref 重复")
    pairs = (
        (ev.reruns, EvidenceKind.RERUN),
        (ev.log_signals, EvidenceKind.LOG),
        (ev.environment, EvidenceKind.ENVIRONMENT),
        (ev.change_records, EvidenceKind.CHANGE_RECORD),
    )
    for structured, kind in pairs:
        if structured and not _has_ledger_kind(ev.ledger, kind):
            raise SchemaValidationError(f"证据账本缺失 {kind} 条目，结构化信号不可回溯")
    if ev.assertion is not None and not _has_ledger_kind(
        ev.ledger, EvidenceKind.ASSERTION
    ):
        raise SchemaValidationError("证据账本缺失 assertion 条目，结构化信号不可回溯")
    if ev.diff is not None and not _has_ledger_kind(ev.ledger, EvidenceKind.DIFF):
        raise SchemaValidationError("证据账本缺失 diff 条目，结构化信号不可回溯")


def _same_commit(ev: FailureEvidence) -> list[RerunRecord]:
    return [r for r in ev.reruns if r.commit_sha == ev.commit_sha]


def _is_defect_message(message: str) -> bool:
    text = message.lower()
    return any(p in text for p in _DEFECT_MESSAGE_PATTERNS)


# ── 五候选规则：每条只在有决定性证据时开火，否则返回 None ─────────────────────


def _rule_flaky(ev: FailureEvidence) -> AttributionCandidate | None:
    same = _same_commit(ev)
    if len(same) < 2:
        return None
    statuses = {r.status for r in same}
    if CaseStatus.PASSED in statuses and (
        CaseStatus.FAILED in statuses or CaseStatus.ERROR in statuses
    ):
        return AttributionCandidate(
            category=FailureCategory.FLAKY,
            confidence=_FLAKY_CONFIDENCE,
            reasons=[
                f"同 commit {ev.commit_sha[:8]} 重跑同时出现 passed 与 failed，"
                "符合 flaky 时过时不过定义"
            ],
            evidence_refs=_refs(ev.ledger, EvidenceKind.RERUN),
        )
    return None


def _rule_assertion_family(ev: FailureEvidence) -> AttributionCandidate | None:
    """失配断言二分：有变更记录背书 → 产品改动；否则 → 用例过期。"""
    if ev.assertion is None or ev.assertion.matches:
        return None
    symbol = ev.assertion.symbol_id
    covering = [
        record for record in ev.change_records if not symbol or symbol in record.symbols
    ]
    refs = _refs(ev.ledger, EvidenceKind.ASSERTION)
    reason = (
        f"断言期望 {ev.assertion.expected!r} 与当前代码常量 "
        f"{ev.assertion.actual_constant!r} 失配"
    )
    if covering:
        return AttributionCandidate(
            category=FailureCategory.PRODUCT_CHANGE,
            confidence=_PRODUCT_CHANGE_CONFIDENCE,
            reasons=[
                reason,
                f"变更记录 {covering[0].ref} 覆盖符号 {symbol or '未知'}，"
                "行为变化为有意产品改动",
            ],
            evidence_refs=refs
            + _refs(ev.ledger, EvidenceKind.CHANGE_RECORD),
        )
    return AttributionCandidate(
        category=FailureCategory.CASE_OUTDATED,
        confidence=_OUTDATED_CONFIDENCE,
        reasons=[reason, "无产品变更记录背书，用例仍守旧期望值"],
        evidence_refs=refs,
    )


def _rule_environment(ev: FailureEvidence) -> AttributionCandidate | None:
    if not ev.environment:
        return None
    confidence = min(
        _ENV_CONFIDENCE_CAP,
        _ENV_BASE_CONFIDENCE + _ENV_PER_EXTRA * (len(ev.environment) - 1),
    )
    families = ", ".join(sorted({s.family for s in ev.environment}))
    refs = _refs(ev.ledger, EvidenceKind.ENVIRONMENT)
    # 日志信号与环境信号同族时一并入证（查询条件可复现才是有效证据）
    log_refs = [
        item.ref
        for item in ev.ledger
        if item.kind == EvidenceKind.LOG
        and any(f in item.data.get("families", []) for f in families.split(", "))
    ]
    return AttributionCandidate(
        category=FailureCategory.ENVIRONMENT,
        confidence=round(confidence, 2),
        reasons=[f"环境信号族命中：{families}；{ev.environment[0].detail}"],
        evidence_refs=refs + log_refs,
    )


def _rule_real_defect(ev: FailureEvidence) -> AttributionCandidate | None:
    if ev.diff is not None and ev.diff.stack_symbol_changed:
        return AttributionCandidate(
            category=FailureCategory.REAL_DEFECT,
            confidence=_DEFECT_DIFF_CONFIDENCE,
            reasons=[
                f"失败堆栈符号 {ev.diff.stack_symbol or '未知'} 命中近期 diff "
                f"({ev.diff.base[:8]}..{ev.diff.head[:8]})，回归引入嫌疑"
            ],
            evidence_refs=_refs(ev.ledger, EvidenceKind.DIFF),
        )
    same = _same_commit(ev)
    failures = [
        r for r in same if r.status in (CaseStatus.FAILED, CaseStatus.ERROR)
    ]
    if len(failures) >= 2 and _is_defect_message(ev.message):
        return AttributionCandidate(
            category=FailureCategory.REAL_DEFECT,
            confidence=_DEFECT_REPLICATED_CONFIDENCE,
            reasons=[
                f"同 commit 重复失败 {len(failures)} 次且消息属缺陷族"
                f"（{ev.message[:60]}），无可解释的环境/过期证据"
            ],
            evidence_refs=_refs(ev.ledger, EvidenceKind.RERUN),
        )
    return None


_RULES = (
    _rule_flaky,
    _rule_assertion_family,
    _rule_environment,
    _rule_real_defect,
)


def _needs_human(
    ev: FailureEvidence,
    reason: str,
    refs: list[str],
    candidates: list[AttributionCandidate] | None = None,
) -> AttributionVerdict:
    return AttributionVerdict(
        case_id=ev.case_id,
        category=FailureCategory.NEEDS_HUMAN,
        confidence=0.0,
        # 冲突场景保留候选给人审复核；无候选（薄证据）时为空
        candidates=candidates or [],
        evidence_refs=refs,
        uncovered_layers=list(ev.uncovered_layers),
        needs_human_reason=reason,
    )


def attribute_failure(ev: FailureEvidence) -> AttributionVerdict:
    """对单个失败用例的证据包执行五候选归因（纯函数，确定性输出）。"""
    _validate_packet(ev)

    candidates = [c for rule in _RULES if (c := rule(ev)) is not None]
    candidates = [c for c in candidates if c.confidence >= CONFIDENCE_FLOOR]
    candidates.sort(key=lambda c: c.confidence, reverse=True)

    all_refs = [item.ref for item in ev.ledger]
    if not candidates:
        return _needs_human(ev, "无候选证据达到置信地板，证据不足不硬归因（AC4）", all_refs)

    top = candidates[0]
    runner_up = candidates[1] if len(candidates) > 1 else None

    # 不对称护栏：高置信真缺陷在场时，非缺陷收口必须明显更强，否则交人审
    defect = next(
        (
            c
            for c in candidates
            if c.category == FailureCategory.REAL_DEFECT
            and c.confidence >= _DEFECT_GUARD_CONFIDENCE
        ),
        None,
    )
    if defect is not None and top.category != FailureCategory.REAL_DEFECT:
        if top.confidence - defect.confidence <= CONFLICT_MARGIN:
            return _needs_human(
                ev,
                f"真缺陷证据（置信 {defect.confidence}）与 {top.category.value} "
                f"候选（置信 {top.confidence}）冲突且无法排除缺陷，交人审",
                sorted(set(top.evidence_refs + defect.evidence_refs)),
                candidates=candidates,
            )

    # 并列冲突：前两名拉不开差距且类别不同，交人审
    if (
        runner_up is not None
        and top.confidence - runner_up.confidence <= CONFLICT_MARGIN
        and top.category != runner_up.category
    ):
        return _needs_human(
            ev,
            f"候选置信并列冲突：{top.category.value}({top.confidence}) vs "
            f"{runner_up.category.value}({runner_up.confidence})，交人审",
            sorted(set(top.evidence_refs + runner_up.evidence_refs)),
            candidates=candidates,
        )

    # 裁决引用的每个 ref 必须能在账本回溯（内部不变量，违例即组装 bug）
    unknown = sorted(set(top.evidence_refs) - set(all_refs))
    if unknown:
        raise SchemaValidationError(f"裁决引用了账本不存在的证据 ref: {unknown}")

    return AttributionVerdict(
        case_id=ev.case_id,
        category=top.category,
        confidence=top.confidence,
        candidates=candidates,
        evidence_refs=list(top.evidence_refs),
        uncovered_layers=list(ev.uncovered_layers),
    )
