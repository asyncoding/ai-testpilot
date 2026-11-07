import ReactMarkdown from "react-markdown";
import type { AskResponse } from "@/lib/types";
import { citationErrorLabel, citationErrorPrefix } from "@/lib/citationErrors";
import { intentSourceLabel } from "@/lib/intentSource";
import CitationList from "./CitationList";
import HistorySection from "./HistorySection";

// unknown_reason 原始值 → 中文说明（未知值兜底显示原文，不吞信息）
const REASON_LABEL: Record<string, string> = {
  out_of_scope: "超出知识范围：闲聊/越界问题在 ACL 节点收口，零模型生成、零检索",
  model_unavailable: "模型不可用：录制夹具缺少对应响应，诚实降级，不编造答案",
  empty_retrieval: "未检索到任何证据：显式未知，不放无引用答案出门",
  weak_retrieval: "证据强度不足：检索命中但未过充分性门槛",
  // 具体失败类型（quote_not_verbatim / urn_not_in_evidence / …）看 citation_errors，
  // 不再在此写死"不在白名单内"——实测主因是模型逐字摘抄退化，不是白名单问题
  citation_failed_exhausted: "引用校验两次未达标，按无引用结论率=0 收口为未知",
  recursion_limit_exceeded: "超步预算耗尽：图执行超过 max_agent_steps，落 error 留痕",
};

export default function AnswerCard({ response }: { response: AskResponse }) {
  const unknown = response.status !== "answered";
  return (
    <div className={`panel ${unknown ? "answer-unknown" : "answer-answered"}`}>
      <div className="status-line">
        {unknown ? (
          <span className="unknown-reason">
            未知（{response.status}）
            {response.unknown_reason ? ` · ${response.unknown_reason}` : ""}
          </span>
        ) : (
          <span style={{ color: "var(--green)" }}>已回答（answered）</span>
        )}
        {response.intent && <span> · 意图：{response.intent}</span>}
        {response.intent_source && (
          <span> · 意图来源：{intentSourceLabel(response.intent_source)}</span>
        )}
        <span> · run_id: {response.run_id}</span>
      </div>

      {unknown && (
        <p>
          {REASON_LABEL[response.unknown_reason] ??
            `降级原因：${response.unknown_reason || "（未提供）"}`}
        </p>
      )}

      {/* 引用校验的具体错误按前缀映射成人话；整串保留（含 URN），排查时不用回库 */}
      {unknown && response.citation_errors.length > 0 && (
        <ul className="citation-errors">
          {response.citation_errors.map((raw, i) => (
            <li key={`${raw}-${i}`}>
              <span className="tag">{citationErrorPrefix(raw)}</span>{" "}
              {citationErrorLabel(raw)}
              <span className="hash-line citation-error-raw">{raw}</span>
            </li>
          ))}
        </ul>
      )}

      {response.answer && (
        <>
          <div className="markdown">
            <ReactMarkdown>{response.answer.markdown}</ReactMarkdown>
          </div>
          <HistorySection history={response.answer.history} />
          <CitationList runId={response.run_id} citations={response.answer.citations} />
        </>
      )}

      {response.degraded.length > 0 && (
        <div className="degraded-tags">
          {response.degraded.map((flag) => (
            <span className="tag" key={flag}>
              degraded: {flag}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}
