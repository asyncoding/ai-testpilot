-- TestPilot schema（当前 schema_version = 2；docs/08 FR-CFG-003 AC2：迁移脚本版本化）
-- v1：S0 骨架；v2：入库批次/文档加工状态、entities/relations 挂 doc_urn（S1.1）

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

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text,
    chunk_urn UNINDEXED,
    tokenize = 'unicode61'
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
    stage         TEXT NOT NULL,          -- indexed / failed
    chunk_count   INTEGER NOT NULL DEFAULT 0,
    avg_quality   REAL NOT NULL DEFAULT 0,
    last_run_id   TEXT NOT NULL,
    error         TEXT NOT NULL DEFAULT '',
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ingestion_status_stage ON ingestion_status(stage);

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
