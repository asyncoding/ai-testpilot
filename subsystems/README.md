# subsystems/

Demo 的各子系统工程放在这里，一个子系统一个目录，例如：

```
subsystems/
├── kb-ingest/        # M1/M2：连接器 + 知识加工管线
├── kb-store/         # M3：分层知识库（L0~L3、混合检索）
├── agent-testgen/    # M5：测试设计 Agent（LangGraph）
├── agent-runner/     # M6：测试执行与失败归因 Agent
├── eval/             # M9：golden set / RAGAS / 评测报告
└── web-console/      # 触点层：Web 控制台
```

约定：

- 每个子系统自带 README（跑法、依赖、边界）；跨子系统的公共契约（实体 schema、
  事件格式、工具接口）提到根 `docs/`。
- 向量库/数据库等外部依赖走**适配器接口**，本地最简实现优先（numpy/SQLite/文件），
  换 pgvector/Redis 等只换实现，业务代码不动。
- 所有中间产物不入库（见根 `.gitignore`）。
