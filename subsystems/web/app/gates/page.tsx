"use client";

// docs/15 §7 人工闸门页：5s 轮询队列（不做 SSE/系统通知，headless 友好），
// pending/expired 置顶并由 GateCard 展示倒计时与裁决表单；已闭环闸门只读留痕。
// 倒计时本地每秒重算，后端读时按 deadline 现算过期，两侧都不信任对方时钟做状态。
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import GateCard from "@/components/GateCard";
import { listGates } from "@/lib/api";
import type { GateView } from "@/lib/types";

const POLL_MS = 5000;

export default function GatesPage() {
  const [gates, setGates] = useState<GateView[]>([]);
  const [now, setNow] = useState(() => Date.now());
  const [error, setError] = useState("");
  const [updatedAt, setUpdatedAt] = useState("");

  const refresh = useCallback(async () => {
    const data = await listGates();
    setGates(data.gates);
    setUpdatedAt(new Date().toLocaleTimeString());
  }, []);

  useEffect(() => {
    refresh().catch((e) => setError(e instanceof Error ? e.message : String(e)));
    const poll = setInterval(
      () => refresh().catch((e) => setError(String(e))),
      POLL_MS,
    );
    const tick = setInterval(() => setNow(Date.now()), 1000);
    return () => {
      clearInterval(poll);
      clearInterval(tick);
    };
  }, [refresh]);

  const open = gates.filter((g) => g.status === "pending" || g.status === "expired");
  const closed = gates.filter(
    (g) => g.status === "responded" || g.status === "canceled",
  );
  const pendingCount = gates.filter((g) => g.status === "pending").length;
  const expiredCount = gates.filter((g) => g.status === "expired").length;

  return (
    <main className="container">
      <h1>人工闸门（hybrid-plan-v1）</h1>
      <p className="subtitle">
        混合执行的人工操作与感官裁决队列 · 待人工 {pendingCount} · 已超时 {expiredCount}
        {" · "}
        最近刷新 {updatedAt || "—"} · <Link href="/">返回对话页</Link>
      </p>

      {error && <p className="citation-errors">轮询失败：{error}</p>}

      {open.length === 0 && closed.length === 0 && (
        <section className="panel">
          <p>当前没有闸门。混合会话走到 human:action / human:verdict 步骤时，闸门会出现在这里。</p>
        </section>
      )}

      {open.length > 0 && (
        <>
          <h2>待处理（{open.length}）</h2>
          {open.map((gate) => (
            <GateCard key={gate.gate_id} gate={gate} now={now} onChanged={refresh} />
          ))}
        </>
      )}

      {closed.length > 0 && (
        <>
          <h2>已闭环（{closed.length}）</h2>
          {closed.map((gate) => (
            <GateCard key={gate.gate_id} gate={gate} now={now} onChanged={refresh} />
          ))}
        </>
      )}
    </main>
  );
}
