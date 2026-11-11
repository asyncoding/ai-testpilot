"""影响面分析、MR 摘要与回归选择精度纯逻辑（docs/08 FR-FRS-003，S4.3）。

零 IO、零三方：worker 负责取数（scm ChangeSet、store 关系/用例、工单适配器），
本模块只做确定性推导，三件套各自可 hostless 直测：

1. :func:`build_impact_analysis` —— 影响链「变更符号 → 关联用例 → 历史缺陷」，
   用例三分类闭集（docs/08 AC1 原话）：

   - ``runnable`` 可直接跑：用例活跃、非探索性、引用的符号身份未被破坏；
   - ``no_automation`` 无自动化：``exploratory=True`` 的用例本就不进自动化链路；
   - ``conflict_update`` 与 diff 冲突需更新：引用的符号被删除/重命名/签名变更，
     用例步骤或断言锚点失效，必须人工更新后才能再跑。

2. :func:`summarize_change_set` + :func:`supplement_summary` —— MR 摘要**只从
   diff 反推**（函数签名不收 commit message），确定性脊柱先行；模型只能在变更
   符号 URN 白名单内补风险备注，越界/空文本/坏形状逐行进 ``rejected`` 幻觉台账，
   永不改写确定性条目（纪律对齐 S2.3 testpoint 白名单）。

3. :func:`regression_selection_precision` —— 推荐精度以实际执行结果回算
   （docs/06：推荐且实际执行的用例中确实与变更相关的比例，门限 ≥0.8）；
   空分母返回 ``precision=None``（无基线不展示百分比，FR-EV-003 AC2）。
"""

from __future__ import annotations

from typing import Literal, Sequence

from pydantic import BaseModel, Field, computed_field

from .case_relations import CaseRelation
from .enums import CaseStatus, ChangeType
from .models import ChangeSet, Issue, SymbolRename, TestCase

# ── 闭集词表：分类与原因码不允许模型/调用方自由扩词 ──────────────────────────

ImpactClassification = Literal[
    "runnable", "no_automation", "conflict_update"
]
ImpactReason = Literal[
    "symbol_removed",        # 引用符号被删除（且无 rename 链）
    "symbol_renamed",        # 引用符号已重命名（S4.2 renamed_to 链目标已知）
    "signature_changed",     # 引用符号签名变更，断言锚点可能失效
    "symbol_modified",       # 引用符号仅函数体修改（身份不变，可直接跑）
    "exploratory_case",      # 探索性用例，无自动化
    "requirement_entry",     # 经需求入口（case_validates_requirement）纳入
]
SummaryNoteKind = Literal["file", "symbol", "rename", "file_rename"]
SummaryGenerationSource = Literal["deterministic", "model"]

# docs/06 §指标「回归选择有效率」门限；调用方可覆盖，默认 0.8
DEFAULT_PRECISION_GATE = 0.8
_SYMBOL_ID_SEP = "#"


class ImpactError(ValueError):
    """影响面输入非法（空 base/head、坏 URN 关系等），HTTP 层映射 422。"""


class ImpactRequest(BaseModel):
    """影响面/摘要 API 的语言中立请求体（薄 HTTP 层不另造契约模型）。"""

    base_commit: str
    head_commit: str
    requirement_urns: list[str] = Field(default_factory=list)


class PrecisionRequest(ImpactRequest):
    """精度回算请求：在影响面请求上钉一个真实执行 run。"""

    run_id: str
    full_suite_size: int = 0


# ── 1. 影响链 ────────────────────────────────────────────────────────────────


class ChangedSymbol(BaseModel):
    """一个已解析到库内 URN 的变更符号（worker 负责 git symbol_id→URN 解析）。"""

    urn: str
    change_type: ChangeType
    # rename 链目标 URN（S4.2 relations.kind='renamed_to'）；无链为空串
    renamed_to: str = ""


class UnresolvedSymbol(BaseModel):
    """git 报了变更但库内解析不到 URN 的符号：如实挂账，绝不按路径臆造 URN。"""

    symbol_id: str
    change_type: ChangeType
    reason: str = "no_index_match"


class ImpactedCase(BaseModel):
    case_urn: str
    external_id: str
    title: str
    classification: ImpactClassification = "runnable"
    reasons: list[ImpactReason] = Field(default_factory=list)
    # 命中的变更符号 URN（去重排序）；仅需求入口命中时为空
    symbol_urns: list[str] = Field(default_factory=list)
    # 经哪些需求 URN 入口纳入（去重排序）
    requirement_urns: list[str] = Field(default_factory=list)
    # 关联关系的最高置信度（低置信关系默认在 worker 侧已过滤）
    confidence: float = 0.0


