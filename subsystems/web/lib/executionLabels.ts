// S3.5 执行账本/归因页面的中文标签与徽章 class 映射（与 docs/09 术语表口径一致）。
// 后端枚举经 model_dump(mode="json") 序列化为字符串；未知值回退原文，不渲染崩溃。
import type {
  CaseStatusStr,
  ExecutionRunStatus,
  FailureCategoryStr,
} from "./types";

export const RUN_STATUS_LABELS: Record<string, string> = {
  queued: "已排队",
  running: "执行中",
  passed: "通过",
  failed: "失败",
  error: "基础设施错误",
  timeout: "超时",
  canceled: "已取消",
};

export const CASE_STATUS_LABELS: Record<CaseStatusStr, string> = {
  passed: "通过",
  failed: "失败",
  skipped: "跳过",
  error: "错误",
};

// 归因五候选 + needs_human 的人话标签（docs/09 首现注释同口径）
export const VERDICT_LABELS: Record<FailureCategoryStr, string> = {
  real_defect: "真缺陷",
  case_outdated: "用例过期",
  flaky: "不稳定（flaky）",
  environment: "环境失败",
  product_change: "产品改动",
  needs_human: "需人审",
};

export function runStatusLabel(status: string): string {
  return RUN_STATUS_LABELS[status] ?? status;
}

export function caseStatusLabel(status: string): string {
  return CASE_STATUS_LABELS[status as CaseStatusStr] ?? status;
}

export function verdictLabel(category: string): string {
  return VERDICT_LABELS[category as FailureCategoryStr] ?? category;
}

// 短 hash：后端 run_id 为 run-<hex> 形态，列表只展示前 12 位，详情给全量
export function shortId(value: string, len = 12): string {
  return value.length > len ? `${value.slice(0, len)}…` : value;
}

export function runStatusClass(status: string): string {
  if (status === "passed") return "st st-confirmed";
  if (status === "failed" || status === "error" || status === "timeout")
    return "st st-failed";
  if (status === "queued" || status === "running") return "st st-pending";
  return "st"; // canceled 等中性态
}

export function caseStatusClass(status: string): string {
  if (status === "passed") return "st st-confirmed";
  if (status === "failed" || status === "error") return "st st-failed";
  return "st";
}

export function verdictClass(category: string): string {
  return `vc vc-${category}`;
}
