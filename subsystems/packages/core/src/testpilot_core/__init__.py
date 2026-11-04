"""TestPilot 核心域：统一领域模型、端口接口、策略。

纪律（docs/07 §3）：本包零 IO、零外部 SDK、零框架依赖；
只允许依赖 pydantic 与标准库。import-lint（scripts/import_lint.py）强制该校验。
"""

SCHEMA_VERSION = 3

__all__ = ["SCHEMA_VERSION"]