class AffectedSymbol(BaseModel):
    urn: str
    change_type: ChangeType
    renamed_to: str = ""
    case_count: int = 0


class DefectLink(BaseModel):
    """历史缺陷/工单与本次受影响用例的确定性链接（Issue.related_case_ids）。"""

    issue_urn: str
    external_id: str
    title: str
    status: str
    severity: str = ""
    matched_case_ids: list[str] = Field(default_factory=list)


class ImpactAnalysis(BaseModel):
    base_commit: str
    head_commit: str
    requirement_urns: list[str] = Field(default_factory=list)
    affected_symbols: list[AffectedSymbol] = Field(default_factory=list)
    cases: list[ImpactedCase] = Field(default_factory=list)
    historical_defects: list[DefectLink] = Field(default_factory=list)
    unresolved_symbols: list[UnresolvedSymbol] = Field(default_factory=list)
    cancelled_cases_skipped: list[str] = Field(default_factory=list)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def classification_counts(self) -> dict[str, int]:
        counts = {"runnable": 0, "no_automation": 0, "conflict_update": 0}
        for case in self.cases:
            counts[case.classification] += 1
        return counts

    @property
    def recommended_case_urns(self) -> list[str]:
        """精度回算的推荐集：只有「可直接跑」进入自动化回归推荐。"""
        return [c.case_urn for c in self.cases if c.classification == "runnable"]


