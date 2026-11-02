"""TestPilot 适配器层：端口接口的具体实现。

每个适配器只做"外部世界 ↔ 核心统一模型"的翻译与防腐；
不做业务判断（业务判断在 orchestrator 工作流与 core.policy）。
"""
