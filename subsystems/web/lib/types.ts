// 手写最小类型：镜像后端 AskResponse / qa_runs 契约（不引 openapi 生成，POC 保持轻量）。
// 后端枚举经 model_dump(mode="json") 已序列化为字符串字面量。

export type QaStatus = "answered" | "unknown" | "denied" | "error";
export type VersionState = "consistent" | "verified_at" | "unknown";
export type FeedbackKind = "useful" | "not_useful";
export type FeedbackReason =
  | "not_retrieved"
  | "stale_reference"
  | "off_topic"
  | "factual_error"
  | "other";

export interface QaCitation {
  urn: string;
  uri: string;
  quote: string;
  version_state: VersionState;
  content_hash: string;
  version_ref: string;
}

export interface HistoryTestimony {
  text: string;
  doc_urn: string;
  source_uri: string;
  date: string;
  version: string;
}

export interface AssembledAnswer {
  markdown: string;
  sections: string[];
  citations: QaCitation[];
  history: HistoryTestimony[];
}

export interface AskResponse {
  run_id: string;
  status: QaStatus;
  answer: AssembledAnswer | null;
  unknown_reason: string;
  // 最后一次 citation_check 的具体错误（"前缀: 详情"整串，前缀见 CITATION_ERROR_LABELS）；
  // 空数组=校验通过或未进生成节点。v6 schema 起持久化；静态前端可能比后端新（版本错配），
  // 老后端/老 run 的响应里整个字段缺失，渲染侧必须 ?? [] 兜底。
  citation_errors?: string[];
  // v7：regex_bypass（符号形态免模型）/ model / fallback；老 run/老后端缺失或空串
  intent_source?: string;
  degraded: string[];
  intent: string | null;
  model_version: string;
  prompt_version: string;
  params_version: string;
  data_version: string;
}

export interface AskRequest {
  question: string;
  project?: string;
  branch?: string;
  acl_scope?: string;
  case_key?: string;
}

export interface ConfigResponse {
  profile: string;
  project: string;
  branch: string;
  model_adapter: string;
  search_adapter: string;
  recorded: boolean;
  external_write_enabled: boolean;
  tracer: string;
}

export interface RunListItem {
  run_id: string;
  question: string;
  status: QaStatus;
  intent: string;
  unknown_reason: string;
  created_at: string;
}

// GET /api/qa/runs/{id}/sources/{urn} 的原文信封
export interface SourceEnvelope {
  urn: string;
  kind: string;
  text: string;
  content_hash: string;
  source_uri: string;
  doc_urn: string;
  authority_level: string;
  branch: string;
  stale: string | boolean;
}

export interface TraceEvidence {
  urn: string;
  doc_urn: string;
  kind: string;
  // 融合后排名（1-based）与命中通道（fts/vector，可双路）；比 RRF 小分值更能解释排序。
  // 两者均为 P1/P3 投影新增：v5 时代老后端的 trace 只给 score，缺失时渲染侧回退序号/「—」
  rank?: number;
  channels?: string[];
  score: number;
  authority_level: string;
}

export interface TraceData {
  run_id: string;
  question: string;
  intent: string;
  // v7 意图来源：regex_bypass / model / fallback；v7 之前的老 run/老后端整个字段缺失
  intent_source?: string;
  status: QaStatus;
  unknown_reason: string;
  // v6 起持久化；老后端的 trace 响应无此字段，渲染侧 ?? [] 兜底（曾致 undefined.length 崩溃）
  citation_errors?: string[];
  attempts: number;
  regens: number;
  tokens: { in: number; out: number };
  latency_ms: number;
  degraded: string[];
  retrieval_event_id: number | null;
  // P3 精确符号路由：entities 唯一精确命中时确定性置顶（false/缺失=走常规 RRF 融合）；
  // 老后端 trace 无这组字段
  symbol_pinned?: boolean;
  symbol_pin_urn?: string;
  symbol_pin_name?: string;
  evidence: TraceEvidence[];
  verify_results: unknown[];
  citations: { urn: string; version_state: VersionState; version_ref: string }[];
  versions: { model: string; prompt: string; params: string; data: string };
  thread_id: string;
  created_at: string;
  langfuse: { host: string; session_url: string } | null;
}

export interface FeedbackSummaryRow {
  kind: string;
  reason: string;
  n: number;
}

export interface ApiError {
  error: { type: string; message: string };
}

// ── S2.5 用例草稿人审与回写（FR-TD-004 / FR-WR-001 / FR-UI-002）──────────────
// 镜像后端 approval_api 的 model_dump(mode="json") 载荷；枚举均为字符串字面量。

export type OracleTypeStr =
  | "ui_assertion"
  | "log_assertion"
  | "state_assertion"
  | "manual";
