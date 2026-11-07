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
