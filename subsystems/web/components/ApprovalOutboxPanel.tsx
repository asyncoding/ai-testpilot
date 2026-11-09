"use client";

// FR-WR-001 AC1 外部副作用账本：每条 R2 记录四要素齐——
// 目标系统、请求体 diff（含渲染 Markdown）、幂等键、回滚说明。
import { OUTBOX_STATUS_LABELS, shortUrn } from "@/lib/approvalLabels";
import type { DispatchReport, OutboxRecordView } from "@/lib/types";

interface Props {
  records: OutboxRecordView[];
  report: DispatchReport | null;
  dispatching: boolean;
  onDispatch: () => void;
}

export default function ApprovalOutboxPanel({
  records,
  report,
  dispatching,
  onDispatch,
}: Props) {
  return (
    <div className="panel">
      <div className="ap-head">
        <strong>外部副作用账本 outbox（{records.length}）</strong>
        <button type="button" disabled={dispatching} onClick={onDispatch}>
          {dispatching ? "派发中…" : "派发一轮 pending/failed"}
        </button>
      </div>
      <p className="null-note">
        四要素齐备（FR-WR-001 AC1）：目标系统 · 请求体 diff · 幂等键 · 回滚说明；
        kill switch 停用时派发只计数 blocked，不产生写入、不消耗重试。
      </p>
      {report && (
        <p className="status-line">
          最近一轮派发：status={report.status}，scanned={report.scanned}，
          confirmed={report.confirmed}，blocked={report.blocked}，
          failed={report.failed}，skipped_unsupported={report.skipped_unsupported}
        </p>
      )}
      {records.length === 0 ? (
        <p className="null-note">暂无 outbox 记录（批准含采纳/修改的评审后出现）</p>
      ) : (
        <table className="ap-outbox-table">
          <thead>
            <tr>
              <th>状态</th>
              <th>目标系统</th>
              <th>幂等键</th>
              <th>attempts</th>
              <th>external_ref / 详情</th>
            </tr>
          </thead>
          <tbody>
            {records.map((r) => (
              <tr key={r.id ?? r.idempotency_key}>
                <td>
                  <span className={`st st-${r.status}`}>
                    {OUTBOX_STATUS_LABELS[r.status] ?? r.status}
                  </span>
                </td>
                <td>
                  <code>{r.target_system}</code>
                </td>
                <td className="hash-line" title={r.idempotency_key}>
                  {shortUrn(r.idempotency_key, 44)}
                </td>
                <td>{r.attempts}</td>
                <td>
                  {r.external_ref ? (
                    <code title={r.external_ref}>{shortUrn(r.external_ref, 48)}</code>
                  ) : (
                    "—"
                  )}
                  <details className="ap-details">
                    <summary>请求体 diff / 回滚说明 / 错误</summary>
                    <dl className="kv-table">
                      <dt>请求体（request_body）</dt>
                      <dd>
                        <pre className="ap-pre">
                          {JSON.stringify(r.request_body, null, 2)}
                        </pre>
                      </dd>
                      <dt>回滚说明（rollback_note）</dt>
                      <dd>{r.rollback_note || "—"}</dd>
                      <dt>approval_id</dt>
                      <dd className="hash-line">{r.approval_id}</dd>
                      {r.error && (
                        <>
                          <dt>错误</dt>
                          <dd className="ap-error">{r.error}</dd>
                        </>
                      )}
                    </dl>
                  </details>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
