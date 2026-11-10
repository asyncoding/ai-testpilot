-- TestPilot schema（当前 schema_version = 14；docs/08 FR-CFG-003 AC2：迁移脚本版本化）
-- v1：S0 骨架；v2：入库批次/文档加工状态、entities/relations 挂 doc_urn（S1.1）
-- v3：chunks_fts 换 trigram 分词（CJK 子串召回，S1.2；老库由 _migrate_fts_to_trigram
--     DROP+回填）、retrieval_events 各检索阶段命中率留痕（FR-KNO-001 AC1）
-- v4：verification_events live-verify 每次校验留痕（S1.3，FR-QA-002 AC2：代码类问题
--     100% 有 trace 可查的校验调用，含降级；CREATE IF NOT EXISTS 对老库幂等，无列迁移）
-- v5~v12：qa_runs/反馈、test_cases/requirement_skeletons/test_points、
--     oracle_suggestions（v11）+ 人审四列与 preference_samples（v12），见 core/__init__.py
-- v13：rule_assets L3 规则资产版本链（S2.6a FR-KNO-002），新表 IF NOT EXISTS 幂等补建
-- v14：bad_cases 反馈/bad case 闭环台账（S2.6c FR-EV-005），新表 IF NOT EXISTS 幂等补建

PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 知识源文档
CREATE TABLE IF NOT EXISTS source_documents (
    urn             TEXT PRIMARY KEY,
    source_system   TEXT NOT NULL,
    uri             TEXT NOT NULL,
    title           TEXT NOT NULL DEFAULT '',
    version         TEXT,
    content_hash    TEXT NOT NULL,
    authority_level TEXT NOT NULL,
    acl_scope       TEXT NOT NULL DEFAULT 'default',
    branch          TEXT NOT NULL DEFAULT 'main',
    mtime           TEXT,
    raw_json        TEXT NOT NULL
);

-- 切块
CREATE TABLE IF NOT EXISTS chunks (
    urn           TEXT PRIMARY KEY,
    doc_urn       TEXT NOT NULL REFERENCES source_documents(urn),
    idx           INTEGER NOT NULL,
    kind          TEXT NOT NULL,
    text          TEXT NOT NULL,
    symbol_urn    TEXT,
    quality_score REAL NOT NULL DEFAULT 0.5,
    branch        TEXT NOT NULL DEFAULT 'main',
    valid_from    TEXT,
    valid_to      TEXT,
    stale         TEXT NOT NULL DEFAULT 'fresh',
    content_hash  TEXT NOT NULL,
    raw_json      TEXT NOT NULL,
    UNIQUE(doc_urn, idx)
);

-- trigram：连续汉字整段成词的 unicode61 对自然语言必 0 命中（实测，见 handoff
-- S1.1.2）；trigram 按 3 字子串索引，支持 CJK 子串召回（<3 字符词不索引，
-- 查询侧用 LIKE 兜底，见 HybridSearchAdapter）。
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text,
    chunk_urn UNINDEXED,
    tokenize = 'trigram'
);

-- 实体与关系（L1）
CREATE TABLE IF NOT EXISTS entities (
    urn       TEXT PRIMARY KEY,
    kind      TEXT NOT NULL,
    name      TEXT NOT NULL,
    branch    TEXT NOT NULL DEFAULT 'main',
    doc_urn   TEXT,
    raw_json  TEXT NOT NULL
);
-- P3 符号路由：查询侧按 entities.name 精确反查（IF NOT EXISTS 幂等，老库启动即建）
CREATE INDEX IF NOT EXISTS idx_entities_name_branch ON entities(name, branch);

CREATE TABLE IF NOT EXISTS relations (
    urn           TEXT PRIMARY KEY,
    source_urn    TEXT NOT NULL,
    target_urn    TEXT NOT NULL,
    kind          TEXT NOT NULL,
    confidence    REAL NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '[]',
    doc_urn       TEXT
);

