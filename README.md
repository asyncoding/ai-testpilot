# ai-testpilot

面向智能研发测试场景的 AI Agent Demo（工程 POC）——智能研发测试助手 TestPilot。

## 文档

- [docs/00-Demo总体设计-智能研发测试助手.md](docs/00-Demo总体设计-智能研发测试助手.md)
  系统全景、M1–M10 模块、四层知识库、脏乱数据治理、保鲜/重构/双轨、对接触点、演示脚本

  

  
- [docs/03-语料工程与实测环境（AlarmKitSpike）.md](docs/03-语料工程与实测环境（AlarmKitSpike）.md)
  被测工程事实、语料资产清单、xcodebuild/日志/xcresult 命令、各模块喂料方式
- [docs/12-用例导出与Xcode执行闭环方案.md](docs/12-用例导出与Xcode执行闭环方案.md)
  用例 JSON 导出 → 外部 Agent 生成 Xcode 测试 → 映射扫描 → 真实 xcodebuild 执行 → commit 影响面选择重跑的五切片方案与契约
- [docs/13-系统使用说明书（克隆到演示全流程）.md](docs/13-系统使用说明书（克隆到演示全流程）.md)
  新克隆仓库后的分步操作手册：环境准备、两个演示场景的完整命令顺序、故障排查表
- [docs/16-步骤断言关联与Oracle形态升级方案.md](docs/16-步骤断言关联与Oracle形态升级方案.md)
  Oracle 步骤锚点 step_id（P0+P1 已落地）、知识型 bullet 分流 context_notes（C2 已落地）、审批页步骤展示、case-export-v2 与外部 Agent prompt 升级（P3 待排），schema v15 零 DDL
- [docs/17-核心文档规范化方案.md](docs/17-核心文档规范化方案.md)
  方案稿：V1 需求/用例文档治理——ISO 29119-3/ISTQB/EARS/Gherkin/SbE 参照、五段式用例模板与 EARS 需求模板、testpilot-lint-cases 探针与量化指标、HITL 辅助重写流程、分批迁移计划
- [CLAUDE.md](CLAUDE.md)：Agent 工作纪律与跨会话接力规则（agy/opencode 等经 `AGENTS.md` 软链读取同一份）

## 目录

```
├── CLAUDE.md / AGENTS.md   # Agent 纪律（AGENTS.md 为软链）
├── docs/                   # 设计与计划文档
├── subsystems/             # 各子系统工程（按需新建，见该目录 README）
└── .agent-handoff/         # 跨 Agent 接力工作态（gitignore，不入库）
```
