"use client";

import { useCallback, useEffect, useState } from "react";
import AnswerCard from "@/components/AnswerCard";
import Banner from "@/components/Banner";
import FeedbackBar from "@/components/FeedbackBar";
import QuestionComposer, {
  type ComposerPayload,
} from "@/components/QuestionComposer";
import TraceDrawer from "@/components/TraceDrawer";
import { askQuestion, fetchConfig, listRuns } from "@/lib/api";
import type {
  AskResponse,
  ConfigResponse,
  RunListItem,
} from "@/lib/types";

export default function Home() {
  const [config, setConfig] = useState<ConfigResponse | null>(null);
  const [response, setResponse] = useState<AskResponse | null>(null);
  const [runs, setRuns] = useState<RunListItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [traceRunId, setTraceRunId] = useState<string | null>(null);

  useEffect(() => {
    fetchConfig().then(setConfig).catch((e) => setError(String(e)));
    listRuns()
      .then((r) => setRuns(r.runs))
      .catch(() => {
        // 首屏历史为空/服务未起时不阻断问答区
      });
  }, []);

  const refreshRuns = useCallback(() => {
    listRuns()
      .then((r) => setRuns(r.runs))
      .catch(() => undefined);
  }, []);

  const handleSubmit = async (payload: ComposerPayload) => {
    if (!config) return;
    setLoading(true);
    setError("");
    try {
      const resp = await askQuestion({
        question: payload.question,
        case_key: payload.case_key,
        acl_scope: payload.acl_scope,
        project: config.project,
        branch: config.branch,
      });
      setResponse(resp);
      refreshRuns();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  };

  return (
    <main className="container">
      <h1>TestPilot 智能研发测试助手</h1>
      <p className="subtitle">对话页雏形（FR-UI-001）：问答 · 可溯源引用 · 三态版本标注 · 反馈 · trace</p>

      <Banner config={config} />
      {error && <div className="error-banner">请求失败：{error}</div>}

      <QuestionComposer onSubmit={handleSubmit} loading={loading} />

      {response && (
        <>
          <AnswerCard response={response} />
          <div className="panel">
            <FeedbackBar runId={response.run_id} />
            <div className="feedback-bar">
              <button type="button" onClick={() => setTraceRunId(response.run_id)}>
                查看 trace 时间线
              </button>
            </div>
          </div>
        </>
      )}

      <div className="panel run-history">
        <strong>历史 run（最近 {runs.length} 条）</strong>
        {runs.length === 0 ? (
          <p className="null-note">暂无记录</p>
        ) : (
          <table>
            <thead>
              <tr>
                <th>run_id</th>
                <th>问题</th>
                <th>状态</th>
                <th>未知原因</th>
                <th>trace</th>
              </tr>
            </thead>
            <tbody>
              {runs.map((run) => (
                <tr key={run.run_id}>
                  <td className="hash-line">{run.run_id.slice(0, 12)}…</td>
                  <td>{run.question}</td>
                  <td>{run.status}</td>
                  <td>{run.unknown_reason || "—"}</td>
                  <td>
                    <button
                      type="button"
                      className="link-btn"
                      onClick={() => setTraceRunId(run.run_id)}
                    >
                      打开
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {traceRunId && (
        <TraceDrawer
          runId={traceRunId}
          open={traceRunId !== null}
          onClose={() => setTraceRunId(null)}
        />
      )}
    </main>
  );
}
