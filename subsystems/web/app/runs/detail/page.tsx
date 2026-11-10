"use client";

// S3.5d 执行 run 详情页（FR-EX-002/007）：账本投影 + 结构化 TestRun——
// 用例三态、失败消息/堆栈、制品 URN、归因五候选（置信度/理由/证据 ref/规则版本）、
// 显式未覆盖层。queued/running 行 run=null：只渲染账本投影，不假设字段存在。
//
// 路由形态用 /runs/detail?runId=... 而非动态段：站点是 output:"export"
// 纯静态导出，构建期没有 run 清单可喂 generateStaticParams；查询参数在浏览器
// 运行期读取，配合 useSearchParams 的 Suspense 边界即可静态预渲染外壳。
import Link from "next/link";
import { Suspense, useCallback, useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import { cancelExecutionRun, fetchExecutionRun } from "@/lib/api";
import {
  caseStatusClass,
  caseStatusLabel,
  runStatusClass,
  runStatusLabel,
  verdictClass,
  verdictLabel,
} from "@/lib/executionLabels";
import type {
  AttributionVerdictView,
  ExecutionRunDetail,
} from "@/lib/types";

function DetailBody({ runId }: { runId: string }) {
  const [detail, setDetail] = useState<ExecutionRunDetail | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    try {
      setDetail(await fetchExecutionRun(runId));
      setError("");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [runId]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const handleCancel = async () => {
    setBusy(true);
    setNotice("");
    try {
      const result = await cancelExecutionRun(runId);
      setNotice(`取消标记已落库：cancel_requested=${result.cancel_requested}`);
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  if (error) {
    return (
      <>
        <nav className="top-nav">
          <Link href="/runs">← 返回执行账本</Link>
        </nav>
        <div className="error-banner">请求失败：{error}</div>
      </>
    );
  }
  if (!detail) {
    return <p className="null-note">加载中…</p>;
  }

  const run = detail.run;
  const verdictByCase = new Map<string, AttributionVerdictView>(
    (run?.attributions ?? []).map((v) => [v.case_id, v]),
  );
  const cancellable =
    detail.status === "queued" || detail.status === "running";

  return (
    <>
      <nav className="top-nav">
        <Link href="/runs">← 返回执行账本</Link>
      </nav>
      <h1>执行 run 详情</h1>
      <p className="subtitle hash-line">run_id: {detail.run_id}</p>

      {notice && <div className="ap-notice">{notice}</div>}

      <section className="panel">
        <h2 className="ex-section-title">账本投影</h2>
        <table className="kv-table">
          <tbody>
            <tr>
              <th>状态</th>
              <td>
                <span className={runStatusClass(detail.status)}>
                  {runStatusLabel(detail.status)}
                </span>
                {cancellable && (
                  <button
                    type="button"
                    className="link-btn ex-cancel-btn"
                    disabled={busy}
                    onClick={handleCancel}
                  >
                    {busy ? "提交中…" : "请求取消"}
                  </button>
                )}
              </td>
            </tr>
            <tr>
              <th>项目</th>
              <td>{detail.project}</td>
            </tr>
            <tr>
              <th>commit</th>
              <td className="hash-line">{detail.commit_sha}</td>
            </tr>
            <tr>
              <th>幂等键</th>
              <td className="hash-line">{detail.idempotency_key}</td>
            </tr>
            <tr>
              <th>创建/更新</th>
              <td className="hash-line">
                {detail.created_at} / {detail.updated_at}
              </td>
            </tr>
            {detail.error && (
              <tr>
                <th>错误</th>
                <td className="ap-error">{detail.error}</td>
              </tr>
            )}
          </tbody>
        </table>
      </section>

      {run === null ? (
        <p className="panel null-note">
          run 仍处于 {runStatusLabel(detail.status)}
          ：raw_json 尚未回写，结构化 TestRun（用例/制品/归因）要等 worker
          收口后才可见；账本投影已经持久化，崩溃也能凭幂等键重放。
        </p>
      ) : (
        <>
          <section className="panel">
            <h2 className="ex-section-title">执行环境</h2>
            <table className="kv-table">
              <tbody>
                <tr>
                  <th>provider / 环境</th>
                  <td>
                    {run.environment.provider} · {run.environment.env_id}
                    {run.environment.version
                      ? ` · ${run.environment.version}`
                      : ""}
                  </td>
                </tr>
                <tr>
                  <th>能力</th>
                  <td>{run.environment.capabilities.join(", ") || "—"}</td>
                </tr>
                <tr>
                  <th>时间窗 / 耗时</th>
                  <td className="hash-line">
                    {run.started_at ?? "—"} → {run.finished_at ?? "—"}（
                    {run.duration_ms ?? "—"} ms）
                  </td>
                </tr>
                <tr>
                  <th>runner 版本</th>
                  <td className="hash-line">{run.runner_capability_version}</td>
                </tr>
                <tr>
                  <th>build</th>
                  <td className="hash-line">{run.build_id ?? "—"}</td>
                </tr>
              </tbody>
            </table>
            {run.uncovered_layers.length > 0 && (
              <div className="ex-uncovered">
                <strong>显式未覆盖层（只能生产/真机验证）：</strong>
                {run.uncovered_layers.map((layer) => (
                  <span key={layer} className="tag">
                    {layer}
                  </span>
                ))}
              </div>
            )}
          </section>

          <section className="panel">
            <h2 className="ex-section-title">
              用例结果（{run.case_results.length}）
            </h2>
            <table className="ex-case-table">
              <thead>
                <tr>
                  <th>用例</th>
                  <th>状态</th>
                  <th>耗时</th>
                  <th>失败消息 / 堆栈顶</th>
                  <th>归因</th>
                </tr>
              </thead>
              <tbody>
                {run.case_results.map((c) => {
                  const verdict = verdictByCase.get(c.external_case_id);
                  return (
                    <tr key={c.external_case_id}>
                      <td className="hash-line">{c.external_case_id}</td>
                      <td>
                        <span className={caseStatusClass(c.status)}>
                          {caseStatusLabel(c.status)}
                        </span>
                      </td>
                      <td>{c.duration_ms ?? "—"} ms</td>
                      <td>
                        {c.message || "—"}
                        {c.stack_top && (
                          <div className="ex-subline hash-line">
                            @ {c.stack_top}
                          </div>
                        )}
                        {c.artifact_urns.length > 0 && (
                          <details className="ap-details">
                            <summary>制品 URN（{c.artifact_urns.length}）</summary>
                            {c.artifact_urns.map((urn) => (
                              <div key={urn} className="hash-line ex-urn">
                                {urn}
                              </div>
                            ))}
                          </details>
                        )}
                      </td>
                      <td>
                        {verdict ? (
                          <span className={verdictClass(verdict.category)}>
                            {verdictLabel(verdict.category)}
                            {verdict.confidence > 0
                              ? ` ${verdict.confidence}`
                              : ""}
                          </span>
                        ) : (
                          <span className="null-note">—</span>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </section>

          {run.artifacts.length > 0 && (
            <section className="panel">
              <h2 className="ex-section-title">
                run 级制品（{run.artifacts.length}）
              </h2>
              <table className="ex-case-table">
                <thead>
                  <tr>
                    <th>类型</th>
                    <th>URI</th>
                    <th>内容哈希</th>
                    <th>保留期</th>
                  </tr>
                </thead>
                <tbody>
                  {run.artifacts.map((a) => (
                    <tr key={a.uri}>
                      <td>{a.type}</td>
                      <td className="hash-line ex-urn">{a.uri}</td>
                      <td className="hash-line">{a.content_hash ?? "—"}</td>
                      <td>{a.retention_days} 天</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {run.raw_report_ref && (
                <p className="ex-subline hash-line">
                  原始报告：{run.raw_report_ref}
                </p>
              )}
            </section>
          )}

          <section>
            <h2 className="ex-section-title">
              失败归因结论（{run.attributions.length}）
            </h2>
            {run.attributions.length === 0 ? (
              <p className="panel null-note">
                本 run 无失败/错误用例，归因流程不触发（全绿 run 结论列表为空）。
              </p>
            ) : (
              run.attributions.map((v) => (
                <article key={v.case_id} className="panel ex-verdict-card">
                  <header className="ex-verdict-head">
                    <span className={verdictClass(v.category)}>
                      {verdictLabel(v.category)}
                    </span>
                    <strong className="hash-line">{v.case_id}</strong>
                    <span className="ex-subline">
                      置信度 {v.confidence} · 规则 {v.rule_version}
                    </span>
                  </header>
                  {v.needs_human_reason && (
                    <p className="ex-needs-human-reason">
                      人审原因：{v.needs_human_reason}
                    </p>
                  )}
                  {v.candidates.length > 0 && (
                    <div className="ex-candidates">
                      <div className="ex-subline">候选（按置信度排序）：</div>
                      <ul>
                        {v.candidates.map((cand, i) => (
                          <li key={`${cand.category}-${i}`}>
                            <span className={verdictClass(cand.category)}>
                              {verdictLabel(cand.category)}
                            </span>{" "}
                            {cand.confidence}
                            <ul>
                              {cand.reasons.map((reason) => (
                                <li key={reason}>{reason}</li>
                              ))}
                            </ul>
                            <div className="ex-refs">
                              证据：
                              {cand.evidence_refs.map((ref) => (
                                <span key={ref} className="ex-ref">
                                  {ref}
                                </span>
                              ))}
                            </div>
                          </li>
                        ))}
                      </ul>
                    </div>
                  )}
                  <div className="ex-refs">
                    裁决证据账本：
                    {v.evidence_refs.length === 0 && (
                      <span className="null-note">无（薄证据人审）</span>
                    )}
                    {v.evidence_refs.map((ref) => (
                      <span key={ref} className="ex-ref">
                        {ref}
                      </span>
                    ))}
                  </div>
                  {v.uncovered_layers.length > 0 && (
                    <div className="ex-uncovered">
                      该结论的未覆盖层：
                      {v.uncovered_layers.map((layer) => (
                        <span key={layer} className="tag">
                          {layer}
                        </span>
                      ))}
                    </div>
                  )}
                </article>
              ))
            )}
          </section>
        </>
      )}
    </>
  );
}

function DetailPage() {
  const params = useSearchParams();
  const runId = params.get("runId") ?? "";
  return (
    <>
      {runId ? (
        <DetailBody runId={runId} />
      ) : (
        <>
          <nav className="top-nav">
            <Link href="/runs">← 返回执行账本</Link>
          </nav>
          <div className="error-banner">
            缺少 runId 查询参数，请从执行账本列表进入详情。
          </div>
        </>
      )}
    </>
  );
}

export default function ExecutionRunDetailPage() {
  // useSearchParams 在静态导出下要求 Suspense 边界（构建期静态预渲染外壳）
  return (
    <main className="container">
      <Suspense fallback={<p className="null-note">加载中…</p>}>
        <DetailPage />
      </Suspense>
    </main>
  );
}
