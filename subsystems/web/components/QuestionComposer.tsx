"use client";

import { useState } from "react";

export interface PresetCase {
  label: string;
  case_key: string;
  question: string;
}

// 与 scripts/qa_probe.py / verify_s0 A8 同款的四个确定性演示故事
export const PRESET_CASES: PresetCase[] = [
  {
    label: "① 代码守卫链→未知",
    case_key: "stale_code",
    question: "C.keep() 方法的实现逻辑是什么？",
  },
  {
    label: "② 事故双段证词",
    case_key: "kill_backup",
    question: "侧边键停钟失效的兜底机制是怎么演进的？现在 kill-backup 多少秒？",
  },
  {
    label: "③ 模型降级→未知",
    case_key: "unanswerable",
    question: "kill-backup 的告警阈值如何在生产环境配置？",
  },
  {
    label: "④ 闲聊 ACL 收口",
    case_key: "chitchat",
    question: "你好，讲个笑话",
  },
];

export interface ComposerPayload {
  question: string;
  case_key: string;
  acl_scope: string;
}

export default function QuestionComposer({
  onSubmit,
  loading,
}: {
  onSubmit: (payload: ComposerPayload) => void;
  loading: boolean;
}) {
  const [question, setQuestion] = useState("");
  const [caseKey, setCaseKey] = useState("");
  const [aclScope, setAclScope] = useState("default");

  const applyPreset = (preset: PresetCase) => {
    setQuestion(preset.question);
    setCaseKey(preset.case_key);
  };

  const submit = () => {
    if (!question.trim()) return;
    onSubmit({ question: question.trim(), case_key: caseKey, acl_scope: aclScope });
  };

  return (
    <div className="panel">
      <div className="preset-row">
        {PRESET_CASES.map((preset) => (
          <button
            key={preset.case_key}
            type="button"
            className="preset-btn"
            onClick={() => applyPreset(preset)}
          >
            {preset.label}
          </button>
        ))}
      </div>
      <div className="composer-row">
        <textarea
          value={question}
          placeholder="输入问题；点上方预设可自动填充确定性演示故事（携带 case_key）"
          onChange={(e) => {
            setQuestion(e.target.value);
            // 手动改题面后脱离预设故事：录制模型将走确定性未知降级
            setCaseKey("");
          }}
        />
        <div className="composer-side">
          <select
            value={aclScope}
            onChange={(e) => setAclScope(e.target.value)}
            aria-label="acl_scope"
          >
            <option value="default">acl: default</option>
            <option value="restricted">acl: restricted</option>
          </select>
          <button
            type="button"
            className="primary"
            disabled={loading || !question.trim()}
            onClick={submit}
          >
            {loading ? "提问中…" : "提问"}
          </button>
        </div>
      </div>
    </div>
  );
}
