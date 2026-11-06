import type { ConfigResponse } from "@/lib/types";

// FR-OPS-002：offline-demo 页面必须显式标注「证据来自录制夹具」，
// 并如实说明录制模型下自由提问会走未知降级（不是 bug，是夹具本质限制）。
export default function Banner({ config }: { config: ConfigResponse | null }) {
  if (!config) return null;
  return (
    <div>
      {config.recorded && (
        <div className="banner banner-recorded">
          <strong>证据来自录制夹具（profile: {config.profile}）</strong>
          本演示不访问真实大模型：模型应答、测试结果与 git 核验均来自预先录制的确定性夹具。
          四个预设问题可完整演示；自由提问因录制响应缺少对应键，会确定性降级为
          「未知 / model_unavailable」，这是诚实降级而非故障。
        </div>
      )}
      <div className="banner banner-readonly">
        external_write_enabled = {String(config.external_write_enabled)}
        ：演示环境默认 dry-run，所有外部写操作只进 outbox，不真实外发。
        检索适配器：{config.search_adapter}；tracer：{config.tracer}
        （noop 时 trace 抽屉不提供 Langfuse 深链）。
      </div>
    </div>
  );
}
