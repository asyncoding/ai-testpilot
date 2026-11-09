"""TestPilot 核心域：统一领域模型、端口接口、策略。

纪律（docs/07 §3）：本包零 IO、零外部 SDK、零框架依赖；
只允许依赖 pydantic 与标准库。import-lint（scripts/import_lint.py）强制该校验。
"""

# v5（S1.4）：新增 qa_runs 表与 feedback.run_id 索引；老库经 schema.sql
# CREATE IF NOT EXISTS 幂等补建，无需 ALTER 列迁移。
# v6（S1.6 前置）：qa_runs 增 citation_errors 列，持久化引用校验的具体错误
# （quote_not_verbatim/urn_not_in_evidence/no_citations），老库经 _COLUMN_MIGRATIONS ALTER 补列。
# v7（P4 延迟治理）：qa_runs 增 intent_source 列，区分意图来源 regex_bypass（确定性
# 免模型）/ model / fallback，支撑两类 run 的延迟 SQL 切片对比。
# v8（S2.1 FR-CON-003）：新增 test_cases 表承载用例文档解析结果；解析死信复用
# dead_letters（source=fs-cases）。老库经 schema.sql CREATE IF NOT EXISTS 幂等补建。
# v9（S2.2 FR-TD-001）：新增 requirement_skeletons 表承载需求骨架抽取结果；
# 解析死信复用 dead_letters（source=fs-reqs），老库同样幂等补建。
SCHEMA_VERSION = 9

__all__ = ["SCHEMA_VERSION"]