-- 入库批次与文档加工状态（v2，FR-ING-001：每步留痕、可查可重跑）
CREATE TABLE IF NOT EXISTS ingestion_runs (
    run_id         TEXT PRIMARY KEY,
    source_system  TEXT NOT NULL,
    trigger        TEXT NOT NULL DEFAULT 'manual',
    status         TEXT NOT NULL DEFAULT 'running',
    docs_seen      INTEGER NOT NULL DEFAULT 0,
    docs_created   INTEGER NOT NULL DEFAULT 0,
    docs_updated   INTEGER NOT NULL DEFAULT 0,
    docs_deleted   INTEGER NOT NULL DEFAULT 0,
    docs_unchanged INTEGER NOT NULL DEFAULT 0,
    docs_failed    INTEGER NOT NULL DEFAULT 0,
    chunks_total   INTEGER NOT NULL DEFAULT 0,
    report_json    TEXT NOT NULL DEFAULT '{}',
    started_at     TEXT NOT NULL,
    finished_at    TEXT
);

CREATE TABLE IF NOT EXISTS ingestion_status (
    doc_urn       TEXT PRIMARY KEY,
    source_system TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    stage         TEXT NOT NULL,          -- indexed / failed（内容加工阶段）
    chunk_count   INTEGER NOT NULL DEFAULT 0,
    avg_quality   REAL NOT NULL DEFAULT 0,
    last_run_id   TEXT NOT NULL,
    error         TEXT NOT NULL DEFAULT '',
    updated_at    TEXT NOT NULL,
    -- v3 增量列：向量嵌入阶段（独立于内容加工，失败可重试，unchanged 不重嵌）
    embed_stage       TEXT NOT NULL DEFAULT '',  -- '' / embedded / failed
    embed_model       TEXT NOT NULL DEFAULT '',
    embed_dim         INTEGER NOT NULL DEFAULT 0,
    embed_latency_ms  INTEGER NOT NULL DEFAULT 0,
    embed_error       TEXT NOT NULL DEFAULT '',
    embedded_at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_ingestion_status_stage ON ingestion_status(stage);

-- 检索各阶段留痕（v3，FR-KNO-001 AC1：vector-only/BM25-only/混合命中率可对比）
CREATE TABLE IF NOT EXISTS retrieval_events (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    ts               TEXT NOT NULL,
    query            TEXT NOT NULL,
    query_sha        TEXT NOT NULL,
    project          TEXT NOT NULL DEFAULT '',
    branch           TEXT NOT NULL DEFAULT 'main',
    acl_scope        TEXT NOT NULL DEFAULT 'default',
    kinds            TEXT NOT NULL DEFAULT '',     -- 逗号分隔的类型过滤
    model_version    TEXT NOT NULL DEFAULT '',
    fts_available    INTEGER NOT NULL DEFAULT 1,
    vector_available INTEGER NOT NULL DEFAULT 1,
    degraded         INTEGER NOT NULL DEFAULT 0,
    fts_urns         TEXT NOT NULL DEFAULT '[]',
    vector_urns      TEXT NOT NULL DEFAULT '[]',
    fused_urns       TEXT NOT NULL DEFAULT '[]',
    fts_latency_ms   INTEGER NOT NULL DEFAULT 0,
    vector_latency_ms INTEGER NOT NULL DEFAULT 0,
    raw_json         TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_retrieval_events_ts ON retrieval_events(ts);

-- live-verify 校验留痕（v4，FR-QA-002 AC2：代码类问题生成前 100% 实时复核；
-- 含 verified/body_changed/exists 与降级原因；exists/body_changed 可空——
-- 降级时没有事实结论，绝不拿 None 冒充 False）
CREATE TABLE IF NOT EXISTS verification_events (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    ts               TEXT NOT NULL,
    urn              TEXT NOT NULL,
    kind             TEXT NOT NULL DEFAULT '',
    project          TEXT NOT NULL DEFAULT '',
    branch           TEXT NOT NULL DEFAULT '',
    requested_commit TEXT NOT NULL DEFAULT '',
    head_commit      TEXT NOT NULL DEFAULT '',
    symbol_exists    INTEGER,  -- EXISTS 是 SQL 保留字，列名避开；语义=符号/锚点在目标版本是否存在
    verified         INTEGER NOT NULL DEFAULT 0,
    body_changed     INTEGER,
    degraded         INTEGER NOT NULL DEFAULT 0,
    reason           TEXT NOT NULL DEFAULT '',
    error            TEXT NOT NULL DEFAULT '',
    indexed_commit         TEXT,
    indexed_chunker_version TEXT,
    current_chunker_version TEXT NOT NULL DEFAULT '',
    latency_ms       INTEGER NOT NULL DEFAULT 0,
    raw_json         TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_verification_events_ts ON verification_events(ts);

-- 人审
CREATE TABLE IF NOT EXISTS approvals (
    approval_id     TEXT PRIMARY KEY,
    action_level    TEXT NOT NULL,
    target_system   TEXT NOT NULL,
    request_summary TEXT NOT NULL,
    request_diff    TEXT NOT NULL DEFAULT '{}',
    decision        TEXT NOT NULL DEFAULT 'pending',
    reviewer        TEXT,
    reason          TEXT NOT NULL DEFAULT '',
    based_on        TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL,
    decided_at      TEXT
);

-- 外部副作用账本（先账后事，docs/08 FR-WR-002）
CREATE TABLE IF NOT EXISTS outbox (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key  TEXT NOT NULL UNIQUE,
    action_level     TEXT NOT NULL,
    target_system    TEXT NOT NULL,
    request_body     TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'pending',
    external_ref     TEXT,
    approval_id      TEXT NOT NULL REFERENCES approvals(approval_id),
    attempts         INTEGER NOT NULL DEFAULT 0,
    rollback_note    TEXT NOT NULL DEFAULT '',
    error            TEXT NOT NULL DEFAULT '',
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

-- 反馈
CREATE TABLE IF NOT EXISTS feedback (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT NOT NULL,
    target_urn    TEXT NOT NULL,
    kind          TEXT NOT NULL,
    reason        TEXT,
    detail        TEXT NOT NULL DEFAULT '',
    model_version TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL
);
-- S1.4：bad case 池 = feedback join qa_runs，run_id 走索引
CREATE INDEX IF NOT EXISTS idx_feedback_run_id ON feedback(run_id);

-- 问答运行记录（S1.4 FR-QA-001～004）：一次 ask 一行，节点无副作用，
-- 由 QaService 在图执行后统一落库；四元组列与 eval_runs 对齐，S1.6 可直接 join。
CREATE TABLE IF NOT EXISTS qa_runs (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id             TEXT NOT NULL UNIQUE,
    ts                 TEXT NOT NULL,
    project            TEXT NOT NULL DEFAULT '',
    branch             TEXT NOT NULL DEFAULT 'main',
    acl_scope          TEXT NOT NULL DEFAULT 'default',
    question           TEXT NOT NULL,
    intent             TEXT NOT NULL DEFAULT '',
    status             TEXT NOT NULL,
    answer_payload     TEXT NOT NULL DEFAULT '{}',
    citations          TEXT NOT NULL DEFAULT '[]',
    evidence           TEXT NOT NULL DEFAULT '[]',
    verify_results     TEXT NOT NULL DEFAULT '[]',
    retrieval_event_id INTEGER,
    unknown_reason     TEXT NOT NULL DEFAULT '',
    -- v6：引用校验具体错误（quote_not_verbatim/urn_not_in_evidence/no_citations），
    -- 只存最后一次 citation_check 的结果；空列表=校验通过或未进生成
    citation_errors    TEXT NOT NULL DEFAULT '[]',
    -- v7：意图来源 regex_bypass（符号形态免模型）/ model / fallback；老行为 '' 表 legacy
    intent_source      TEXT NOT NULL DEFAULT '',
    degraded           TEXT NOT NULL DEFAULT '[]',
    attempts           INTEGER NOT NULL DEFAULT 0,
    regens             INTEGER NOT NULL DEFAULT 0,
    model_version      TEXT NOT NULL DEFAULT '',
    prompt_version     TEXT NOT NULL DEFAULT '',
    params_version     TEXT NOT NULL DEFAULT '',
    data_version       TEXT NOT NULL DEFAULT '',
    tokens_in          INTEGER NOT NULL DEFAULT 0,
    tokens_out         INTEGER NOT NULL DEFAULT 0,
    latency_ms         INTEGER NOT NULL DEFAULT 0,
    thread_id          TEXT NOT NULL DEFAULT '',
    created_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_qa_runs_ts ON qa_runs(ts);
CREATE INDEX IF NOT EXISTS idx_qa_runs_status ON qa_runs(status);

-- 评测
CREATE TABLE IF NOT EXISTS eval_cases (
    case_id       TEXT PRIMARY KEY,
    kind          TEXT NOT NULL,
    question      TEXT NOT NULL,
    ground_truth  TEXT NOT NULL DEFAULT '{}',
    required_evidence TEXT NOT NULL DEFAULT '[]',
    holdout       INTEGER NOT NULL DEFAULT 0,
    locked_version TEXT
);

CREATE TABLE IF NOT EXISTS eval_runs (
    eval_run_id   TEXT PRIMARY KEY,
    data_version  TEXT NOT NULL,
    model_version TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    params_version TEXT NOT NULL,
    degraded_path INTEGER NOT NULL DEFAULT 0,
    metrics       TEXT NOT NULL DEFAULT '{}',
    report_uri    TEXT,
    created_at    TEXT NOT NULL
);

-- 事件与死信
CREATE TABLE IF NOT EXISTS events (
    event_id      TEXT PRIMARY KEY,
    event_type    TEXT NOT NULL,
    source_system TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'received',
    payload_json  TEXT NOT NULL DEFAULT '{}',
    occurred_at   TEXT NOT NULL,
    received_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dead_letters (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    error       TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

-- 结构化用例（S2.1 FR-CON-003）：用例 Markdown 连接器的解析产物。
-- 死信不另起表：解析失败统一进 dead_letters（source='fs-cases'），UI 可查。
-- 重灌幂等：URN 稳定（文档 external_id + TC ID），按 doc_urn 整组替换。
CREATE TABLE IF NOT EXISTS test_cases (
    urn            TEXT PRIMARY KEY,
    external_id    TEXT NOT NULL,
    case_id        TEXT NOT NULL,
    doc_urn        TEXT NOT NULL,
    module         TEXT NOT NULL DEFAULT '',
    title          TEXT NOT NULL DEFAULT '',
    priority       TEXT NOT NULL DEFAULT '',
    lifecycle      TEXT NOT NULL DEFAULT 'active',
    oracle_missing INTEGER NOT NULL DEFAULT 0,
    step_count     INTEGER NOT NULL DEFAULT 0,
    oracle_count   INTEGER NOT NULL DEFAULT 0,
    source_line    INTEGER,
    content_hash   TEXT NOT NULL,
    branch         TEXT NOT NULL DEFAULT 'main',
    raw_json       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_test_cases_doc ON test_cases(doc_urn);
CREATE INDEX IF NOT EXISTS idx_test_cases_module ON test_cases(module);

-- 需求骨架（S2.2 FR-TD-001）：需求 Markdown 连接器的抽取结果。
-- raw_json 存完整 RequirementParseResult（富结构+缺口+死信摘要），投影列供 SQL 盘点。
-- 重灌幂等：URN = 文档 external_id 的 requirement_doc URN，按 doc 整组替换。
CREATE TABLE IF NOT EXISTS requirement_skeletons (
    urn               TEXT PRIMARY KEY,
    external_id       TEXT NOT NULL,
    content_hash      TEXT NOT NULL,
    branch            TEXT NOT NULL DEFAULT 'main',
    feature_count     INTEGER NOT NULL DEFAULT 0,
    rule_count        INTEGER NOT NULL DEFAULT 0,
    ac_count          INTEGER NOT NULL DEFAULT 0,
    ac_missing_count  INTEGER NOT NULL DEFAULT 0,
    gap_count         INTEGER NOT NULL DEFAULT 0,
    dead_letter_count INTEGER NOT NULL DEFAULT 0,
    linking_source    TEXT NOT NULL DEFAULT 'deterministic',
    raw_json          TEXT NOT NULL,
    indexed_at        TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_requirement_skeletons_external
    ON requirement_skeletons(external_id);

-- 测试点（S2.3 FR-TD-002）：测试点生成 worker 的产物，草稿态数据，S2.5 HITL
-- 批准前不进正式用例库。URN 内容寻址（external_id#fr_id/kind-hash），按 doc_urn
-- 整组替换防幽灵；模型幻觉行不入库（进 dead_letters source=tp-gen）。
CREATE TABLE IF NOT EXISTS test_points (
    urn            TEXT PRIMARY KEY,
    external_id    TEXT NOT NULL,
    doc_urn        TEXT NOT NULL,
    fr_id          TEXT NOT NULL DEFAULT '',
    kind           TEXT NOT NULL,
    priority       TEXT NOT NULL DEFAULT 'P2',
    provenance     TEXT NOT NULL DEFAULT 'deterministic',
    text           TEXT NOT NULL,
    source_refs    TEXT NOT NULL DEFAULT '[]',
    content_hash   TEXT NOT NULL,
    branch         TEXT NOT NULL DEFAULT 'main',
    raw_json       TEXT NOT NULL,
    indexed_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_test_points_doc ON test_points(doc_urn);
CREATE INDEX IF NOT EXISTS idx_test_points_fr ON test_points(fr_id);

-- oracle 挖掘草稿（S2.4 FR-TD-003，schema v11）：四源挖掘的产物，status 恒为
-- pending 草稿，**不回写正式 test_cases.oracles**，S2.5 HITL 审批后才转正（FR-TD-004）。
-- URN 内容寻址（case external_id + type|expectation 哈希），按 case_urn 整案替换防幽灵；
-- 被白名单拒入的模型行进 dead_letters(source=oracle-mine)，不进本表。
-- exploratory=1 表示该用例挖完仍无可执行 oracle（AC3），自动化链路据此排除。
CREATE TABLE IF NOT EXISTS oracle_suggestions (
    urn            TEXT PRIMARY KEY,
    doc_urn        TEXT NOT NULL,
    case_urn       TEXT NOT NULL,
    external_id    TEXT NOT NULL,
    oracle_type    TEXT NOT NULL,
    expectation    TEXT NOT NULL,
    source_kind    TEXT NOT NULL,
    provenance     TEXT NOT NULL DEFAULT 'deterministic',
    source_refs    TEXT NOT NULL DEFAULT '[]',
    confidence     REAL NOT NULL DEFAULT 0.5,
    status         TEXT NOT NULL DEFAULT 'pending',
    exploratory    INTEGER NOT NULL DEFAULT 0,
    content_hash   TEXT NOT NULL,
    branch         TEXT NOT NULL DEFAULT 'main',
    raw_json       TEXT NOT NULL,
    -- S2.5 人审留痕（v12 老库 ALTER 补列）：评审后关联审批记录与评审人/时间/理由，
    -- pending 草稿四列均为 NULL/空；重新挖掘的文档级清扫会整组回收后重建为 pending。
    approval_id    TEXT,
    reviewed_by    TEXT,
    reviewed_at    TEXT,
    decision_reason TEXT NOT NULL DEFAULT '',
    indexed_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_oracle_suggestions_doc ON oracle_suggestions(doc_urn);
CREATE INDEX IF NOT EXISTS idx_oracle_suggestions_case ON oracle_suggestions(case_urn);
CREATE INDEX IF NOT EXISTS idx_oracle_suggestions_status ON oracle_suggestions(status);

-- 人工修改 diff 偏好样本（S2.5 FR-TD-004，schema v12）：仅 reviewer 选择 modify 时
-- 产生，original/revised 双版本 + 字段级 diff 忠实留痕；S2.6 KNO-002 再做 L3 资产
-- 版本化与 few-shot 回流。与 approvals 并列而非并入：审批是动作账本，样本是学习素材。
CREATE TABLE IF NOT EXISTS preference_samples (
    sample_id      TEXT PRIMARY KEY,
    case_urn       TEXT NOT NULL,
    suggestion_urn TEXT NOT NULL,
    reviewer       TEXT NOT NULL,
    reason         TEXT NOT NULL DEFAULT '',
    original_json  TEXT NOT NULL,
    revised_json   TEXT NOT NULL,
    diff_json      TEXT NOT NULL DEFAULT '{}',
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_preference_samples_case ON preference_samples(case_urn);
CREATE INDEX IF NOT EXISTS idx_preference_samples_suggestion ON preference_samples(suggestion_urn);

-- L3 规则资产版本链（S2.6a FR-KNO-002，schema v13）：术语表/prompt/few-shot/golden/
-- 工具白名单统一为「资产 + 不可变版本」。只 INSERT、永不 UPDATE/DELETE；版本号在
-- 资产内单调递增，(asset_id, content_hash) 唯一使同内容重放幂等不增生；diff 不落库，
-- 读时由 core.rule_assets.diff_revisions 对两版 canonical 内容现算（留痕可回放）。
CREATE TABLE IF NOT EXISTS rule_assets (
    asset_id     TEXT NOT NULL,
    version      INTEGER NOT NULL,
    kind         TEXT NOT NULL,
    name         TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    content_json TEXT NOT NULL,
    author       TEXT NOT NULL DEFAULT '',
    note         TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    PRIMARY KEY (asset_id, version),
    UNIQUE (asset_id, content_hash)
);
CREATE INDEX IF NOT EXISTS idx_rule_assets_kind ON rule_assets(kind);

-- 任务队列（offline profile 的 QueuePort 实现）
CREATE TABLE IF NOT EXISTS jobs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    job_type         TEXT NOT NULL,
    payload_json     TEXT NOT NULL,
    idempotency_key  TEXT NOT NULL UNIQUE,
    status           TEXT NOT NULL DEFAULT 'queued',
    attempts         INTEGER NOT NULL DEFAULT 0,
    error            TEXT NOT NULL DEFAULT '',
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    -- v15（S3.1 FR-EX-002）：领任务即写租约到期时间，worker 崩溃后过期 running
    -- 任务可被回收重领；cancel_requested 是协作式取消标记，worker 在认领/执行点检查。
    lease_expires_at TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_jobs_status_type ON jobs(status, job_type);

-- bad case 闭环台账（S2.6c FR-EV-005，v14）：完整模型以 raw_json 为唯一还原来源，
-- 投影列供 SQL 盘点/过滤；四元组快照列对齐 qa_runs/eval_runs 口径（docs/06 §6.1）。
-- UNIQUE(source, source_ref) 与 feedback_id 部分唯一索引双保险保证负反馈幂等。
CREATE TABLE IF NOT EXISTS bad_cases (
    bad_case_id              TEXT PRIMARY KEY,
    source                   TEXT NOT NULL,
    source_ref               TEXT NOT NULL,
    feedback_id              TEXT NOT NULL DEFAULT '',
    status                   TEXT NOT NULL,
    title                    TEXT NOT NULL,
    question                 TEXT NOT NULL DEFAULT '',
    detail                   TEXT NOT NULL DEFAULT '',
    reason                   TEXT NOT NULL DEFAULT '',
    data_version             TEXT NOT NULL DEFAULT '',
    model_version            TEXT NOT NULL DEFAULT '',
    prompt_version           TEXT NOT NULL DEFAULT '',
    params_version           TEXT NOT NULL DEFAULT '',
    triaged_by               TEXT NOT NULL DEFAULT '',
    triaged_at               TEXT,
    decision_note            TEXT NOT NULL DEFAULT '',
    golden_case_id           TEXT NOT NULL DEFAULT '',
    regression_eval_run_id   TEXT NOT NULL DEFAULT '',
    created_at               TEXT NOT NULL,
    updated_at               TEXT NOT NULL,
    closed_at                TEXT,
    history_json             TEXT NOT NULL DEFAULT '[]',
    raw_json                 TEXT NOT NULL,
    UNIQUE(source, source_ref)
);
CREATE INDEX IF NOT EXISTS idx_bad_cases_status ON bad_cases(status);
CREATE INDEX IF NOT EXISTS idx_bad_cases_source ON bad_cases(source);
-- 仅 qa_feedback 行有 feedback_id：部分唯一索引允许其余源多行空串共存
CREATE UNIQUE INDEX IF NOT EXISTS idx_bad_cases_feedback
    ON bad_cases(feedback_id) WHERE feedback_id <> '';

-- v15（S3.1 FR-EX-002）：测试执行账本。先持久化账本再 enqueue（外部副作用前先记账，
-- 崩溃只留可幂等重放的幻影记录）。raw_json 是 TestRun 的唯一还原来源，投影列供
-- SQL 盘点/过滤；UNIQUE(idempotency_key) 保证同请求重放返回同一 run_id。
CREATE TABLE IF NOT EXISTS test_runs (
    run_id           TEXT PRIMARY KEY,
    idempotency_key  TEXT NOT NULL UNIQUE,
    project          TEXT NOT NULL,
    commit_sha       TEXT NOT NULL,
    status           TEXT NOT NULL,
    request_json     TEXT NOT NULL,
    raw_json         TEXT NOT NULL DEFAULT '',
    error            TEXT NOT NULL DEFAULT '',
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    started_at       TEXT,
    finished_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_test_runs_status ON test_runs(status);
CREATE INDEX IF NOT EXISTS idx_test_runs_project ON test_runs(project);

-- 每日重跑配额账本（FR-EX-002 AC2：超日配额的重跑拒绝并记账）。request_id UNIQUE
-- 保证同一请求重放不重复扣减；(budget_key, budget_date) 支撑当日槽位计数。
CREATE TABLE IF NOT EXISTS rerun_budget (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    budget_key   TEXT NOT NULL,
    budget_date  TEXT NOT NULL,
    request_id   TEXT NOT NULL UNIQUE,
    run_id       TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rerun_budget_key_date
    ON rerun_budget(budget_key, budget_date);
