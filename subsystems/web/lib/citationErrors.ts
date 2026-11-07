// citation_errors 持久化的是后端 validate_citations 产出的 "前缀: 详情" 整串
// （前缀事实源：packages/core/.../answering.py validate_citations）。
// 前端按前缀映射人话说明；未知前缀不吞，原样展示整串。

export const CITATION_ERROR_LABELS: Record<string, string> = {
  // P2（qa-prompt-2）起：模型只输出 anchor，服务端确定性渲染 quote
  empty_anchor: "引用的 anchor 为空（模型未给出证据定位文本）",
  anchor_not_found: "anchor 在证据块中逐字找不到（模型改写或编造了定位文本，已 fail-closed 拒绝）",
  // 渲染器不变量兜底前缀，正常链路不应出现（出现即渲染器接线 bug）
  quote_not_verbatim: "引用原文与证据块不逐字一致（模型改写/省略了代码，如插值、省略号）",
  urn_not_in_evidence: "引用 URN 不在本次检索证据白名单内",
  no_citations: "草稿未给出任何引用",
  // 历史前缀：qa-prompt-1 时代的 v6 run 可能落库，保留以正确展示旧记录
  empty_quote: "引用的 quote 为空（旧版提示词链路的历史记录）",
};

export function citationErrorPrefix(raw: string): string {
  const idx = raw.indexOf(":");
  return idx === -1 ? raw : raw.slice(0, idx);
}

export function citationErrorLabel(raw: string): string {
  const prefix = citationErrorPrefix(raw);
  return CITATION_ERROR_LABELS[prefix] ?? raw;
}
