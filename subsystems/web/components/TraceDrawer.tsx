"use client";

import { useEffect, useState } from "react";
import { fetchTrace } from "@/lib/api";
import type { TraceData } from "@/lib/types";
import { citationErrorLabel, citationErrorPrefix } from "@/lib/citationErrors";
import { intentSourceLabel } from "@/lib/intentSource";

// FR-EV-004 AC1：UI → trace 反查一次问答的完整本地链路。
// Langfuse 深链仅在 tracer=langfuse 时存在；noop 如实提示，不造链接。
export default function TraceDrawer({
  runId,
  open,
  onClose,
}: {
  runId: string;
  open: boolean;
  onClose: () => void;
}) {
  const [trace, setTrace] = useState<TraceData | null>(null);
  const [error, setError] = useState("");
  // 静态前端可能比后端新（版本错配）：v6 前的老 trace 没有 citation_errors，
  // 直接 .length 会抛 undefined.length 把整个抽屉打白，缺失按空数组渲染
  const citationErrors = trace?.citation_errors ?? [];

  useEffect(() => {
    if (!open) return;
    setTrace(null);
    setError("");
    fetchTrace(runId)
      .then(setTrace)
      .catch((e) => setError(e instanceof Error ? e.message : String(e)));
  }, [open, runId]);

  if (!open) return null;

  return (
    <>
      <div className="drawer-mask" onClick={onClose} />
      <div className="drawer" role="dialog" aria-label="trace 时间线">
        <button type="button" className="drawer-close" onClick={onClose}>
          ✕
        </button>
        <h3>Trace 时间线</h3>
        {error && <p className="feedback-err">trace 获取失败：{error}</p>}
        {!trace && !error && <p>加载中…</p>}
        {trace && (
          <>
            <table className="kv-table">
              <tbody>
                <tr><th>run_id</th><td>{trace.run_id}</td></tr>
                <tr><th>问题</th><td>{trace.question}</td></tr>
                <tr>
                  <th>状态 / 意图</th>
                  <td>
                    {trace.status} / {trace.intent || "—"}
                    {trace.intent_source && (
                      <>
                        {" "}
                        <span className="tag" title={intentSourceLabel(trace.intent_source)}>
                          {trace.intent_source}
                        </span>
                      </>
                    )}
                  </td>
                </tr>
                {trace.unknown_reason && (
                  <tr><th>未知原因</th><td>{trace.unknown_reason}</td></tr>
                )}
                {citationErrors.length > 0 && (
                  <tr>
                    <th>引用校验错误</th>
                    <td>
                      <ul className="citation-errors">
                        {citationErrors.map((raw, i) => (
                          <li key={`${raw}-${i}`}>
                            <span className="tag">{citationErrorPrefix(raw)}</span>{" "}
                            {citationErrorLabel(raw)}
                            <span className="hash-line citation-error-raw">{raw}</span>
                          </li>
                        ))}
                      </ul>
                    </td>
                  </tr>
                )}
                <tr><th>尝试 / 重生成</th><td>{trace.attempts} / {trace.regens}</td></tr>
                <tr><th>token</th><td>in={trace.tokens.in}，out={trace.tokens.out}</td></tr>
                <tr><th>耗时</th><td>{trace.latency_ms} ms</td></tr>
                <tr>
                  <th>retrieval_event_id</th>
                  <td>
                    {trace.retrieval_event_id === null ? (
                      <span className="null-note">null（FTS 通道不写 retrieval_events，如实呈现）</span>
                    ) : (
                      trace.retrieval_event_id
                    )}
                  </td>
                </tr>
                {trace.symbol_pinned && (
                  <tr>
                    <th>符号置顶</th>
                    <td>
                      <span className="tag">symbol_pinned</span>{" "}
                      精确符号 <code>{trace.symbol_pin_name}</code> 经 entities 表唯一命中，
                      确定性置顶（不参与向量语义抽奖）
                    </td>
                  </tr>
                )}
                <tr>
                  <th>版本四元组</th>
                  <td>
                    model={trace.versions.model || "—"}<br />
                    prompt={trace.versions.prompt || "—"}<br />
                    params={trace.versions.params || "—"}<br />
                    data={trace.versions.data || (
                      <span className="null-note">空（非 code 意图不跑 live-verify）</span>
                    )}
                  </td>
                </tr>
                <tr><th>thread_id</th><td className="hash-line">{trace.thread_id}</td></tr>
                <tr><th>created_at</th><td>{trace.created_at}</td></tr>
              </tbody>
            </table>

            <h4>降级标记</h4>
            {trace.degraded.length === 0 ? (
              <p className="null-note">无</p>
            ) : (
              trace.degraded.map((f) => <span className="tag" key={f}>{f}</span>)
            )}

            <h4>证据（{trace.evidence.length}）</h4>
            <table className="kv-table">
              <thead>
                {/* RRF 分值是 0–0.033 的排名刻度，精确符号也只有 0.012，
                    展示它只会误导"精确匹配分很低"；排名+命中通道才解释排序 */}
                <tr><th>URN</th><th>kind</th><th>排名</th><th>命中通道</th><th>权威</th></tr>
              </thead>
              <tbody>
                {trace.evidence.map((e, idx) => {
                  // 老后端证据项只有 score：rank 回退展示序号、channels 缺失显「—」
                  const channels = e.channels ?? [];
                  return (
                  <tr key={e.urn}>
                    <td className="hash-line">{e.urn}</td>
                    <td>{e.kind}</td>
                    <td>
                      #{e.rank ?? idx + 1}
                      {trace.symbol_pinned && e.urn === trace.symbol_pin_urn && (
                        <> <span className="tag">置顶</span></>
                      )}
                    </td>
                    <td>{channels.length ? channels.join("+") : "—"}</td>
                    <td>{e.authority_level || "—"}</td>
                  </tr>
                  );
                })}
              </tbody>
            </table>

            <h4>引用三态（{trace.citations.length}）</h4>
            {trace.citations.map((c) => (
              <div key={c.urn}>
                <span className={`badge badge-${c.version_state}`}>{c.version_state}</span>{" "}
                <span className="cite-urn">
                  {c.urn}
                  {c.version_ref ? ` @ ${c.version_ref}` : ""}
                </span>
              </div>
            ))}

            <h4>现场核验（verify_results）</h4>
            {trace.verify_results.length === 0 ? (
              <p className="null-note">本次 run 无 live-verify 记录</p>
            ) : (
              <pre className="source-text">{JSON.stringify(trace.verify_results, null, 2)}</pre>
            )}

            <h4>Langfuse</h4>
            {trace.langfuse ? (
              <a href={trace.langfuse.session_url} target="_blank" rel="noreferrer">
                在 Langfuse 打开 session（{trace.langfuse.host}）
              </a>
            ) : (
              <p className="null-note">
                当前 tracer=noop，无 Langfuse 深链；切 langfuse profile 后此处生成
                /traces?session=run_id 链接。
              </p>
            )}
          </>
        )}
      </div>
    </>
  );
}
