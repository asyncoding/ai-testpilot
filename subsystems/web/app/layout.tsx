import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "TestPilot 智能研发测试助手 · 对话页",
  description: "知识问答 + 可溯源引用 + 三态版本标注 + 反馈与 trace（FR-UI-001）",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  );
}
