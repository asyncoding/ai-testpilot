# ai-testpilot

面向智能研发测试场景的 AI Agent Demo（工程 POC）——智能研发测试助手 TestPilot。

## 文档

- [docs/00-Demo总体设计-智能研发测试助手.md](docs/00-Demo总体设计-智能研发测试助手.md)
  系统全景、M1–M10 模块、四层知识库、脏乱数据治理、保鲜/重构/双轨、对接触点、演示脚本

  

  
- [docs/03-语料工程与实测环境（AlarmKitSpike）.md](docs/03-语料工程与实测环境（AlarmKitSpike）.md)
  被测工程事实、语料资产清单、xcodebuild/日志/xcresult 命令、各模块喂料方式
- [CLAUDE.md](CLAUDE.md)：Agent 工作纪律与跨会话接力规则（agy/opencode 等经 `AGENTS.md` 软链读取同一份）

## 目录

```
├── CLAUDE.md / AGENTS.md   # Agent 纪律（AGENTS.md 为软链）
├── docs/                   # 设计与计划文档
├── subsystems/             # 各子系统工程（按需新建，见该目录 README）
└── .agent-handoff/         # 跨 Agent 接力工作态（gitignore，不入库）
```