def build_impact_analysis(
    *,
    base_commit: str,
    head_commit: str,
    changed_symbols: Sequence[ChangedSymbol],
    relations: Sequence[CaseRelation],
    cases: Sequence[TestCase],
    issues: Sequence[Issue] = (),
    requirement_urns: Sequence[str] = (),
    unresolved_symbols: Sequence[UnresolvedSymbol] = (),
) -> ImpactAnalysis:
    """影响链纯推导：变更符号 + 用例关系 → 三分类用例清单 + 历史缺陷链接。

    取消态用例整体跳过并登记（与 S2.6b build_case_relations 同口径）；
    关系闭集外的 kind 一律忽略（fail-closed 不猜语义）；同一用例经多条路径
    命中时原因/符号/需求取并集，置信度取 max，输出按 external_id 稳定排序。
    """
    if not base_commit or not head_commit:
        raise ImpactError("base_commit/head_commit 不能为空：影响面必须锚定版本区间")

    changed_by_urn = {s.urn: s for s in changed_symbols}
    requirement_set = set(requirement_urns)

    # 关系投影：符号 → 用例集；需求 → 用例集；用例 → (目标 URN 集, 最高置信)
    symbol_to_cases: dict[str, set[str]] = {}
    requirement_to_cases: dict[str, set[str]] = {}
    targets_by_case: dict[str, set[str]] = {}
    confidence_by_case: dict[str, float] = {}
    for rel in relations:
        confidence_by_case[rel.source_urn] = max(
            confidence_by_case.get(rel.source_urn, 0.0), rel.confidence
        )
        targets_by_case.setdefault(rel.source_urn, set()).add(rel.target_urn)
        if rel.kind == "case_references_symbol" and rel.target_urn in changed_by_urn:
            symbol_to_cases.setdefault(rel.target_urn, set()).add(rel.source_urn)
        elif (
            rel.kind == "case_validates_requirement"
            and rel.target_urn in requirement_set
        ):
            requirement_to_cases.setdefault(rel.target_urn, set()).add(rel.source_urn)

    active_cases = {c.urn: c for c in cases if c.lifecycle != "cancelled"}
    cancelled = sorted(
        c.external_id for c in cases if c.lifecycle == "cancelled"
    )

    in_scope: dict[str, ImpactedCase] = {}

    def seat(case_urn: str) -> ImpactedCase:
        case = active_cases[case_urn]
        return in_scope.setdefault(case_urn, ImpactedCase(
            case_urn=case_urn,
            external_id=case.external_id,
            title=case.title,
            confidence=confidence_by_case.get(case_urn, 0.0),
        ))

    # 路径 A：变更符号 → 引用用例（关系目标必须命中本次变更符号白名单）
    for symbol_urn, case_urns in symbol_to_cases.items():
        changed = changed_by_urn[symbol_urn]
        for case_urn in case_urns:
            if case_urn not in active_cases:
                continue  # 关系指向已取消/不存在用例：不造条目
            seat(case_urn).symbol_urns.append(symbol_urn)

    # 路径 B：需求入口 → 验证用例
    for requirement_urn, case_urns in requirement_to_cases.items():
        for case_urn in case_urns:
            if case_urn not in active_cases:
                continue
            row = seat(case_urn)
            row.requirement_urns.append(requirement_urn)
            if "requirement_entry" not in row.reasons:
                row.reasons.append("requirement_entry")

    # 分类：原因闭集 + 三分类桶（探索性优先判 no_automation，冲突原因仍保留）
    for row in in_scope.values():
        case = active_cases[row.case_urn]
        hard_conflict = False
        for symbol_urn in row.symbol_urns:
            changed = changed_by_urn[symbol_urn]
            if changed.change_type == ChangeType.REMOVED:
                if changed.renamed_to:
                    row.reasons.append("symbol_renamed")
                else:
                    row.reasons.append("symbol_removed")
                hard_conflict = True
            elif changed.change_type == ChangeType.SIGNATURE_CHANGED:
                row.reasons.append("signature_changed")
                hard_conflict = True
            elif changed.change_type == ChangeType.MODIFIED:
                row.reasons.append("symbol_modified")
        if case.exploratory:
            row.reasons.append("exploratory_case")
            row.classification = "no_automation"
        elif hard_conflict:
            row.classification = "conflict_update"
        else:
            row.classification = "runnable"
        # 稳定化：去重排序，原因码按闭集声明顺序排
        row.symbol_urns = sorted(set(row.symbol_urns))
        row.requirement_urns = sorted(set(row.requirement_urns))
        row.reasons = sorted(set(row.reasons), key=_REASON_ORDER.index)

    affected = [
        AffectedSymbol(
            urn=symbol.urn,
            change_type=symbol.change_type,
            renamed_to=symbol.renamed_to,
            case_count=len(symbol_to_cases.get(symbol.urn, set())
                           & set(active_cases)),
        )
        for symbol in sorted(changed_symbols, key=lambda s: s.urn)
    ]

    # 历史缺陷：工单 related_case_ids 与本次在 Scope 用例 external_id 求交
    in_scope_external = {row.external_id for row in in_scope.values()}
    defects: list[DefectLink] = []
    for issue in issues:
        matched = sorted(
            cid for cid in issue.related_case_ids if cid in in_scope_external
        )
        if matched:
            defects.append(DefectLink(
                issue_urn=issue.urn,
                external_id=issue.external_id,
                title=issue.title,
                status=issue.status,
                severity=issue.severity,
                matched_case_ids=matched,
            ))

    return ImpactAnalysis(
        base_commit=base_commit,
        head_commit=head_commit,
        requirement_urns=sorted(requirement_set),
        affected_symbols=affected,
        cases=sorted(in_scope.values(), key=lambda r: r.external_id),
        historical_defects=defects,
        unresolved_symbols=list(unresolved_symbols),
        cancelled_cases_skipped=cancelled,
    )


_REASON_ORDER = [
    "symbol_removed", "symbol_renamed", "signature_changed",
    "symbol_modified", "exploratory_case", "requirement_entry",
]


# ── 2. MR 摘要（diff 反推，模型白名单补点） ─────────────────────────────────


class SummaryBullet(BaseModel):
    """一条确定性摘要；evidence 是符号 ID/文件路径，每条结论可回查 diff。"""

    text: str
    kind: SummaryNoteKind
    evidence: list[str] = Field(default_factory=list)


class ChangeSummary(BaseModel):
    base_commit: str
    head_commit: str
    generated_from: Literal["diff"] = "diff"  # 显式声明：永不读 commit message
    files_changed: int = 0
    files_added: int = 0
    files_deleted: int = 0
    symbols_added: int = 0
    symbols_removed: int = 0
    symbols_signature_changed: int = 0
    symbols_modified: int = 0
    rename_pairs: list[SymbolRename] = Field(default_factory=list)
    bullets: list[SummaryBullet] = Field(default_factory=list)


class SummaryNote(BaseModel):
    """模型补的风险备注；只能挂在白名单（本次变更符号 URN）内。"""

    text: str
    symbol_urn: str
    provenance: Literal["model"] = "model"


