"use client";

// S3.5d 测试执行账本列表页（FR-EX-002/007）：执行 run 盘点、状态/项目过滤、
// 归因计数投影（needs_human 高亮）、协作式取消。所有数据来自 GET /api/runs，
// 静态导出由 FastAPI 同端口托管；离线 profile 的证据来自录制夹具（见首页 Banner）。
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { cancelExecutionRun, listExecutionRuns } from "@/lib/api";
import {
  runStatusClass,
  runStatusLabel,
  shortId,
} from "@/lib/executionLabels";
import type { ExecutionRunListItem } from "@/lib/types";

const STATUS_FILTERS = [
  "",
  "queued",
  "running",
  "passed",
  "failed",
  "error",
  "timeout",
  "canceled",
];

export default function ExecutionRunsPage() {
  const [runs, setRuns] = useState<ExecutionRunListItem[]>([]);
  const [status, setStatus] = useState("");
  const [project, setProject] = useState("");
  const [applied, setApplied] = useState<{ status?: string; project?: string }>(
    {},
  );
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busyId, setBusyId] = useState("");

  const refresh = useCallback(async () => {
    try {
      const resp = await listExecutionRuns(applied);
      setRuns(resp.runs);
      setError("");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [applied]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const handleCancel = async (runId: string) => {
    setBusyId(runId);
    setNotice("");
    try {
      const result = await cancelExecutionRun(runId);
      setNotice(
        `取消标记已落库（run_id=${shortId(result.run_id, 20)}，` +
          `cancel_requested=${result.cancel_requested}）`,
      );
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusyId("");
    }
  };

  return (
    <main className="container">
      <nav className="top-nav">
        <Link href="/">← 返回问答页（FR-UI-001）</Link>
      </nav>
      <h1>测试执行账本</h1>
      <p className="subtitle">
        FR-EX-002 / FR-EX-007：先记账后派发的执行 run 盘点 · 失败归因计数 ·
        协作式取消；点 run_id 进详情看用例三态、证据账本与未覆盖层。
      </p>

      {error && <div className="error-banner">请求失败：{error}</div>}
      {notice && <div className="ap-notice">{notice}</div>}

      <form
        className="ex-filters"
        onSubmit={(e) => {
          e.preventDefault();
          setApplied({
            status: status || undefined,
            project: project.trim() || undefined,
          });
        }}
      >
        <label>
          状态
          <select value={status} onChange={(e) => setStatus(e.target.value)}>
            {STATUS_FILTERS.map((s) => (
              <option key={s} value={s}>
                {s ? runStatusLabel(s) : "全部"}
              </option>
            ))}
          </select>
        </label>
        <label>
          项目
          <input
            value={project}
            placeholder="如 AlarmKitSpike-demo"
            onChange={(e) => setProject(e.target.value)}
          />
        </label>
        <button type="submit">应用过滤</button>
      </form>

      <div className="panel">
        {runs.length === 0 ? (
          <p className="null-note">
            暂无执行记录（POST /api/runs 提交或 runner CLI submit 后出现在这里；
            queued 幻影行也会先落账本）。
          </p>
        ) : (
          <table className="ex-runs-table">
            <thead>
              <tr>
                <th>run_id</th>
                <th>项目 / commit</th>
                <th>状态</th>
                <th>归因（需人审/总数）</th>
                <th>创建时间</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {runs.map((run) => {
                const cancellable =
                  run.status === "queued" || run.status === "running";
                return (
                  <tr key={run.run_id}>
                    <td className="hash-line">
                      <Link
                        href={`/runs/detail?runId=${encodeURIComponent(run.run_id)}`}
                      >
                        {shortId(run.run_id, 20)}
                      </Link>
                    </td>
                    <td>
                      <div>{run.project}</div>
                      <div className="ex-subline hash-line">
                        {shortId(run.commit_sha, 12)}
                      </div>
                    </td>
                    <td>
                      <span className={runStatusClass(run.status)}>
                        {runStatusLabel(run.status)}
                      </span>
                      {run.error && (
                        <div className="ex-subline ap-error" title={run.error}>
                          {run.error}
                        </div>
                      )}
                    </td>
                    <td>
                      {run.verdict_total === 0 ? (
                        <span className="null-note">—</span>
                      ) : (
                        <span
                          className={
                            run.needs_human > 0
                              ? "ex-needs-human"
                              : "ex-verdict-ok"
                          }
                        >
                          {run.needs_human} / {run.verdict_total}
                        </span>
                      )}
                    </td>
                    <td className="hash-line">{run.created_at}</td>
                    <td>
                      {cancellable && (
                        <button
                          type="button"
                          className="link-btn"
                          disabled={busyId === run.run_id}
                          onClick={() => handleCancel(run.run_id)}
                        >
                          {busyId === run.run_id ? "提交中…" : "请求取消"}
                        </button>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
    </main>
  );
}
