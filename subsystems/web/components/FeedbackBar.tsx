"use client";

import { useState } from "react";
import { submitFeedback } from "@/lib/api";
import type { FeedbackKind, FeedbackReason } from "@/lib/types";

// FR-QA-004：👍 直提；👎 必须选四原因之一，可选 detail。
const REASONS: { value: FeedbackReason; label: string }[] = [
  { value: "not_retrieved", label: "没检索到相关内容" },
  { value: "stale_reference", label: "引用过时/失效" },
  { value: "off_topic", label: "答非所问" },
  { value: "factual_error", label: "事实错误" },
];

export default function FeedbackBar({ runId }: { runId: string }) {
  const [kind, setKind] = useState<FeedbackKind | null>(null);
  const [reason, setReason] = useState<FeedbackReason | null>(null);
  const [detail, setDetail] = useState("");
  const [submittedId, setSubmittedId] = useState<number | null>(null);
  const [error, setError] = useState("");
  const [sending, setSending] = useState(false);

  const send = async (selected: FeedbackKind, withReason?: FeedbackReason | null) => {
    setSending(true);
    setError("");
    try {
      const resp = await submitFeedback(runId, selected, withReason ?? null, detail);
      setSubmittedId(resp.id);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSending(false);
    }
  };

  if (submittedId !== null) {
    return (
      <div className="feedback-bar">
        <span className="feedback-ok">反馈已落库（feedback.id={submittedId}），谢谢！</span>
      </div>
    );
  }

  return (
    <div className="feedback-bar">
      <span>这条回答有帮助吗？</span>
      <button
        type="button"
        className={kind === "useful" ? "active" : ""}
        disabled={sending}
        onClick={() => send("useful")}
      >
        👍 有用
      </button>
      <button
        type="button"
        className={kind === "not_useful" ? "active" : ""}
        disabled={sending}
        onClick={() => setKind("not_useful")}
      >
        👎 没用
      </button>

      {kind === "not_useful" && (
        <>
          <div className="feedback-reasons">
            {REASONS.map((r) => (
              <label key={r.value}>
                <input
                  type="radio"
                  name="feedback-reason"
                  checked={reason === r.value}
                  onChange={() => setReason(r.value)}
                />
                {r.label}（{r.value}）
              </label>
            ))}
          </div>
          <div className="feedback-detail">
            <input
              placeholder="补充说明（可选）"
              value={detail}
              onChange={(e) => setDetail(e.target.value)}
            />
            <button
              type="button"
              className="primary"
              disabled={sending || reason === null}
              onClick={() => send("not_useful", reason)}
            >
              提交反馈
            </button>
          </div>
        </>
      )}
      {error && <span className="feedback-err">反馈失败：{error}</span>}
    </div>
  );
}