class RejectedNote(BaseModel):
    """幻觉台账：被白名单/形状校验拒绝的模型行，原样留痕（对齐 S2.3）。"""

    text: str
    symbol_urn: str
    reason: Literal["unknown_symbol", "empty_text", "bad_shape"]
    detail: str = ""


class ChangeSummaryResult(BaseModel):
    summary: ChangeSummary
    notes: list[SummaryNote] = Field(default_factory=list)
    rejected: list[RejectedNote] = Field(default_factory=list)
    generation_source: SummaryGenerationSource = "deterministic"
    model_version: str = ""

    @property
    def hallucination_rate(self) -> float:
        """被拒行 /（采纳备注 + 被拒行）；无模型产出时 0.0。"""
        rejected = len(self.rejected)
        denominator = len(self.notes) + rejected
        return round(rejected / denominator, 6) if denominator else 0.0


def _path_of(symbol_id: str) -> str:
    """git symbol_id 形态 ``<path>#<anchor>``；无分隔符时整体视为路径。"""
    return symbol_id.partition(_SYMBOL_ID_SEP)[0] or symbol_id


def summarize_change_set(
    change_set: ChangeSet,
    renames: Sequence[SymbolRename] = (),
) -> ChangeSummary:
    """从 ChangeSet 确定性反推 MR 摘要；入参不含 message 字段，物理上无法依赖。

    文件级 A/D 由符号变更类型推断（纯新增文件只含 added、纯删除只含 removed），
    无符号变化的文件（文档/配置）保守计 modified，不臆造 A/D。
    """
    counts = {
        ChangeType.ADDED: 0, ChangeType.REMOVED: 0,
        ChangeType.SIGNATURE_CHANGED: 0, ChangeType.MODIFIED: 0,
    }
    types_by_path: dict[str, set[ChangeType]] = {}
    for change in change_set.symbol_changes:
        counts[change.change_type] += 1
        types_by_path.setdefault(_path_of(change.symbol_id), set()).add(
            change.change_type
        )

    files_with_symbols = set(types_by_path)
    files_added = sorted(
        path for path, types in types_by_path.items()
        if types == {ChangeType.ADDED}
    )
    files_deleted = sorted(
        path for path, types in types_by_path.items()
        if types == {ChangeType.REMOVED}
    )

    bullets: list[SummaryBullet] = []
    for rename in renames:
        bullets.append(SummaryBullet(
            text=(
                f"符号重命名：{rename.old_symbol_id} → {rename.new_symbol_id}"
                f"（配对方式 {rename.reason}，置信度 {rename.confidence:.2f}）"
            ),
            kind="rename",
            evidence=[rename.old_symbol_id, rename.new_symbol_id],
        ))
    for file_rename in change_set.file_renames:
        score = (
            f"，git 相似度 {file_rename.similarity}"
            if file_rename.similarity is not None else ""
        )
        bullets.append(SummaryBullet(
            text=f"文件重命名/移动：{file_rename.old_path} → {file_rename.new_path}{score}",
            kind="file_rename",
            evidence=[file_rename.old_path, file_rename.new_path],
        ))
    for path in sorted(types_by_path):
        types = types_by_path[path]
        parts = [
            f"新增 {sum(1 for c in change_set.symbol_changes
                        if _path_of(c.symbol_id) == path
                        and c.change_type == ChangeType.ADDED)}",
            f"删除 {sum(1 for c in change_set.symbol_changes
                        if _path_of(c.symbol_id) == path
                        and c.change_type == ChangeType.REMOVED)}",
            f"签名变更 {sum(1 for c in change_set.symbol_changes
                            if _path_of(c.symbol_id) == path
                            and c.change_type == ChangeType.SIGNATURE_CHANGED)}",
            f"函数体修改 {sum(1 for c in change_set.symbol_changes
                             if _path_of(c.symbol_id) == path
                             and c.change_type == ChangeType.MODIFIED)}",
        ]
        bullets.append(SummaryBullet(
            text=f"文件 {path}：" + "、".join(parts),
            kind="file",
            evidence=[path],
        ))

    return ChangeSummary(
        base_commit=change_set.base_commit,
        head_commit=change_set.head_commit,
        files_changed=len(change_set.changed_files),
        files_added=len(files_added),
        files_deleted=len(files_deleted),
        symbols_added=counts[ChangeType.ADDED],
        symbols_removed=counts[ChangeType.REMOVED],
        symbols_signature_changed=counts[ChangeType.SIGNATURE_CHANGED],
        symbols_modified=counts[ChangeType.MODIFIED],
        rename_pairs=list(renames),
        bullets=bullets,
    )


