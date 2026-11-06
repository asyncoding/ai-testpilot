"use client";

import { useState } from "react";
import { fetchSource } from "@/lib/api";
import type { QaCitation, SourceEnvelope } from "@/lib/types";

const STATE_LABEL: Record<string, string> = {
  consistent: "版本一致",
  verified_at: "已现场核验",
  unknown: "版本未知",
};

// 引用列表：三态徽章 + 点击经白名单代理拉原文（含 hash/uri/权威等级）。
// 后端白名单外 URN 一律 404，前端不拼任何文件系统路径。
export default function CitationList({
  runId,
  citations,
}: {
  runId: string;
  citations: QaCitation[];
}) {
  const [source, setSource] = useState<SourceEnvelope | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const openSource = async (urn: string) => {
    setLoading(true);
    setError("");
    setSource(null);
    try {
      setSource(await fetchSource(runId, urn));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  };

  if (!citations.length) return null;
  return (
    <div>
      <h4>引用（{citations.length}）</h4>
      <ul className="citation-list">
        {citations.map((cite) => (
          <li key={cite.urn}>
            <div className="cite-head">
              <span className={`badge badge-${cite.version_state}`}>
                {STATE_LABEL[cite.version_state] ?? cite.version_state}
                {cite.version_state === "verified_at" && cite.version_ref
                  ? ` @ ${cite.version_ref}`
                  : ""}
              </span>
              <button
                type="button"
                className="link-btn"
                onClick={() => openSource(cite.urn)}
              >
                查看原文与 hash
              </button>
              <span className="cite-urn">{cite.urn}</span>
            </div>
            {cite.quote && (
              <p className="cite-quote">「{cite.quote}」</p>
            )}
          </li>
        ))}
      </ul>

      {(loading || source || error) && (
        <>
          <div className="drawer-mask" onClick={() => setSource(null)} />
          <div className="drawer" role="dialog" aria-label="证据原文">
            <button type="button" className="drawer-close" onClick={() => setSource(null)}>
              ✕
            </button>
            <h3>证据原文</h3>
            {loading && <p>加载中…</p>}
            {error && <p className="feedback-err">原文获取失败：{error}</p>}
            {source && (
              <>
                <table className="kv-table">
                  <tbody>
                    <tr><th>URN</th><td className="hash-line">{source.urn}</td></tr>
                    <tr><th>kind</th><td>{source.kind}</td></tr>
                    <tr><th>content_hash</th><td className="hash-line">{source.content_hash}</td></tr>
                    <tr><th>source_uri</th><td className="hash-line">{source.source_uri || "—"}</td></tr>
                    <tr><th>权威等级</th><td>{source.authority_level || "—"}</td></tr>
                    <tr><th>分支 / stale</th><td>{source.branch || "—"} / {String(source.stale)}</td></tr>
                  </tbody>
                </table>
                <h4>原文</h4>
                <div className="source-text">{source.text}</div>
              </>
            )}
          </div>
        </>
      )}
    </div>
  );
}
