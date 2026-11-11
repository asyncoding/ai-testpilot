"use client";

// S4.5 知识管理页（FR-UI-004）：文档/切块/实体/质量分/stale 一屏盘点，
// 权威等级与知识 owner 内联编辑（R1 内部写，审计历史进 raw_json 治理段），
// stale 条目展示 valid_from→valid_to 的 reindex 状态，盘点表可导出。
// 静态导出由 FastAPI 同端口托管；离线 profile 的证据来自录制夹具（见首页 Banner）。
import Link from "next/link";
import { Fragment, useCallback, useEffect, useState } from "react";
import {
  editGovernance,
  knowledgeInventoryExportUrl,
  listDocumentChunks,
  listKnowledgeDocuments,
  listKnowledgeEntities,
  listStaleChunks,
} from "@/lib/api";
import type {
  GovernanceEditBody,
  KnowledgeChunkRow,
  KnowledgeDocumentRow,
  KnowledgeEntityRow,
  StaleChunkRow,
} from "@/lib/types";

const AUTHORITY_OPTIONS = [
  { value: "L0_realtime", label: "L0 实时联邦" },
  { value: "L1_authoritative", label: "L1 权威规范" },
  { value: "L2_experience", label: "L2 经验证词" },
  { value: "L3_rule", label: "L3 规则资产" },
];

function authorityLabel(value: string): string {
  return AUTHORITY_OPTIONS.find((o) => o.value === value)?.label ?? value;
}

function short(text: string, n = 48): string {
  return text.length > n ? `${text.slice(0, n)}…` : text;
}

