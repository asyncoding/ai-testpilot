import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "TestPilot · 知识管理（FR-UI-004）",
  description:
    "文档/切块/实体关系/质量分/stale 盘点，权威等级与知识 owner 编辑（S4.5）",
};

export default function KnowledgeLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return children;
}
