// S2.5 审批页展示标签：镜像后端枚举/字面量，纯前端映射，不含请求逻辑。
import type {
  OracleTypeStr,
  ReviewAction,
  SuggestionSourceKind,
} from "./types";

// 与 testpilot_core.testcase.EXPLICIT_TYPE_LABELS 对齐（回写 Markdown 同款显式标签）
export const ORACLE_TYPE_LABELS: Record<OracleTypeStr, string> = {
  ui_assertion: "【UI 断言】",
  log_assertion: "【日志断言】",
  state_assertion: "【状态断言】",
  manual: "【人工确认】",
};

export const ORACLE_TYPE_OPTIONS: OracleTypeStr[] = [
  "ui_assertion",
  "log_assertion",
  "state_assertion",
  "manual",
];

// 对齐 MinedOracle.source_kind 五值（oracle_mining.py）
export const SOURCE_KIND_LABELS: Record<SuggestionSourceKind, string> = {
  requirement_ac: "需求验收标准",
  existing_assertion: "现有测试断言",
  incident: "历史缺陷",
  code_symbol: "代码符号",
  model_proposed: "模型补点",
};

export const REVIEW_ACTION_LABELS: Record<ReviewAction, string> = {
  adopt: "采纳",
  modify: "修改",
  reject: "拒绝",
};

export const OUTBOX_STATUS_LABELS: Record<string, string> = {
  pending: "待派发",
  sent: "已发送",
  confirmed: "已确认",
  failed: "失败可重试",
};

// URN 较长，表格/卡片里只展示尾段，完整值放 title 属性
export function shortUrn(urn: string, keep = 48): string {
  return urn.length <= keep ? urn : `…${urn.slice(-(keep - 1))}`;
}