export type SuggestionStatus = "pending" | "approved" | "rejected";
export type ReviewAction = "adopt" | "modify" | "reject";
export type ApprovalDecisionStr = "pending" | "approved" | "rejected";
export type OutboxStatusStr = "pending" | "sent" | "confirmed" | "failed";
export type SuggestionSourceKind =
  | "requirement_ac"
  | "existing_assertion"
  | "incident"
  | "code_symbol"
  | "model_proposed";
export type SuggestionProvenance = "deterministic" | "model";

export interface MinedOracleView {
  type: OracleTypeStr;
  expectation: string;
  source_refs: string[];
  confidence: number;
  source_kind: SuggestionSourceKind;
  provenance: SuggestionProvenance;
  // docs/16 步骤锚点：null=用例级（全程）断言；老后端/老数据缺字段时渲染侧 ?? null 兜底
  step_id: number | null;
}

export interface SuggestionView {
  urn: string;
  case_urn: string;
  external_id: string;
  status: SuggestionStatus | string;
  exploratory: boolean;
  oracle: MinedOracleView;
}

export interface ReviewQueueItem {
  case_urn: string;
  external_id: string;
  title: string;
  priority: string;
  module: string;
  exploratory: boolean;
  oracle_missing: boolean;
  // docs/16 §4.5：审批页前置/步骤展示，数据来自 test_cases.raw_json，老后端可能缺字段
  steps?: string[];
  preconditions?: string[];
  // docs/16 §4.3 C2：背景与平台约束只读展示，不可作为断言依据，老后端可能缺字段
  context_notes?: string[];
  suggestions: SuggestionView[];
}

export interface SuggestionDecisionHttp {
  suggestion_urn: string;
  action: ReviewAction;
  // modify 时可改类型/期望；adopt/reject 留 null（携带会被 core fail-closed 拒绝）
  oracle_type?: OracleTypeStr | null;
  expectation?: string | null;
}

export interface CaseDecisionHttpBody {
  decision: Exclude<ApprovalDecisionStr, "pending">;
  reviewer: string;
  reason?: string;
  items: SuggestionDecisionHttp[];
}

export interface ApprovalRecord {
  approval_id: string;
  action_level: string; // 序列化值如 "R2"
  target_system: string;
  request_summary: string;
  request_diff: Record<string, unknown>;
  decision: ApprovalDecisionStr;
  reviewer: string | null;
  reason: string;
  based_on_versions: Record<string, string>;
  created_at: string;
  decided_at: string | null;
}

export interface OutboxRecordView {
  id: number | null;
  idempotency_key: string;
  action_level: string;
  target_system: string;
  // 回写场景含 markdown/oracles/case_external_id 等；保持开放索引以便 details 直出
  request_body: Record<string, string>;
  status: OutboxStatusStr | string;
  external_ref: string | null;
  approval_id: string;
  attempts: number;
  rollback_note: string;
  error: string;
  created_at: string;
  updated_at: string;
}

export interface SuggestionUpdateView {
  urn: string;
  status: string;
  reason: string;
}

export interface CaseDecisionPlan {
  approval: ApprovalRecord;
  outbox: OutboxRecordView | null; // 整案拒绝时为 null（无外部副作用）
  preference_samples: unknown[];
  suggestion_updates: SuggestionUpdateView[];
  final_oracles: unknown[];
  markdown: string;
  idempotency_key: string;
}

export interface WritePolicyView {
  profile_external_write_enabled: boolean;
  runtime_disabled: boolean;
  effective_writes_enabled: boolean;
}

export interface DispatchReport {
  status: "succeeded" | "partial" | string;
  scanned: number;
  dispatched: number;
  confirmed: number;
  blocked: number;
  failed: number;
  skipped_unsupported: number;
  results: unknown[];
}

// ── S3.5 测试执行账本与失败归因（/api/runs/*，FR-EX-001/002/007）──────────────
// 镜像 workers.execution_service 投影与 core TestRun/AttributionVerdict 的
// model_dump(mode="json") 载荷；枚举均为字符串字面量，老 run 缺字段时渲染侧 ?? 兜底。

export type ExecutionRunStatus =
  | "queued"
  | "running"
  | "passed"
  | "failed"
  | "error"
  | "timeout"
  | "canceled";
export type CaseStatusStr = "passed" | "failed" | "skipped" | "error";
export type FailureCategoryStr =
  | "real_defect"
  | "case_outdated"
  | "flaky"
  | "environment"
  | "product_change"
  | "needs_human";

export interface ExecutionRunListItem {
  run_id: string;
  idempotency_key: string;
  project: string;
  commit_sha: string;
  status: ExecutionRunStatus | string;
  error: string;
  created_at: string;
  updated_at: string;
  // S3.5a 归因投影：queued/running 行恒为 0（raw_json 尚未回写）
  verdict_total: number;
  needs_human: number;
}

export interface EnvironmentInfoView {
  provider: string;
  env_id: string;
  capabilities: string[];
  version: string;
}