export default function KnowledgePage() {
  const [docs, setDocs] = useState<KnowledgeDocumentRow[]>([]);
  const [stale, setStale] = useState<StaleChunkRow[]>([]);
  const [entities, setEntities] = useState<KnowledgeEntityRow[]>([]);
  const [branch, setBranch] = useState("");
  const [appliedBranch, setAppliedBranch] = useState("");
  const [expandedUrn, setExpandedUrn] = useState("");
  const [chunks, setChunks] = useState<KnowledgeChunkRow[]>([]);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busyUrn, setBusyUrn] = useState("");

  // 内联治理表单：每个文档一份草稿，未保存不影响其他行
  const [drafts, setDrafts] = useState<Record<string, { owner: string; level: string }>>({});

  const refresh = useCallback(async () => {
    try {
      const [docResp, staleResp, entityResp] = await Promise.all([
        listKnowledgeDocuments(appliedBranch || undefined),
        listStaleChunks(),
        listKnowledgeEntities(),
      ]);
      setDocs(docResp.documents);
      setStale(staleResp.stale_chunks);
      setEntities(entityResp.entities);
      setError("");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [appliedBranch]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const toggleChunks = async (urn: string) => {
    if (expandedUrn === urn) {
      setExpandedUrn("");
      setChunks([]);
      return;
    }
    try {
      const resp = await listDocumentChunks(urn);
      setExpandedUrn(urn);
      setChunks(resp.chunks);
      setError("");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const draftFor = (doc: KnowledgeDocumentRow) =>
    drafts[doc.urn] ?? { owner: doc.owner, level: doc.authority_level };

  const updateDraft = (
    urn: string,
    patch: Partial<{ owner: string; level: string }>,
  ) => {
    setDrafts((prev) => {
      const current = prev[urn] ?? { owner: "", level: "" };
      return { ...prev, [urn]: { ...current, ...patch } };
    });
  };

  const saveGovernance = async (doc: KnowledgeDocumentRow) => {
    const draft = draftFor(doc);
    const body: GovernanceEditBody = { edited_by: "web-console" };
    // 只提交发生变化的字段：未改 owner 就不回传，避免把空串当清空操作
    if (draft.owner.trim() && draft.owner !== doc.owner) body.owner = draft.owner.trim();
    if (draft.level && draft.level !== doc.authority_level)
      body.authority_level = draft.level;
    if (body.owner === undefined && body.authority_level === undefined) {
      setNotice("没有检测到变更，未发起写请求");
      return;
    }
    setBusyUrn(doc.urn);
    setNotice("");
    try {
      const result = await editGovernance(doc.urn, body);
      setNotice(
        `治理编辑已落账（owner=${result.owner || "—"}，` +
          `权威=${authorityLabel(result.authority_level)}，审计历史 ${
            result.governance.history.length
          } 条）`,
      );
      setDrafts((prev) => {
        const next = { ...prev };
        delete next[doc.urn];
        return next;
      });
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusyUrn("");
    }
  };

  return (
    <main className="container">
      <nav className="top-nav">
        <Link href="/">← 返回问答页（FR-UI-001）</Link>
      </nav>
      <h1>知识管理</h1>
      <p className="subtitle">
        FR-UI-004：文档 / 切块 / 实体关系 / 质量分 / stale 盘点 · 权威等级与知识
        owner 编辑（审计只追加，reindex 不抹除）；stale 行展示 reindex 留痕。
      </p>

      {error && <div className="error-banner">请求失败：{error}</div>}
      {notice && <div className="ap-notice">{notice}</div>}

      <form
        className="ex-filters"
        onSubmit={(e) => {
          e.preventDefault();
          setAppliedBranch(branch.trim());
        }}
      >
        <label>
          分支
          <input
            value={branch}
            placeholder="如 alarmkit-spike，留空全部"
            onChange={(e) => setBranch(e.target.value)}
          />
        </label>
        <button type="submit">应用过滤</button>
        <a className="link-btn" href={knowledgeInventoryExportUrl}>
          导出盘点表（JSON）
        </a>
      </form>

      <div className="panel">
        {docs.length === 0 ? (
          <p className="null-note">
            暂无知识文档（testpilot-ingest 入库后出现在这里；stale 计数来自
            chunks.stale 投影列）。
          </p>
        ) : (
          <table className="ex-runs-table">
            <thead>
              <tr>
                <th>文档</th>
                <th>分支</th>
                <th>权威等级 / owner</th>
                <th>切块（stale）</th>
                <th>最高质量分</th>
                <th>治理编辑</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {docs.map((doc) => {
                const draft = draftFor(doc);
                return (
                  <Fragment key={doc.urn}>
                    <tr>
                      <td>
                        <div>{doc.title || short(doc.urn, 36)}</div>
                        <div className="ex-subline hash-line" title={doc.urn}>
                          {short(doc.urn, 56)}
                        </div>
                      </td>
                      <td>{doc.branch}</td>
                      <td>
                        <div>{authorityLabel(doc.authority_level)}</div>
                        <div className="ex-subline">
                          owner：{doc.owner || <span className="null-note">未指派</span>}
                        </div>
                      </td>
                      <td>
                        {doc.chunk_count}
                        {doc.stale_count > 0 && (
                          <span className="ex-needs-human">（{doc.stale_count} stale）</span>
                        )}
                      </td>
                      <td>{doc.max_quality.toFixed(3)}</td>
                      <td>
                        <div className="composer-row">
                          <input
                            aria-label={`owner-${doc.urn}`}
                            placeholder="owner"
                            value={draft.owner}
                            onChange={(e) =>
                              updateDraft(doc.urn, { owner: e.target.value })
                            }
                          />
                          <select
                            aria-label={`authority-${doc.urn}`}
                            value={draft.level}
                            onChange={(e) =>
                              updateDraft(doc.urn, { level: e.target.value })
                            }
                          >
                            {AUTHORITY_OPTIONS.map((o) => (
                              <option key={o.value} value={o.value}>
                                {o.label}
                              </option>
                            ))}
                          </select>
                          <button
                            type="button"
                            className="link-btn"
                            disabled={busyUrn === doc.urn}
                            onClick={() => saveGovernance(doc)}
                          >
                            {busyUrn === doc.urn ? "提交中…" : "保存治理"}
                          </button>
                        </div>
                      </td>
                      <td>
                        <button
                          type="button"
                          className="link-btn"
                          onClick={() => toggleChunks(doc.urn)}
                        >
                          {expandedUrn === doc.urn ? "收起切块" : "查看切块"}
                        </button>
                      </td>
                    </tr>
                    {expandedUrn === doc.urn && (
                      <tr>
                        <td colSpan={7}>
                          <table className="ex-runs-table">
                            <thead>
                              <tr>
                                <th>#</th>
                                <th>kind</th>
                                <th>质量分</th>
                                <th>stale</th>
                                <th>valid_from → valid_to（reindex 留痕）</th>
                              </tr>
                            </thead>
                            <tbody>
                              {chunks.map((c) => (
                                <tr key={c.urn}>
                                  <td>{c.idx}</td>
                                  <td>{c.kind}</td>
                                  <td>{c.quality_score.toFixed(3)}</td>
                                  <td>
                                    {c.stale === "stale" ? (
                                      <span className="ex-needs-human">stale</span>
                                    ) : (
                                      "fresh"
                                    )}
                                  </td>
                                  <td className="hash-line">
                                    {short(c.valid_from ?? "—", 12)} →{" "}
                                    {short(c.valid_to ?? "—", 12)}
                                  </td>
                                </tr>
                              ))}
                            </tbody>
                          </table>
                        </td>
                      </tr>
                    )}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        )}
      </div>

      <h1>stale 切块（reindex 状态）</h1>
      <div className="panel">
        {stale.length === 0 ? (
          <p className="null-note">没有 stale 切块：检索默认隐藏过期内容，当前全库新鲜。</p>
        ) : (
          <table className="ex-runs-table">
            <thead>
              <tr>
                <th>切块 URN</th>
                <th>所属文档</th>
                <th>符号</th>
                <th>valid_from → valid_to</th>
              </tr>
            </thead>
            <tbody>
              {stale.map((s) => (
                <tr key={s.urn}>
                  <td className="hash-line" title={s.urn}>
                    {short(s.urn, 56)}
                  </td>
                  <td className="hash-line" title={s.doc_urn}>
                    {short(s.doc_urn, 40)}
                  </td>
                  <td className="hash-line">{s.symbol_urn ? short(s.symbol_urn, 40) : "—"}</td>
                  <td className="hash-line">
                    {short(s.valid_from ?? "—", 12)} → {short(s.valid_to ?? "—", 12)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <h1>实体盘点（L1）</h1>
      <div className="panel">
        {entities.length === 0 ? (
          <p className="null-note">暂无实体（代码符号/术语随入库管线派生）。</p>
        ) : (
          <table className="ex-runs-table">
            <thead>
              <tr>
                <th>名称</th>
                <th>kind</th>
                <th>分支</th>
                <th>所属文档</th>
              </tr>
            </thead>
            <tbody>
              {entities.map((e) => (
                <tr key={e.urn}>
                  <td>{e.name}</td>
                  <td>{e.kind}</td>
                  <td>{e.branch}</td>
                  <td className="hash-line" title={e.doc_urn ?? ""}>
                    {e.doc_urn ? short(e.doc_urn, 48) : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </main>
  );
}
