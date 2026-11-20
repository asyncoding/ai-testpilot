"use client";

// docs/01 §7 单条人工闸门卡片：pending 展示倒计时与操作/裁决表单，
// expired 给续期/放弃，responded/canceled 只读留痕。证据 URN 强制时
// 前端先拦一道（后端 core 仍 fail-closed，前端只是体验层）。
import { useEffect, useState } from "react";
import { abandonGate, renewGate, respondGate } from "@/lib/api";
import type { GateView, VerdictFieldView } from "@/lib/types";

const REVIEWER_KEY = "testpilot.gate.reviewer";
const RENEW_SECONDS = 300;

function remainingSeconds(deadline: string, now: number): number {
  return Math.max(0, Math.floor((new Date(deadline).getTime() - now) / 1000));
}

function formatCountdown(seconds: number): string {
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return `${m}:${s.toString().padStart(2, "0")}`;
}

const STATUS_LABEL: Record<string, string> = {
  pending: "待人工",
  expired: "已超时",
  responded: "已响应",
  canceled: "已放弃",
};

interface Props {
  gate: GateView;
  now: number;
  onChanged: () => void;
}

export default function GateCard({ gate, now, onChanged }: Props) {
  const fields = gate.verdict_form?.fields ?? [];
  const [values, setValues] = useState<Record<string, unknown>>(() =>
    Object.fromEntries(
      fields.map((f) => [
        f.key,
        f.kind === "boolean" ? true : f.kind === "choice" ? f.options[0] ?? "" : "",
      ]),
    ),
  );
  const [evidence, setEvidence] = useState(gate.evidence_urns.join("\n"));
  const [note, setNote] = useState("");
  const [reviewer, setReviewer] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    setReviewer(localStorage.getItem(REVIEWER_KEY) ?? "");
  }, []);

  const requireEvidence = gate.verdict_form?.require_evidence ?? false;
  const evidenceUrns = evidence.split("\n").map((s) => s.trim()).filter(Boolean);

  async function guard(action: () => Promise<unknown>) {
    setBusy(true);
    setError("");
    try {
      await action();
      onChanged();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  function submit() {
    if (reviewer.trim()) localStorage.setItem(REVIEWER_KEY, reviewer.trim());
    if (requireEvidence && evidenceUrns.length === 0) {
      setError("本裁决要求绑定系统侧证据 URN（os_log/截图/录屏），不接受空口打勾");
      return;
    }
    const missing = fields
      .filter((f: VerdictFieldView) => f.required)
      .filter((f) => values[f.key] === "" || values[f.key] === undefined)
      .map((f) => f.label);
    if (missing.length) {
      setError(`必填字段未完成：${missing.join("、")}`);
      return;
    }
    void guard(() =>
      respondGate(gate.gate_id, {
        values,
        evidence_urns: evidenceUrns,
        note,
        decided_by: reviewer.trim(),
      }),
    );
  }

  const closed = gate.status === "responded" || gate.status === "canceled";
  const remaining = remainingSeconds(gate.deadline, now);

  return (
    <section className={`panel gate-card gate-${gate.status}`}>
      <header className="gate-head">
        <div>
          <span className={`badge gate-badge gate-badge-${gate.status}`}>
            {STATUS_LABEL[gate.status] ?? gate.status}
          </span>
          <span className="gate-actor">{gate.actor}</span>
        </div>
        <div className="gate-meta">
          <span title="会话 / 步骤">
            {gate.session_id} · {gate.step_id}
          </span>
          {gate.status === "pending" && (
            <span
              className={`gate-countdown ${remaining < 30 ? "gate-countdown-warn" : ""}`}
              title="距超时"
            >
              ⏱ {formatCountdown(remaining)}
            </span>
          )}
        </div>
      </header>

      <p className="gate-prompt">{gate.prompt}</p>

      {gate.evidence_urns.length > 0 && (
        <div className="gate-evidence">
          <strong>系统侧证据：</strong>
          {gate.evidence_urns.map((urn) => (
            <code key={urn} className="cite-urn">
              {urn}
            </code>
          ))}
        </div>
      )}

      {!closed && fields.length > 0 && (
        <div className="gate-form">
          {fields.map((f) => (
            <label key={f.key} className="gate-field">
              <span>
                {f.label}
                {f.required ? " *" : ""}
              </span>
              {f.kind === "boolean" ? (
                <select
                  value={String(values[f.key])}
                  onChange={(e) =>
                    setValues((v) => ({ ...v, [f.key]: e.target.value === "true" }))
                  }
                >
                  <option value="true">是</option>
                  <option value="false">否</option>
                </select>
              ) : f.kind === "choice" ? (
                <select
                  value={String(values[f.key] ?? "")}
                  onChange={(e) =>
                    setValues((v) => ({ ...v, [f.key]: e.target.value }))
                  }
                >
                  {f.options.map((opt) => (
                    <option key={opt} value={opt}>
                      {opt}
                    </option>
                  ))}
                </select>
              ) : (
                <input
                  type="text"
                  value={String(values[f.key] ?? "")}
                  onChange={(e) =>
                    setValues((v) => ({ ...v, [f.key]: e.target.value }))
                  }
                />
              )}
            </label>
          ))}
        </div>
      )}

      {!closed && (
        <div className="gate-form">
          {requireEvidence && (
            <label className="gate-field">
              <span>证据 URN（每行一个，强制）*</span>
              <textarea
                rows={2}
                value={evidence}
                onChange={(e) => setEvidence(e.target.value)}
                placeholder="urn:testpilot:store:artifact:..."
              />
            </label>
          )}
          <label className="gate-field">
            <span>备注（可选）</span>
            <input type="text" value={note} onChange={(e) => setNote(e.target.value)} />
          </label>
          <label className="gate-field">
            <span>见证人 *</span>
            <input
              type="text"
              value={reviewer}
              onChange={(e) => setReviewer(e.target.value)}
              placeholder="POC 单用户无认证，但必填留痕"
            />
          </label>
        </div>
      )}

      {!closed && (
        <div className="gate-actions">
          {gate.status === "pending" && (
            <button
              type="button"
              className="primary"
              disabled={busy || !reviewer.trim()}
              onClick={submit}
            >
              {gate.verdict_form ? "提交裁决" : "确认人工动作已完成"}
            </button>
          )}
          {gate.status === "expired" && (
            <button
              type="button"
              className="primary"
              disabled={busy}
              onClick={() =>
                guard(() => renewGate(gate.gate_id, RENEW_SECONDS))
              }
            >
              续期 5 分钟（重开闸门）
            </button>
          )}
          <button
            type="button"
            className="danger"
            disabled={busy}
            onClick={() => guard(() => abandonGate(gate.gate_id))}
          >
            放弃（会话转取消）
          </button>
        </div>
      )}

      {closed && gate.response && (
        <div className="gate-closed">
          <strong>裁决留痕：</strong>
          <code>{JSON.stringify(gate.response.values)}</code>
          <span className="tag">{gate.response.decided_by}</span>
          <span className="tag">{gate.response.decided_at}</span>
          {gate.response.evidence_urns.map((urn) => (
            <code key={urn} className="cite-urn">
              {urn}
            </code>
          ))}
        </div>
      )}
      {gate.status === "canceled" && (
        <p className="gate-closed">该闸门已放弃，关联混合会话应走 canceled 收口。</p>
      )}
      {gate.renewed_at && (
        <p className="gate-renewed">曾续期：{gate.renewed_at}</p>
      )}
      {error && <p className="citation-errors">{error}</p>}
    </section>
  );
}
