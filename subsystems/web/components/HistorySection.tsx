import type { HistoryTestimony } from "@/lib/types";

// FR-QA-003：历史证词是语料逐字原文（模型全程不接触、不改写），
// 以 blockquote + 出处行呈现，与模型生成的「当前结论」物理分节。
export default function HistorySection({
  history,
}: {
  history: HistoryTestimony[];
}) {
  if (!history.length) return null;
  return (
    <div className="history-section">
      <h4>历史证词（逐字引用，未经模型改写）</h4>
      {history.map((item, idx) => (
        <div className="history-item" key={`${item.doc_urn}-${idx}`}>
          <blockquote>{item.text}</blockquote>
          <div className="source-line">
            {item.doc_urn}
            {item.date ? ` · ${item.date}` : ""}
            {item.version ? ` · ${item.version}` : ""}
          </div>
        </div>
      ))}
    </div>
  );
}
