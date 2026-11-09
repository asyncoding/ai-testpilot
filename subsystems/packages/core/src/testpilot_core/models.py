"""统一领域模型（docs/07 §4，docs/08 各 FR 的数据契约）。

设计纪律：
- 所有外部资源带 source_system + 外部 id + version，URN 由 urn.make_urn 生成；
- 证据（Evidence）贯穿所有结论；无证据的事实性结论不得产出；
- 模型语言中立：TestRun 不出现 scheme/udid/pom 等平台专有字段。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

from .enums import (
    ActionLevel,
    ApprovalDecision,
    ArtifactType,
    AuthorityLevel,
    CaseStatus,
    ChangeType,
    FeedbackKind,
    FeedbackReason,
    OracleType,
    OutboxStatus,
    QaIntent,
    QaStatus,
    RunStatus,
    StaleStatus,
    VersionState,
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── 项目与仓库 ─────────────────────────────────────────────────────────────

class RepoRef(BaseModel):
    project: str
    repo_path: str  # 来自配置，禁止代码内写死机器绝对路径
    default_branch: str = "main"
    tech_stack: Literal["ios", "java", "other"] = "ios"


class EventCheckpoint(BaseModel):
    """增量扫描水位。"""

    source_system: str
    last_event_at: datetime | None = None
    last_cursor: str | None = None


# ── 知识：文档 / 切块 / 实体 / 关系 ─────────────────────────────────────────

class SourceDocument(BaseModel):
    urn: str
    source_system: str
    uri: str
    title: str = ""
    version: str | None = None  # etag / commit / 文档版本
    content_hash: str
    authority_level: AuthorityLevel = AuthorityLevel.L2_EXPERIENCE
    acl_scope: str = "default"  # 权限过滤用，与 IAM AccessScope 对齐
    branch: str = "main"
    mtime: datetime | None = None
    metadata_: dict[str, Any] = Field(default_factory=dict, alias="metadata")

    model_config = {"populate_by_name": True}


class KnowledgeChunk(BaseModel):
    urn: str
    doc_urn: str
    index: int
    kind: Literal["text", "code_symbol", "test_case", "incident", "rule"]
    text: str
    symbol_urn: str | None = None
    quality_score: float = 0.5
    branch: str = "main"
    valid_from: str | None = None  # commit
    valid_to: str | None = None
    stale: StaleStatus = StaleStatus.FRESH
    content_hash: str
    metadata_: dict[str, Any] = Field(default_factory=dict, alias="metadata")

    model_config = {"populate_by_name": True}


class VectorItem(BaseModel):
    """向量库写入单元（S1.2）：id 直接用 chunk URN，稳定且幂等。

    metadata 承载 project/branch/acl_scope/kind/content_hash/quality，
    供向量库侧过滤；不同 profile 的 collection 名由配置决定。
    """

    id: str
    embedding: list[float]
    text: str = ""
    metadata_: dict[str, Any] = Field(default_factory=dict, alias="metadata")

    model_config = {"populate_by_name": True}


class VectorHit(BaseModel):
    """向量库单条命中；distance 为引擎原生距离（越小越近），已按升序排列。"""

    id: str
    distance: float
    metadata_: dict[str, Any] = Field(default_factory=dict, alias="metadata")

    model_config = {"populate_by_name": True}


class Entity(BaseModel):
    urn: str
    kind: Literal["requirement", "test_case", "code_symbol", "issue", "doc_section", "incident", "pipeline"]
    name: str
    branch: str = "main"
    metadata_: dict[str, Any] = Field(default_factory=dict, alias="metadata")

    model_config = {"populate_by_name": True}


class Relation(BaseModel):
    """实体关系，必须带 confidence 与证据（docs/00 §3.1）。"""

    urn: str
    source_urn: str
    target_urn: str
    kind: str  # covers / fixes / references / triggers ...
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_urns: list[str] = Field(default_factory=list)


# ── 需求 / 测试点 / 用例 ───────────────────────────────────────────────────

class AcceptanceCriterion(BaseModel):
    text: str
    present: bool = True  # False = 需求里缺失、显式标缺（不编造）


class RequirementSkeleton(BaseModel):
    """自由格式需求抽出的固定骨架（docs/00 §3.2）。"""

    source_urn: str
    features: list[str]
    rules: list[str]
    acceptance_criteria: list[AcceptanceCriterion]
    modules: list[str]


class TestPoint(BaseModel):
    text: str
    priority: Literal["P0", "P1", "P2", "P3"] = "P2"
    source_refs: list[str] = Field(default_factory=list)  # 需求条目/事故/符号 URN
    # S2.3 FR-TD-002 元数据（均带默认值，S0 既有 TestCaseDraft 构造不受影响）：
    # fr_id 把测试点钉回白名单功能点（盲标 P0/P1 漏点统计的连接键）；
    # kind 是封闭小词表；provenance 区分确定性脊柱与模型补点（幻觉追责到来源）。
    fr_id: str = ""
    kind: Literal[
        "functional", "rule", "acceptance", "boundary", "negative", "model_proposed"
    ] = "functional"
    provenance: Literal["deterministic", "model"] = "deterministic"


class Oracle(BaseModel):
    type: OracleType
    expectation: str
    source_refs: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class TestCase(BaseModel):
    urn: str
    external_id: str
    title: str
    preconditions: list[str] = Field(default_factory=list)
    steps: list[str]
    oracles: list[Oracle] = Field(default_factory=list)
    oracle_missing: bool = False  # 只有步骤无预期（docs/08 FR-CON-003）
    exploratory: bool = False  # 挖不到 oracle 的探索性用例，不进自动化链路
    tags: list[str] = Field(default_factory=list)
    requirement_urn: str | None = None
    version: str | None = None
    # S2.1 文档解析期元数据（FR-CON-003）：模块归属/优先级/生命周期/原文行号，
    # 均带默认值，AI 草稿与执行链路构造 TestCase 时不受影响
    module: str = ""
    priority: Literal["P0", "P1", "P2", "P3", ""] = ""
    lifecycle: Literal["active", "cancelled"] = "active"
    source_line: int | None = None  # 1-based，标题行或表格行，供证据反查


class TestCaseDraft(BaseModel):
    """AI 生成草稿：不进正式库，必须经 HITL（docs/08 FR-TD-004）。"""

    draft_id: str
    requirement_urn: str | None
    test_points: list[TestPoint]
    cases: list[TestCase]
    model_version: str
    created_at: datetime = Field(default_factory=utcnow)


# ── 代码 ──────────────────────────────────────────────────────────────────

class CodeSymbol(BaseModel):
    """稳定符号 ID，不绑行号（docs/00 §4.2）；行范围随版本变化。"""

    urn: str
    language: str
    symbol_id: str  # 如 MissionCoordinator.schedulePrimary
    file_path: str
    line_start: int | None = None
    line_end: int | None = None
    signature: str = ""
    commit: str
    body_hash: str
    calls: list[str] = Field(default_factory=list)
    called_by: list[str] = Field(default_factory=list)


class SymbolChange(BaseModel):
    symbol_id: str
    change_type: ChangeType


class ChangeSet(BaseModel):
    repo: RepoRef
    base_commit: str
    head_commit: str
    changed_files: list[str] = Field(default_factory=list)
    symbol_changes: list[SymbolChange] = Field(default_factory=list)
    diff_uri: str | None = None
    merge_request: str | None = None


class SymbolSnapshot(BaseModel):
    symbol: CodeSymbol
    exists: bool
    body_changed: bool


class LiveVerifyResult(BaseModel):
    """live-verify 一次校验的结构化结果（docs/08 FR-QA-002）。

    端口 KnowledgeSearchPort.live_verify 仍以 dict（model_dump）返回，
    保证工作流侧语言中立；每次校验（含失败/降级）都在 verification_events
    持久化一行——FR-QA-002 AC2 要求代码类问题 100% 有 trace 可查的工具调用。
    """

    urn: str
    kind: str = ""  # text | code_symbol；URN 解析失败等早降级时还不知道 kind
    requested_commit: str = ""  # 调用方指定的 commit；空串表示 HEAD
    head_commit: str = ""  # 实际校验时解析到的 commit
    exists: bool | None = None  # 目标版本符号/锚点是否存在；git 不可达时为 None
    verified: bool = False  # 索引证据在目标版本仍成立（存在且 hash 一致）
    body_changed: bool | None = None  # True=stale-index；None=无法判定
    reason: str = ""
    degraded: bool = False  # git 不可达/路径越界/URN 不可解析等：结果不可信
    error: str = ""
    indexed_commit: str | None = None  # 索引切块 valid_from
    indexed_chunker_version: str | None = None
    current_chunker_version: str = ""
    latency_ms: int = 0
    # 留痕维度（verification_events 投影列）；raw 放排障细节（git 路径/anchor/异常类）
    project: str = ""
    branch: str = ""
    raw: dict[str, Any] = Field(default_factory=dict)


# ── 执行（语言中立契约，docs/08 FR-EX-001）────────────────────────────────

class EnvironmentRequirements(BaseModel):
    capabilities: list[str] = Field(default_factory=list)  # 如 ios.simulator / ios.device
    labels: dict[str, str] = Field(default_factory=dict)


class CaseSelector(BaseModel):
    """选择执行哪些用例；平台专有表达（-only-testing / --tests）由适配器解释。"""

    test_ids: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    fixture_hint: str | None = None  # 离线录制适配器选夹具用，真实适配器忽略


class TestExecutionRequest(BaseModel):
    request_id: str
    idempotency_key: str
    project: str
    selector: CaseSelector
    commit_sha: str
    environment: EnvironmentRequirements = Field(default_factory=EnvironmentRequirements)
    build_id: str | None = None
    triggered_by: str = "agent"
    daily_budget_key: str | None = None  # 重跑配额账本 key


class ArtifactRef(BaseModel):
    type: ArtifactType
    uri: str
    content_hash: str | None = None
    retention_days: int = 14


class CaseResult(BaseModel):
    external_case_id: str
    node_id: str = ""
    status: CaseStatus
    duration_ms: int | None = None
    failure_type: str | None = None
    message: str = ""
    stack_top: str = ""
    artifact_urns: list[str] = Field(default_factory=list)


class EnvironmentInfo(BaseModel):
    provider: str  # ios-simulator / junit-local / k8s ...
    env_id: str
    capabilities: list[str] = Field(default_factory=list)
    version: str = ""


class TestRun(BaseModel):
    run_id: str
    idempotency_key: str
    project: str
    commit_sha: str
    build_id: str | None = None
    status: RunStatus
    environment: EnvironmentInfo
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: int | None = None
    case_results: list[CaseResult] = Field(default_factory=list)
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    raw_report_ref: str | None = None
    runner_capability_version: str = "unknown"
    uncovered_layers: list[str] = Field(default_factory=list)  # 显式声明未覆盖层（真机/签名…）


# ── 工单 ──────────────────────────────────────────────────────────────────

class Issue(BaseModel):
    urn: str
    external_id: str
    title: str
    status: str
    severity: str = ""
    symptom: str = ""
    repro_steps: list[str] = Field(default_factory=list)
    root_cause: str | None = None
    fix_commit: str | None = None
    related_case_ids: list[str] = Field(default_factory=list)
    related_run_id: str | None = None
    uri: str = ""
    version: str | None = None


class IssueDraft(BaseModel):
    title: str
    symptom: str
    repro_steps: list[str] = Field(default_factory=list)
    evidence_urns: list[str] = Field(default_factory=list)
    run_id: str | None = None
    uncovered_layers: list[str] = Field(default_factory=list)


class CommentDraft(BaseModel):
    body: str
    evidence_urns: list[str] = Field(default_factory=list)
    run_id: str | None = None


# ── 证据 ──────────────────────────────────────────────────────────────────

class Evidence(BaseModel):
    """每条事实性结论必须能反查到证据（docs/00 §0、docs/06 护栏）。"""

    urn: str
    source_system: str
    uri: str
    version: str | None = None
    content_hash: str | None = None
    query: str = ""  # 查询条件/谓词本身留痕（日志归因可复现）
    fetched_at: datetime = Field(default_factory=utcnow)
    fetched_by: str = "agent"
    snippet: str = ""
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


# ── 审批与外部副作用账本 ──────────────────────────────────────────────────

class Approval(BaseModel):
    approval_id: str
    action_level: ActionLevel
    target_system: str
    request_summary: str
    request_diff: dict[str, Any] = Field(default_factory=dict)
    decision: ApprovalDecision = ApprovalDecision.PENDING
    reviewer: str | None = None
    reason: str = ""
    based_on_versions: dict[str, str] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)
    decided_at: datetime | None = None


class OutboxRecord(BaseModel):
    """先持久化账本，再发起外部副作用（CLAUDE.md 工程纪律 / docs/08 FR-WR-002）。"""

    id: int | None = None
    idempotency_key: str
    action_level: ActionLevel
    target_system: str
    request_body: dict[str, Any]
    status: OutboxStatus = OutboxStatus.PENDING
    external_ref: str | None = None
    approval_id: str
    attempts: int = 0
    rollback_note: str = ""
    error: str = ""
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class PreferenceSample(BaseModel):
    """人工修改 diff 偏好样本（docs/08 FR-TD-004：diff 即偏好样本）。

    只在 reviewer 选择 modify 时产生；original/revised 双版本 + 字段级 diff 留痕，
    S2.6 KNO-002 再做 L3 资产版本化与 few-shot 回流，本里程碑只负责忠实落库。
    """

    sample_id: str
    case_urn: str
    suggestion_urn: str
    reviewer: str
    reason: str = ""
    original: dict[str, Any]
    revised: dict[str, Any]
    diff: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)


# ── 反馈 ──────────────────────────────────────────────────────────────────

class Feedback(BaseModel):
    run_id: str
    target_urn: str
    kind: FeedbackKind
    reason: FeedbackReason | None = None
    detail: str = ""
    model_version: str = ""
    created_at: datetime = Field(default_factory=utcnow)


# ── 问答工作流（docs/08 FR-QA-001～004，S1.4）──────────────────────────────

class AskRequest(BaseModel):
    """一次问答的入参；S1.5 的 HTTP router 只做本模型的薄包装。"""

    question: str
    project: str = ""
    branch: str = "main"
    acl_scope: str = "default"


class DraftCitation(BaseModel):
    """模型生成草稿中的引用：只能引用白名单 URN。

    P2 起模型只输出 anchor（证据中逐字出现的短文本/标识符），不再输出 quote——
    7B 模型在长上下文里逐字摘抄长代码会系统性腐化（实测 3/3 改写插值/省略号）。
    quote 由服务端按 anchor 确定性渲染（answering.render_citation_quote），
    未命中 fail-closed，杜绝模型转述进入最终引用。
    """

    urn: str
    anchor: str = ""


class AnswerDraft(BaseModel):
    """structured(schema=AnswerDraft) 的输出契约；citations 受 URN 白名单约束。"""

    summary: str
    sections: list[str] = Field(default_factory=list)
    citations: list[DraftCitation] = Field(default_factory=list)


class QaCitation(BaseModel):
    """装配后的最终引用：补齐可点击 URI、hash 与三态版本标注（FR-QA-001 AC1）。"""

    urn: str
    uri: str = ""
    quote: str = ""
    version_state: VersionState
    content_hash: str = ""
    version_ref: str = ""  # verified_at 时记录 HEAD 短 sha，其余为空


class HistoryTestimony(BaseModel):
    """历史证词（FR-QA-003）：text 为语料逐字原文，模型全程不接触、不改写。"""

    text: str
    doc_urn: str
    source_uri: str = ""
    date: str = ""
    version: str = ""


class AssembledAnswer(BaseModel):
    markdown: str
    sections: list[str] = Field(default_factory=list)
    citations: list[QaCitation] = Field(default_factory=list)
    history: list[HistoryTestimony] = Field(default_factory=list)


class SufficiencyVerdict(BaseModel):
    sufficient: bool
    reason: str = ""  # empty_retrieval / weak_retrieval / ""
    score: float = 0.0


class CitationVerdict(BaseModel):
    ok: bool
    errors: list[str] = Field(default_factory=list)


class AclVerdict(BaseModel):
    outcome: Literal["allow", "deny", "out_of_scope"]
    reason: str = ""


class AskResponse(BaseModel):
    """QaService.ask 的返回；qa_runs 落库 payload 与本模型同构。"""

    run_id: str
    status: QaStatus
    answer: AssembledAnswer | None = None
    unknown_reason: str = ""
    # v6：最后一次 citation_check 的具体错误；answered 时为空列表
    citation_errors: list[str] = Field(default_factory=list)
    # v7：意图来源 regex_bypass（符号形态免模型）/ model / fallback；老行为为空串
    intent_source: str = ""
    degraded: list[str] = Field(default_factory=list)
    intent: QaIntent | None = None
    model_version: str = ""
    prompt_version: str = ""
    params_version: str = ""
    data_version: str = ""


# ── 评测 ──────────────────────────────────────────────────────────────────

class EvalCase(BaseModel):
    case_id: str
    kind: Literal["qa", "test_design", "attribution", "negative"]
    question: str
    ground_truth: dict[str, Any] = Field(default_factory=dict)
    required_evidence: list[str] = Field(default_factory=list)
    holdout: bool = False  # 留出题：不入库、不进 few-shot


class EvalVersion(BaseModel):
    """每个评测数字必须可复算：数据/模型/prompt/参数四元组（docs/06 §6.1）。"""

    data_version: str
    model_version: str
    prompt_version: str
    params_version: str
    degraded_path: bool = False  # 是否走了降级路径


class EvalRun(BaseModel):
    eval_run_id: str
    version: EvalVersion
    metrics: dict[str, float] = Field(default_factory=dict)
    report_uri: str | None = None
    created_at: datetime = Field(default_factory=utcnow)


# ── 事件信封（docs/04 §8）─────────────────────────────────────────────────

class EventEnvelope(BaseModel):
    event_id: str
    event_type: str  # CommitPushed / PipelineFinished / TestRunFinished ...
    source_system: str
    occurred_at: datetime
    project: str
    subject_ref: str
    version: str = "1"
    actor: str = ""
    payload_uri: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str
