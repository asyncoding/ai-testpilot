"""TestPilot 核心域：统一领域模型、端口接口、策略。

纪律（docs/07 §3）：本包零 IO、零外部 SDK、零框架依赖；
只允许依赖 pydantic 与标准库。import-lint（scripts/import_lint.py）强制该校验。
"""

# v5（S1.4）：新增 qa_runs 表与 feedback.run_id 索引；老库经 schema.sql
# CREATE IF NOT EXISTS 幂等补建，无需 ALTER 列迁移。
SCHEMA_VERSION = 5

__all__ = ["SCHEMA_VERSION"]
