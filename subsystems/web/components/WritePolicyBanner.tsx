"use client";

// FR-WR-001 AC2 一键停用外写：profile 开关是硬边界，运行期 kill switch 只能收紧。
import type { WritePolicyView } from "@/lib/types";

interface Props {
  policy: WritePolicyView | null;
  busy: boolean;
  onToggle: (runtimeDisabled: boolean) => void;
}

export default function WritePolicyBanner({ policy, busy, onToggle }: Props) {
  if (!policy) {
    return (
      <div className="panel banner banner-readonly">
        写策略加载中…（若服务未起，审批操作不可用）
      </div>
    );
  }

  const { profile_external_write_enabled, runtime_disabled, effective_writes_enabled } =
    policy;

  if (effective_writes_enabled) {
    return (
      <div className="panel kill-on">
        <div className="status-line">
          <strong>外部写入：已启用</strong>
          <span className="tag">profile 允许</span>
          {!runtime_disabled && <span className="tag">kill switch 未停用</span>}
        </div>
        <p className="null-note">
          派发 outbox 会经 MarkdownCaseRepository 真实回写文件。紧急情况下可一键停用
          所有外部写入（进程内即时生效，只收紧不越权）。
        </p>
        <button
          type="button"
          className="danger"
          disabled={busy}
          onClick={() => onToggle(true)}
        >
          🛑 一键停用所有外部写入
        </button>
      </div>
    );
  }

  const reason = !profile_external_write_enabled
    ? "profile 未启用外部写（external_write_enabled=false）——硬边界，运行期不可越权放开"
    : "kill switch 已停用（运行期收紧）";

  return (
    <div className="panel kill-off">
      <div className="status-line">
        <strong>外部写入：已停用</strong>
        <span className="tag">{reason}</span>
      </div>
      <p className="null-note">
        此状态下派发 outbox 只会被策略层拦截（blocked，不消耗重试次数），不会产生任何
        文件/外部系统写入。
      </p>
      {profile_external_write_enabled && (
        <button type="button" disabled={busy} onClick={() => onToggle(false)}>
          恢复外部写入
        </button>
      )}
    </div>
  );
}
