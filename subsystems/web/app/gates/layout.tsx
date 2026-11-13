import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "TestPilot · 人工闸门（hybrid-plan-v1）",
  description: "混合执行人工操作队列、感官裁决单与证据绑定（docs/15 §7）",
};

export default function GatesLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return children;
}
