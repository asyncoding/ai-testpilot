// S2.5 审批页展示标签：镜像后端枚举/字面量，纯前端映射，不含请求逻辑。
import type {
  OracleTypeStr,
  ReviewAction,
  SuggestionSourceKind,
  SuggestionView,
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

// 与 core testpilot_core.urn 的 test_case URN 命名空间对齐（R3 溯源补强）
const TEST_CASE_URN_PREFIX = "urn:testpilot:fs-cases:test_case:";

/**
 * R3：从 source_refs 挑出来源用例 URN（test_case 命名空间），返回 # 后的
 * external_id（如 TC-A5）。确定性平移项经 core 强制追加来源用例 URN，
 * 审批页据此渲染「平移自」徽章，与用例自有断言（文档 URN）区分。
 */
export function transferCaseIds(sourceRefs: string[]): string[] {
  const out: string[] = [];
  for (const ref of sourceRefs) {
    if (!ref.startsWith(TEST_CASE_URN_PREFIX)) continue;
    const hash = ref.indexOf("#");
    const id = hash >= 0 ? ref.slice(hash + 1) : ref;
    if (id && !out.includes(id)) out.push(id);
  }
  return out;
}

// docs/01 §4.5：审批卡片按步骤锚点分组的纯函数（前端无单测，人工走查 + 门禁构建）。
export interface StepGroup {
  // null 恒为第一组「用例级（全程）」；其余按 step_id 数值升序，乱序输入确定收敛
  stepId: number | null;
  label: string;
  suggestions: SuggestionView[];
}

/**
 * 把草稿按 oracle.step_id 确定性分组：null 归「用例级（全程）」置顶，
 * 步骤组按序号升序（与卡片顶部「操作步骤」编号同坐标）；组内保持输入顺序——
 * core build_review_queue 已按「步骤锚点 → 文档子弹序 → URN 终裁」排好，
 * 前端不再二次排序，老文档全 null 锚点时组内即文档预期段顺序。
 * 老后端缺 step_id 字段时 ?? null 兜底，等同升级前的扁平列表。
 */
export function groupSuggestionsByStep(suggestions: SuggestionView[]): StepGroup[] {
  const caseLevel: SuggestionView[] = [];
  const byStep = new Map<number, SuggestionView[]>();
  for (const suggestion of suggestions) {
    const stepId = suggestion.oracle.step_id ?? null;
    if (stepId === null) {
      caseLevel.push(suggestion);
      continue;
    }
    const bucket = byStep.get(stepId);
    if (bucket) {
      bucket.push(suggestion);
    } else {
      byStep.set(stepId, [suggestion]);
    }
  }
  const groups: StepGroup[] = [];
  if (caseLevel.length > 0) {
    groups.push({ stepId: null, label: "用例级（全程）", suggestions: caseLevel });
  }
  for (const stepId of [...byStep.keys()].sort((a, b) => a - b)) {
    groups.push({
      stepId,
      label: `步骤 ${stepId}`,
      suggestions: byStep.get(stepId) ?? [],
    });
  }
  return groups;
}
