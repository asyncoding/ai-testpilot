-- TestPilot schema（当前 schema_version = 4；docs/08 FR-CFG-003 AC2：迁移脚本版本化）
-- v1：S0 骨架；v2：入库批次/文档加工状态、entities/relations 挂 doc_urn（S1.1）
-- v3：chunks_fts 换 trigram 分词（CJK 子串召回，S1.2；老库由 _migrate_fts_to_trigram
--     DROP+回填）、retrieval_events 各检索阶段命中率留痕（FR-KNO-001 AC1）
-- v4：verification_events live-verify 每次校验留痕（S1.3，FR-QA-002 AC2：代码类问题
--     100% 有 trace 可查的校验调用，含降级；CREATE IF NOT EXISTS 对老库幂等，无列迁移）

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
    updated_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_status_type ON jobs(status, job_type);
