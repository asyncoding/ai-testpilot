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
# v10（S2.3 FR-TD-002）：新增 test_points 表承载测试点生成结果（确定性脊柱 +
# 模型白名单补点）；模型幻觉拒入复用 dead_letters（source=tp-gen），老库幂等补建。
# v11（S2.4 FR-TD-003）：新增 oracle_suggestions 草稿表，pending 恒不回写正式
# test_cases；rejected 复用 dead_letters（source=oracle-mine），老库幂等补建。
# v12（S2.5 FR-TD-004）：oracle_suggestions 补人审四列（ALTER），新增
# preference_samples 表承载人工 modify diff 偏好样本，老库平滑迁移。
# v13（S2.6a FR-KNO-002）：新增 rule_assets 表承载 L3 规则资产版本链
# （prompt/few-shot/golden/glossary/tool_whitelist），只 INSERT 不改删，
# (asset_id, content_hash) 唯一约束保证同内容重放不增生，老库幂等补建。
# v14（S2.6c FR-EV-005）：新增 bad_cases 表承载反馈/bad case 闭环台账
# （状态机/四元组快照/golden 与回归关联），UNIQUE(source, source_ref) 与
# feedback_id 部分唯一索引保证负反馈幂等，老库 IF NOT EXISTS 幂等补建。
# v15（S3.1 FR-EX-002）：新增 test_runs 执行账本与 rerun_budget 重跑配额账本
# （先记账后入队，idempotency_key/request_id UNIQUE 保证重放幂等）；jobs 表
# ALTER 增 lease_expires_at/cancel_requested，支撑租约回收与协作式取消。
SCHEMA_VERSION = 15

__all__ = ["SCHEMA_VERSION"]
