"use client";

// FR-UI-002 单条用例待审卡片：草稿类型/期望/来源 URN/置信度/出处全展示，
// 每条草稿可采纳/行内修改/拒绝；动作提交后即时落库，队列刷新后卡片消失。
import { useState } from "react";
import { decideCase } from "@/lib/api";
import {
  ORACLE_TYPE_LABELS,
  ORACLE_TYPE_OPTIONS,
  REVIEW_ACTION_LABELS,
  SOURCE_KIND_LABELS,
  shortUrn,
  transferCaseIds,
} from "@/lib/approvalLabels";
import type {
  CaseDecisionPlan,
  OracleTypeStr,
  ReviewAction,
  ReviewQueueItem,
  SuggestionDecisionHttp,
  SuggestionView,
} from "@/lib/types";

interface Props {
  item: ReviewQueueItem;
  reviewer: string;
  onSubmitted: (plan: CaseDecisionPlan) => void;
}

const ACTIONS: ReviewAction[] = ["adopt", "modify", "reject"];

export default function ApprovalQueueCard({ item, reviewer, onSubmitted }: Props) {
  const [actions, setActions] = useState<Record<string, ReviewAction>>(() =>
    Object.fromEntries(item.suggestions.map((s) => [s.urn, "adopt"])),
  );
  const [types, setTypes] = useState<Record<string, OracleTypeStr>>(() =>
    Object.fromEntries(
      item.suggestions.map((s) => [s.urn, s.oracle.type]),
    ),
  );
  const [expectations, setExpectations] = useState<Record<string, string>>(() =>
    Object.fromEntries(
      item.suggestions.map((s) => [s.urn, s.oracle.expectation]),
    ),
  );
  const [reason, setReason] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");

  const setAction = (urn: string, action: ReviewAction) =>
    setActions((prev) => ({ ...prev, [urn]: action }));

  const submit = async () => {
    setError("");
    if (!reviewer.trim()) {
      setError("请先在页面顶部填写评审人");
      return;
    }
    const items: SuggestionDecisionHttp[] = item.suggestions.map((s) => {
      const action = actions[s.urn] ?? "adopt";
      if (action === "modify") {
        return {
          suggestion_urn: s.urn,
          action,
          oracle_type: types[s.urn] ?? s.oracle.type,
          expectation: expectations[s.urn] ?? s.oracle.expectation,
        };
      }
      return { suggestion_urn: s.urn, action };
    });
    // 与 core 校验同构：modify 期望非空；整案拒绝只能全 reject
    const modifyBlank = item.suggestions.find(
      (s) =>
        actions[s.urn] === "modify" && !(expectations[s.urn] ?? "").trim(),
    );
    if (modifyBlank) {
      setError(`修改动作必须填写非空期望：${shortUrn(modifyBlank.urn, 32)}`);
      return;
    }
    const allReject = items.every((i) => i.action === "reject");
    const anyReject = items.some((i) => i.action === "reject");
    if (anyReject && !allReject) {
      setError("拒绝与采纳/修改不能混用：整案拒绝请将所有草稿置为拒绝，否则请勿拒绝单条");
      return;
    }
    setSubmitting(true);
    try {
      const plan = await decideCase(item.case_urn, {
        decision: allReject ? "rejected" : "approved",
        reviewer: reviewer.trim(),
        reason,
        items,
      });
      onSubmitted(plan);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="panel ap-card">
      <div className="ap-head">
        <div>
          <strong>
            {item.external_id} {item.title}
          </strong>
          <span className="hash-line ap-urn" title={item.case_urn}>
            {shortUrn(item.case_urn)}
          </span>
        </div>
        <div className="ap-badges">
          {item.priority && <span className="tag">{item.priority}</span>}
          {item.module && <span className="tag">{item.module}</span>}
          {item.oracle_missing && <span className="tag tag-amber">缺预期草稿</span>}
          {item.exploratory && <span className="tag tag-amber">含探索性标记</span>}
        </div>
      </div>

      {item.suggestions.map((s) => (
        <SuggestionRow
          key={s.urn}
          suggestion={s}
          action={actions[s.urn] ?? "adopt"}
          editedType={types[s.urn] ?? s.oracle.type}
          editedExpectation={expectations[s.urn] ?? s.oracle.expectation}
          onAction={(a) => setAction(s.urn, a)}
          onType={(t) => setTypes((p) => ({ ...p, [s.urn]: t }))}
          onExpectation={(v) => setExpectations((p) => ({ ...p, [s.urn]: v }))}
        />
      ))}

      <div className="ap-footer">
        <input
          className="ap-reason"
          placeholder="评审理由（可选，将写入 Approval.reason 与每条草稿 decision_reason）"
          value={reason}
          onChange={(e) => setReason(e.target.value)}
        />
        <button
          type="button"
          className="primary"
          disabled={submitting}
          onClick={submit}
        >
          {submitting ? "提交中…" : "提交评审（单事务落库）"}
        </button>
      </div>
      {error && <div className="error-banner">评审失败：{error}</div>}
    </div>
  );
}

function SuggestionRow({
  suggestion,
  action,
  editedType,
  editedExpectation,
  onAction,
  onType,
  onExpectation,
}: {
  suggestion: SuggestionView;
  action: ReviewAction;
  editedType: OracleTypeStr;
  editedExpectation: string;
  onAction: (a: ReviewAction) => void;
  onType: (t: OracleTypeStr) => void;
  onExpectation: (v: string) => void;
}) {
  const o = suggestion.oracle;
  const confirmedConfidence = Math.min(1, o.confidence + 0.3);
  // R3：test_case 命名空间的 source_ref 是平移溯源标记，审批页显式标出来源用例
  const transferredFrom = transferCaseIds(o.source_refs);
  return (
    <div className="ap-suggestion">
      <div className="ap-sg-head">
        <span className="hash-line" title={suggestion.urn}>
          {shortUrn(suggestion.urn, 40)}
        </span>
        <span className="tag">{SOURCE_KIND_LABELS[o.source_kind]}</span>
        {transferredFrom.length > 0 && (
          <span
            className="tag tag-amber"
            title={`相似用例断言平移（R2 仅对缺预期用例开放），来源：${transferredFrom.join(", ")}`}
          >
            平移自 {transferredFrom.join("、")}
          </span>
        )}
        <span className="tag">{o.provenance === "model" ? "模型产出" : "确定性产出"}</span>
        {suggestion.exploratory && <span className="tag tag-amber">探索性</span>}
      </div>

      <div className="ap-oracle">
        <label className="ap-field">
          <span>断言类型</span>
          <select
            value={editedType}
            disabled={action !== "modify"}
            onChange={(e) => onType(e.target.value as OracleTypeStr)}
          >
            {ORACLE_TYPE_OPTIONS.map((t) => (
              <option key={t} value={t}>
                {ORACLE_TYPE_LABELS[t]}
              </option>
            ))}
          </select>
        </label>
        <label className="ap-field ap-field-grow">
          <span>期望（修改时可编辑；来源 URN 不可改，追溯链不断）</span>
          <textarea
            rows={2}
            value={editedExpectation}
            readOnly={action !== "modify"}
            onChange={(e) => onExpectation(e.target.value)}
          />
        </label>
      </div>

      <div className="ap-meta">
        <span>
          置信度 {o.confidence.toFixed(2)}
          {action !== "reject" && (
            <em> → 人审确认后 {confirmedConfidence.toFixed(2)}（+0.3 封顶 1.0）</em>
          )}
        </span>
        <span className="ap-refs">
          来源 URN：
          {o.source_refs.length === 0 ? (
            <em>无</em>
          ) : (
            o.source_refs.map((ref) => (
              <code key={ref} title={ref}>
                {shortUrn(ref, 56)}
              </code>
            ))
          )}
        </span>
      </div>

      <div className="ap-actions" role="group" aria-label="草稿动作">
        {ACTIONS.map((a) => (
          <button
            key={a}
            type="button"
            className={action === a ? "active" : ""}
            onClick={() => onAction(a)}
          >
            {REVIEW_ACTION_LABELS[a]}
          </button>
        ))}
      </div>
    </div>
  );
}