export interface ArtifactRefView {
  type: string;
  uri: string;
  content_hash: string | null;
  retention_days: number;
}

export interface CaseResultView {
  external_case_id: string;
  node_id: string;
  status: CaseStatusStr | string;
  duration_ms: number | null;
  failure_type: string | null;
  message: string;
  stack_top: string;
  artifact_urns: string[];
}

export interface AttributionCandidateView {
  category: FailureCategoryStr | string;
  confidence: number;
  reasons: string[];
  evidence_refs: string[];
}

export interface AttributionVerdictView {
  case_id: string;
  category: FailureCategoryStr | string;
  confidence: number;
  candidates: AttributionCandidateView[];
  evidence_refs: string[];
  uncovered_layers: string[];
  needs_human_reason: string;
  rule_version: string;
}

export interface TestRunView {
  run_id: string;
  idempotency_key: string;
  project: string;
  commit_sha: string;
  build_id: string | null;
  status: ExecutionRunStatus | string;
  environment: EnvironmentInfoView;
  started_at: string | null;
  finished_at: string | null;
  duration_ms: number | null;
  case_results: CaseResultView[];
  artifacts: ArtifactRefView[];
  raw_report_ref: string | null;
  runner_capability_version: string;
  uncovered_layers: string[];
  attributions: AttributionVerdictView[];
}

export interface ExecutionRunDetail {
  run_id: string;
  idempotency_key: string;
  project: string;
  commit_sha: string;
  status: ExecutionRunStatus | string;
  error: string;
  created_at: string;
  updated_at: string;
  // queued/running 行 raw_json 为空：只有账本投影，run=null（前端必须显式处理）
  run: TestRunView | null;
}

export interface CancelRunResult {
  run_id: string;
  cancel_requested: boolean;
  status: string;
}

// ── S4.5 知识管理页（FR-UI-004，/api/knowledge/*）────────────────────────────

export interface KnowledgeGovernance {
  owner: string;
  authority_level: string | null;
  updated_by: string;
  updated_at: string;
  history: {
    at: string;
    by: string;
    owner: string | null;
    authority_level: string | null;
  }[];
}

export interface KnowledgeDocumentRow {
  urn: string;
  source_system: string;
  uri: string;
  title: string;
  version: string | null;
  authority_level: string;
  branch: string;
  mtime: string | null;
  content_hash: string;
  chunk_count: number;
  stale_count: number;
  max_quality: number;
  owner: string;
  governance: KnowledgeGovernance | Record<string, never>;
}

export interface KnowledgeChunkRow {
  urn: string;
  idx: number;
  kind: string;
  symbol_urn: string | null;
  quality_score: number;
  stale: "fresh" | "stale" | string;
  valid_from: string | null;
  valid_to: string | null;
  content_hash: string;
}

export interface StaleChunkRow {
  urn: string;
  doc_urn: string;
  symbol_urn: string | null;
  valid_from: string | null;
  valid_to: string | null;
  content_hash: string;
}

export interface KnowledgeEntityRow {
  urn: string;
  kind: string;
  name: string;
  branch: string;
  doc_urn: string | null;
}

export interface GovernanceEditBody {
  owner?: string;
  authority_level?: string;
  edited_by?: string;
}

export interface GovernanceEditResult {
  urn: string;
  owner: string;
  authority_level: string;
  governance: KnowledgeGovernance;
}

// ── H2 人工闸门（docs/15 §7，hybrid-plan-v1）────────────────────────────────

export type GateStatus = "pending" | "responded" | "expired" | "canceled";
export type GateActor = "human:action" | "human:verdict" | string;
export type VerdictFieldKind = "boolean" | "choice" | "text";

export interface VerdictFieldView {
  key: string;
  label: string;
  kind: VerdictFieldKind;
  options: string[];
  required: boolean;
}

export interface VerdictFormView {
  fields: VerdictFieldView[];
  require_evidence: boolean;
}

export interface HumanVerdictView {
  gate_id: string;
  values: Record<string, unknown>;
  evidence_urns: string[];
  note: string;
  decided_by: string;
  decided_at: string;
}

export interface GateView {
  gate_id: string;
  session_id: string;
  step_id: string;
  actor: GateActor;
  prompt: string;
  verdict_form: VerdictFormView | null;
  evidence_urns: string[];
  deadline: string;
  status: GateStatus;
  created_at: string;
  response: HumanVerdictView | null;
  renewed_at: string | null;
}

export interface GateOpenBody {
  session_id: string;
  step_id: string;
  actor: GateActor;
  action: string;
  prompt: string;
  params?: Record<string, unknown>;
  verdict_form?: VerdictFormView | null;
  evidence_urns?: string[];
  deadline_seconds?: number;
}

export interface GateRespondBody {
  values: Record<string, unknown>;
  note?: string;
  evidence_urns: string[];
  decided_by: string;
}
