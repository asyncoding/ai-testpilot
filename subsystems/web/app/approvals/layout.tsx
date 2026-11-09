import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "TestPilot · 用例审批（FR-UI-002）",
  description: "草稿人审、outbox 幂等回写与外部写 kill switch（FR-TD-004 / FR-WR-001）",
};

export default function ApprovalsLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return children;
}
