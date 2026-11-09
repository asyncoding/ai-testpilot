"use client";

// FR-UI-002 用例审批页：待审队列（草稿 diff/来源/采纳改拒）+ 审批动作账本 +
// outbox 四要素面板 + 一键停用外写。所有动作即时落库，未审批草稿永不进正式用例库
// （后端 AC1 机器断言，前端只呈现 pending 草稿，已评审项不出现在队列）。
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import ApprovalOutboxPanel from "@/components/ApprovalOutboxPanel";
import ApprovalQueueCard from "@/components/ApprovalQueueCard";
import WritePolicyBanner from "@/components/WritePolicyBanner";
import {
  dispatchOutbox,
  fetchReviewQueue,
  getWritePolicy,
  listApprovalRecords,
  listOutboxRecords,
  setWritePolicy,
} from "@/lib/api";
import { shortUrn } from "@/lib/approvalLabels";
import type {
  ApprovalRecord,
  CaseDecisionPlan,
  DispatchReport,
  OutboxRecordView,
  ReviewQueueItem,
  WritePolicyView,
} from "@/lib/types";

export default function ApprovalsPage() {
  const [queue, setQueue] = useState<ReviewQueueItem[]>([]);
  const [records, setRecords] = useState<ApprovalRecord[]>([]);
  const [outbox, setOutbox] = useState<OutboxRecordView[]>([]);
  const [policy, setPolicy] = useState<WritePolicyView | null>(null);
  const [reviewer, setReviewer] = useState("");
  const [notice, setNotice] = useState("");
  const [report, setReport] = useState<DispatchReport | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    const [q, r, o, p] = await Promise.all([
      fetchReviewQueue(),
      listApprovalRecords(),
      listOutboxRecords(),
      getWritePolicy(),
    ]);
    setQueue(q.queue);
    setRecords(r.records);
    setOutbox(o.records);
    setPolicy(p);
  }, []);

  useEffect(() => {
    refresh().catch((e) => setError(e instanceof Error ? e.message : String(e)));
  }, [refresh]);

  const handleSubmitted = (plan: CaseDecisionPlan) => {
    setNotice(
      plan.outbox
        ? `评审已落库：${plan.approval.decision}，outbox ${plan.outbox.status}，幂等键 ${shortUrn(
            plan.idempotency_key,
            36,
          )}`
        : `整案拒绝已落库（approval_id=${shortUrn(plan.approval.approval_id, 36)}），无外部副作用`,
    );
    refresh().catch((e) => setError(String(e)));
  };

  const togglePolicy = async (runtimeDisabled: boolean) => {
    setBusy(true);
    setError("");
    try {
      setPolicy(await setWritePolicy(runtimeDisabled));
    } catch (e) {
      // profile 关着请求放开 → 后端 403 PolicyDeniedError，原样展示
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const handleDispatch = async () => {
    setBusy(true);
    setError("");
    try {
      const r = await dispatchOutbox();
      setReport(r);
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <main className="container">
      <nav className="top-nav">
        <Link href="/">← 返回问答页（FR-UI-001）</Link>
      </nav>
      <h1>用例草稿人审与回写</h1>
      <p className="subtitle">
        FR-UI-002 / FR-TD-004 / FR-WR-001：采纳 · 行内修改 · 拒绝；批准后经 outbox
        幂等回写独立 Markdown 写根，正式用例库审批流零触碰。
      </p>

      <WritePolicyBanner policy={policy} busy={busy} onToggle={togglePolicy} />
      {error && <div className="error-banner">请求失败：{error}</div>}
      {notice && <div className="ap-notice">{notice}</div>}

      <div className="panel">
        <label className="ap-reviewer">
          评审人（必填，POC 无认证；将持久化到 Approval.reviewer）：
          <input
            value={reviewer}
            placeholder="如 expert-zhang"
            onChange={(e) => setReviewer(e.target.value)}
          />
        </label>
      </div>

      <h2 className="ap-section-title">待审队列（{queue.length}）</h2>
      {queue.length === 0 ? (
        <p className="panel null-note">
          暂无 pending 草稿（运行 testpilot-mine-oracles 生成草稿后出现在这里；
          已评审项不再入队）。
        </p>
      ) : (
        queue.map((item) => (
          <ApprovalQueueCard
            key={item.case_urn}
            item={item}
            reviewer={reviewer}
            onSubmitted={handleSubmitted}
          />
        ))
      )}

      <h2 className="ap-section-title">审批动作账本（{records.length}）</h2>
      <div className="panel">
        {records.length === 0 ? (
          <p className="null-note">暂无审批记录（FR-WR-001 AC2：评审人/时间/结论全量持久化）</p>
        ) : (
          <table>
            <thead>
              <tr>
                <th>时间</th>
                <th>评审人</th>
                <th>结论</th>
                <th>级别</th>
                <th>摘要 / diff</th>
              </tr>
            </thead>
            <tbody>
              {records.map((a) => (
                <tr key={a.approval_id}>
                  <td className="hash-line">{a.decided_at ?? a.created_at}</td>
                  <td>{a.reviewer ?? "—"}</td>
                  <td>
                    <span className={`st st-${a.decision}`}>{a.decision}</span>
                  </td>
                  <td>{a.action_level}</td>
                  <td>
                    {a.request_summary}
                    <details className="ap-details">
                      <summary>请求 diff（request_diff）</summary>
                      <pre className="ap-pre">
                        {JSON.stringify(a.request_diff, null, 2)}
                      </pre>
                    </details>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <h2 className="ap-section-title">外部副作用与回写</h2>
      <ApprovalOutboxPanel
        records={outbox}
        report={report}
        dispatching={busy}
        onDispatch={handleDispatch}
      />
    </main>
  );
}
