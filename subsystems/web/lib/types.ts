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
  score: number;
  authority_level: string;
}

export interface TraceData {
  run_id: string;
  question: string;
  intent: string;
  status: QaStatus;
  unknown_reason: string;
  attempts: number;
  regens: number;
  tokens: { in: number; out: number };
  latency_ms: number;
  degraded: string[];
  retrieval_event_id: number | null;
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