def supplement_summary(
    summary: ChangeSummary,
    changed_urns: Sequence[str],
    model_rows: Sequence[dict],
    *,
    model_version: str = "",
) -> ChangeSummaryResult:
    """模型补点白名单校验：只接受挂在本次变更符号 URN 上的非空文本备注。

    坏形状（非 dict / 缺字段）→ ``bad_shape``；空文本 → ``empty_text``；
    URN 不在白名单 → ``unknown_symbol``。确定性 bullets 只读不写，模型行
    永远只能追加备注。
    """
    whitelist = set(changed_urns)
    notes: list[SummaryNote] = []
    rejected: list[RejectedNote] = []
    for row in model_rows:
        if not isinstance(row, dict):
            rejected.append(RejectedNote(
                text="", symbol_urn="", reason="bad_shape",
                detail=f"模型行不是对象: {type(row).__name__}",
            ))
            continue
        text = str(row.get("text", "")).strip()
        symbol_urn = str(row.get("symbol_urn", "")).strip()
        if not symbol_urn or "text" not in row:
            rejected.append(RejectedNote(
                text=text, symbol_urn=symbol_urn,
                reason="bad_shape", detail="缺少 text/symbol_urn 字段",
            ))
            continue
        if not text:
            rejected.append(RejectedNote(
                text="", symbol_urn=symbol_urn, reason="empty_text",
            ))
            continue
        if symbol_urn not in whitelist:
            rejected.append(RejectedNote(
                text=text, symbol_urn=symbol_urn, reason="unknown_symbol",
                detail="备注挂载 URN 不在本次变更符号白名单内",
            ))
            continue
        notes.append(SummaryNote(text=text, symbol_urn=symbol_urn))
    return ChangeSummaryResult(
        summary=summary,
        notes=notes,
        rejected=rejected,
        generation_source="model" if notes else "deterministic",
        model_version=model_version,
    )


# ── 3. 回归选择精度回算（docs/06 口径，门限 0.8） ────────────────────────────


class CaseExecutionOutcome(BaseModel):
    """一次实际执行的用例级结果（worker 从 TestRun.case_results 投影）。"""

    case_urn: str
    status: CaseStatus


class SelectionPrecision(BaseModel):
    recommended_total: int
    executed_total: int
    evaluated: int                                    # |推荐 ∩ 实际执行|
    related_hits: int                                # 其中确实与变更相关
    precision: float | None = None                   # 空分母 → None（不展示百分比）
    full_suite_size: int = 0
    reduction_ratio: float | None = None             # 回归集缩减比例
    truth_total: int = 0
    truth_missed: int = 0                            # 确实相关但未被推荐
    gate_threshold: float = DEFAULT_PRECISION_GATE
    gate_passed: bool = False


def regression_selection_precision(
    *,
    recommended: Sequence[str],
    executed: Sequence[CaseExecutionOutcome],
    related_truth: Sequence[str],
    full_suite_size: int = 0,
    gate_threshold: float = DEFAULT_PRECISION_GATE,
) -> SelectionPrecision:
    """推荐精度回算纯函数（合成/真实执行结果同口径可喂）。

    口径（docs/06）：分母 = 推荐且实际执行的用例；分子 = 其中被缺陷/工单
    证据确认为相关的用例。推荐了但没执行的不进分母（无法回算相关性）。
    空分母给 ``precision=None`` 而不是 0——没有基线时禁止展示百分比结论。
    """
    recommended_set = set(recommended)
    executed_set = {outcome.case_urn for outcome in executed}
    truth_set = set(related_truth)

    evaluated_set = recommended_set & executed_set
    hits = evaluated_set & truth_set
    precision = (
        round(len(hits) / len(evaluated_set), 6) if evaluated_set else None
    )
    reduction = (
        round(1 - len(executed_set) / full_suite_size, 6)
        if full_suite_size > 0 else None
    )
    gate_passed = precision is not None and precision >= gate_threshold
    return SelectionPrecision(
        recommended_total=len(recommended_set),
        executed_total=len(executed_set),
        evaluated=len(evaluated_set),
        related_hits=len(hits),
        precision=precision,
        full_suite_size=full_suite_size,
        reduction_ratio=reduction,
        truth_total=len(truth_set),
        truth_missed=len(truth_set - recommended_set),
        gate_threshold=gate_threshold,
        gate_passed=gate_passed,
    )
