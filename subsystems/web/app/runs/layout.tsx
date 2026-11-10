import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "TestPilot · 测试执行与失败归因（FR-EX-002/007）",
  description:
    "执行账本盘点、用例三态、五候选归因证据链与显式未覆盖层（S3.5）",
};

export default function RunsLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return children;
}
