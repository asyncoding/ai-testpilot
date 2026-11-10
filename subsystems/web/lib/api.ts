// 同源 fetch 封装：静态导出由 FastAPI 同端口托管，开发态经 next.config rewrites 代理。
import type {
  ApprovalRecord,
  AskRequest,
  AskResponse,
  CancelRunResult,
  CaseDecisionHttpBody,
  CaseDecisionPlan,
  ConfigResponse,
  DispatchReport,
  ExecutionRunDetail,
  ExecutionRunListItem,
  FeedbackKind,
  FeedbackReason,
  FeedbackSummaryRow,
  OutboxRecordView,
  ReviewQueueItem,
  RunListItem,
  SourceEnvelope,
  TraceData,
  WritePolicyView,
} from "./types";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!resp.ok) {
    // 后端统一错误体 {error:{type,message}}；原生 422 是 pydantic 校验结构
    let detail = `${resp.status}`;
    try {
      const body = await resp.json();
      detail = body?.error?.message || JSON.stringify(body);
    } catch {
      // 非 JSON 错误体，保留状态码即可
    }
    throw new Error(`HTTP ${resp.status}: ${detail}`);
  }
  return (await resp.json()) as T;
}

export function fetchConfig(): Promise<ConfigResponse> {
  return request<ConfigResponse>("/api/config");
}

export function askQuestion(body: AskRequest): Promise<AskResponse> {
  return request<AskResponse>("/api/qa/ask", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function listRuns(status?: string): Promise<{ runs: RunListItem[] }> {
  const qs = status ? `?status=${encodeURIComponent(status)}` : "";
  return request<{ runs: RunListItem[] }>(`/api/qa/runs${qs}`);
}

export function fetchSource(runId: string, urn: string): Promise<SourceEnvelope> {
  // URN 含 '/' 与 '#'：必须全量百分号编码，否则 '#' 被当 fragment 截断（后端 404）
  return request<SourceEnvelope>(
    `/api/qa/runs/${encodeURIComponent(runId)}/sources/${encodeURIComponent(urn)}`,
  );
}

export function fetchTrace(runId: string): Promise<TraceData> {
  return request<TraceData>(`/api/qa/runs/${encodeURIComponent(runId)}/trace`);
}

export function submitFeedback(
  runId: string,
  kind: FeedbackKind,
  reason?: FeedbackReason | null,
  detail?: string,
): Promise<{ id: number }> {
  return request<{ id: number }>(
    `/api/qa/runs/${encodeURIComponent(runId)}/feedback`,
    {
      method: "POST",
      body: JSON.stringify({ kind, reason: reason ?? null, detail: detail ?? "" }),
    },
  );
}

export function fetchFeedbackSummary(): Promise<FeedbackSummaryRow[]> {
  return request<FeedbackSummaryRow[]>("/api/qa/feedback/summary");
}

// ── S2.5 人审/回写（/api/approvals/*）────────────────────────────────────────

export function fetchReviewQueue(): Promise<{ queue: ReviewQueueItem[] }> {
  return request<{ queue: ReviewQueueItem[] }>("/api/approvals/queue");
}

export function decideCase(
  caseUrn: string,
  body: CaseDecisionHttpBody,
): Promise<CaseDecisionPlan> {
  // URN 含 '/' 与 '#'，必须全量百分号编码（'#' 否则被当 fragment 截断 → 404）
  return request<CaseDecisionPlan>(
    `/api/approvals/cases/${encodeURIComponent(caseUrn)}/decide`,
    { method: "POST", body: JSON.stringify(body) },
  );
}

export function listApprovalRecords(
  limit = 100,
): Promise<{ records: ApprovalRecord[] }> {
  return request<{ records: ApprovalRecord[] }>(
    `/api/approvals/records?limit=${limit}`,
  );
}

export function listOutboxRecords(): Promise<{ records: OutboxRecordView[] }> {
  return request<{ records: OutboxRecordView[] }>("/api/approvals/outbox");
}

export function dispatchOutbox(): Promise<DispatchReport> {
  return request<DispatchReport>("/api/approvals/outbox/dispatch", {
    method: "POST",
    body: "{}",
  });
}

export function getWritePolicy(): Promise<WritePolicyView> {
  return request<WritePolicyView>("/api/approvals/write-policy");
}

export function setWritePolicy(runtimeDisabled: boolean): Promise<WritePolicyView> {
  return request<WritePolicyView>("/api/approvals/write-policy", {
    method: "POST",
    body: JSON.stringify({ runtime_disabled: runtimeDisabled }),
  });
}

// ── S3.5 测试执行账本（/api/runs/*，FR-EX-001/002/007）────────────────────────

export function listExecutionRuns(
  filters: { status?: string; project?: string } = {},
): Promise<{ runs: ExecutionRunListItem[] }> {
  const params = new URLSearchParams();
  if (filters.status) params.set("status", filters.status);
  if (filters.project) params.set("project", filters.project);
  const qs = params.toString();
  return request<{ runs: ExecutionRunListItem[] }>(
    `/api/runs${qs ? `?${qs}` : ""}`,
  );
}

export function fetchExecutionRun(runId: string): Promise<ExecutionRunDetail> {
  return request<ExecutionRunDetail>(
    `/api/runs/${encodeURIComponent(runId)}`,
  );
}

export function cancelExecutionRun(runId: string): Promise<CancelRunResult> {
  return request<CancelRunResult>(
    `/api/runs/${encodeURIComponent(runId)}/cancel`,
    { method: "POST", body: "{}" },
  );
}
