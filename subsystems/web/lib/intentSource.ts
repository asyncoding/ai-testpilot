// qa_runs.intent_source 的前端人话映射（v7 起持久化）。
// 事实源：packages/orchestrator/.../qa_graph/nodes.py 的 INTENT_SOURCE_* 常量。
// 未知值不吞，原样展示；空串是 v7 之前老 run 的 legacy 形态。

export const INTENT_SOURCE_LABELS: Record<string, string> = {
  regex_bypass: "符号形态免模型（省一次 LLM 调用）",
  model: "模型分类",
  fallback: "异常兜底（关键词保守分类）",
};

export function intentSourceLabel(raw: string): string {
  if (!raw) return "—（v7 之前的历史 run）";
  return INTENT_SOURCE_LABELS[raw] ?? raw;
}
