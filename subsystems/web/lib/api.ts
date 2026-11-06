// 同源 fetch 封装：静态导出由 FastAPI 同端口托管，开发态经 next.config rewrites 代理。
import type {
  AskRequest,
  AskResponse,
  ConfigResponse,
  FeedbackKind,
  FeedbackReason,
  FeedbackSummaryRow,
  RunListItem,
  SourceEnvelope,
  TraceData,
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
