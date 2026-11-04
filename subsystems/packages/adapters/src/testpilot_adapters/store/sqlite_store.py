"""SQLite/FTS5 存储实现。"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Any, Sequence

from testpilot_core import SCHEMA_VERSION
from testpilot_core.enums import OutboxStatus
from testpilot_core.models import (
    Approval,
    Entity,
    EventEnvelope,
    EvalRun,
    Feedback,
    KnowledgeChunk,
    OutboxRecord,
    Relation,
    SourceDocument,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# 老库平滑升级：表已存在时 CREATE IF NOT EXISTS 不会加列，按 PRAGMA 检查后 ALTER。
_COLUMN_MIGRATIONS = {
    "entities": {"doc_urn": "TEXT"},
    "relations": {"doc_urn": "TEXT"},
    # v3 向量嵌入阶段（FR-ING-001：每步留痕可重跑）
    "ingestion_status": {
        "embed_stage": "TEXT NOT NULL DEFAULT ''",
        "embed_model": "TEXT NOT NULL DEFAULT ''",
        "embed_dim": "INTEGER NOT NULL DEFAULT 0",
        "embed_latency_ms": "INTEGER NOT NULL DEFAULT 0",
        "embed_error": "TEXT NOT NULL DEFAULT ''",
        "embedded_at": "TEXT",
    },
}


def _migrate_columns(conn: sqlite3.Connection) -> None:
    for table, columns in _COLUMN_MIGRATIONS.items():
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for name, decl in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def _migrate_fts_to_trigram(conn: sqlite3.Connection) -> None:
    """v2→v3：FTS5 不支持原地换 tokenizer。老库的 unicode61 表 DROP 后按
    trigram 重建并从 chunks 回填（连续汉字在 unicode61 下整段成词，自然语言
    检索必 0 命中，见 handoff S1.1.2 取证）。"""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='chunks_fts'"
    ).fetchone()
    if row and row["sql"] and "trigram" not in row["sql"]:
        conn.execute("DROP TABLE chunks_fts")
        conn.execute(
            "CREATE VIRTUAL TABLE chunks_fts USING fts5("
            "text, chunk_urn UNINDEXED, tokenize='trigram')"
        )
        conn.execute("INSERT INTO chunks_fts(text, chunk_urn) SELECT text, urn FROM chunks")


def bootstrap_store(data_dir: str | Path) -> "Store":
    """在数据目录下初始化 store/testpilot.sqlite，幂等；老库自动平滑升级。"""
    data_dir = Path(data_dir)
    db_path = data_dir / "store" / "testpilot.sqlite"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    schema_sql = resources.files("testpilot_adapters.store").joinpath("schema.sql").read_text(encoding="utf-8")
    conn.executescript(schema_sql)
    _migrate_columns(conn)
    _migrate_fts_to_trigram(conn)
    conn.execute(
        """INSERT INTO meta(key,value) VALUES('schema_version',?)
           ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()
    return Store(conn)


class Store:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def close(self) -> None:
        self.conn.close()

    # ── 元信息 ─────────────────────────────────────────────────────────
    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    # ── 文档/切块 + FTS5 ────────────────────────────────────────────────
    def upsert_document(self, doc: SourceDocument) -> None:
        self.conn.execute(
            """INSERT INTO source_documents
               (urn, source_system, uri, title, version, content_hash,
                authority_level, acl_scope, branch, mtime, raw_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(urn) DO UPDATE SET
                 version=excluded.version, content_hash=excluded.content_hash,
                 mtime=excluded.mtime, raw_json=excluded.raw_json""",
            (
                doc.urn, doc.source_system, doc.uri, doc.title, doc.version, doc.content_hash,
                str(doc.authority_level), doc.acl_scope, doc.branch,
                doc.mtime.isoformat() if doc.mtime else None,
                doc.model_dump_json(by_alias=True),
            ),
        )
        self.conn.commit()

    def upsert_chunk(self, chunk: KnowledgeChunk) -> None:
        self.conn.execute("DELETE FROM chunks_fts WHERE chunk_urn=?", (chunk.urn,))
        self.conn.execute(
            """INSERT INTO chunks
               (urn, doc_urn, idx, kind, text, symbol_urn, quality_score,
                branch, valid_from, valid_to, stale, content_hash, raw_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(urn) DO UPDATE SET
                 text=excluded.text, stale=excluded.stale, content_hash=excluded.content_hash""",
            (
                chunk.urn, chunk.doc_urn, chunk.index, chunk.kind, chunk.text, chunk.symbol_urn,
                chunk.quality_score, chunk.branch, chunk.valid_from, chunk.valid_to,
                str(chunk.stale), chunk.content_hash, chunk.model_dump_json(by_alias=True),
            ),
        )
        self.conn.execute("INSERT INTO chunks_fts(text, chunk_urn) VALUES(?,?)", (chunk.text, chunk.urn))
        self.conn.commit()

    def search_fts(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        if not query.strip():
            return []
        rows = self.conn.execute(
            """SELECT c.urn, c.doc_urn, c.kind, c.stale
               FROM chunks_fts f JOIN chunks c ON c.urn = f.chunk_urn
               WHERE chunks_fts MATCH ? ORDER BY rank LIMIT ?""",
            (query, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def _like_escape(term: str) -> str:
        return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    def search_fts_matches(
        self,
        match_expr: str,
        like_terms: Sequence[str] = (),
        *,
        branch: str = "main",
        acl_scope: str = "default",
        kinds: Sequence[str] = (),
        include_stale: bool = False,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """trigram FTS 布尔查询 + 短词 LIKE 兜底（retrieval.shape_trigram_query 产物）。

        返回 [{urn, score}]，score=-bm25（越大越相关）。match_expr 与 like_terms
        之间 OR；project/分支/密级/类型过滤在 SQL 内完成。输入已由纯函数整形，
        LIKE 词只含字母/数字/下划线/CJK，仍做 % _ 转义。
        """
        # FTS5 下 bm25() 只能用在"带 MATCH 的那条 SELECT 本身"：与 LIKE 在
        # WHERE 里 OR 并列报 "unable to use function MATCH..."；包进子查询
        # 再聚合，trigram 分词器命中首行即报 "unable to use function bm25..."
        # （均已实测）。故两条路径分别查询，Python 合并去重：MATCH 命中按
        # bm25 排序在前，LIKE-only 命中置后——融合阶段只消费排名。
        filter_sql = ["AND c.branch = ?", "AND d.acl_scope = ?"]
        filter_params: list[Any] = [branch, acl_scope]
        if kinds:
            filter_sql.append(f"AND c.kind IN ({','.join('?' for _ in kinds)})")
            filter_params.extend(kinds)
        if not include_stale:
            filter_sql.append("AND c.stale = 'fresh'")
        filters = "\n".join(filter_sql)

        merged: dict[str, float] = {}
        order: list[str] = []
        if match_expr:
            sql = (
                "SELECT c.urn AS urn, -bm25(chunks_fts) AS score\n"
                "FROM chunks_fts JOIN chunks c ON c.urn = chunks_fts.chunk_urn\n"
                "JOIN source_documents d ON d.urn = c.doc_urn\n"
                "WHERE chunks_fts MATCH ?\n" + filters + "\n"
                "ORDER BY score DESC, c.doc_urn, c.idx LIMIT ?"
            )
            for r in self.conn.execute(
                sql, [match_expr, *filter_params, limit]
            ).fetchall():
                if r["urn"] not in merged:
                    merged[r["urn"]] = r["score"]
                    order.append(r["urn"])
        if like_terms and len(order) < limit:
            like_sql = " OR ".join("c.text LIKE ? ESCAPE '\\'" for _ in like_terms)
            sql = (
                "SELECT c.urn AS urn\n"
                "FROM chunks c JOIN source_documents d ON d.urn = c.doc_urn\n"
                f"WHERE ({like_sql})\n" + filters + "\n"
                "ORDER BY c.doc_urn, c.idx LIMIT ?"
            )
            params = [
                f"%{self._like_escape(t)}%" for t in like_terms
            ] + filter_params + [limit]
            for r in self.conn.execute(sql, params).fetchall():
                if r["urn"] not in merged:
                    merged[r["urn"]] = 0.0
                    order.append(r["urn"])
        return [{"urn": urn, "score": merged[urn]} for urn in order[:limit]]

    def load_retrieval_chunks(self, urns: Sequence[str]) -> list[dict[str, Any]]:
        """按 URN 批量取切块与所属文档元信息（KNO-001 AC2：URI + content_hash）。

        返回顺序与传入 urns 一致（融合排序在调用侧）；缺失 URN 跳过。
        """
        if not urns:
            return []
        placeholders = ",".join("?" for _ in urns)
        rows = self.conn.execute(
            f"""SELECT c.raw_json AS chunk_json, c.quality_score AS quality_score,
                       d.uri AS doc_uri, d.mtime AS doc_mtime,
                       d.authority_level AS doc_authority,
                       d.content_hash AS doc_content_hash,
                       d.acl_scope AS acl_scope
                FROM chunks c JOIN source_documents d ON d.urn = c.doc_urn
                WHERE c.urn IN ({placeholders})""",
            list(urns),
        ).fetchall()
        by_urn = {}
        for r in rows:
            chunk = json.loads(r["chunk_json"])
            chunk["doc_uri"] = r["doc_uri"]
            chunk["doc_mtime"] = r["doc_mtime"]
            chunk["doc_authority"] = r["doc_authority"]
            chunk["doc_content_hash"] = r["doc_content_hash"]
            by_urn[chunk["urn"]] = chunk
        return [by_urn[u] for u in urns if u in by_urn]

    def record_retrieval_event(
        self,
        *,
        query: str,
        query_sha: str,
        project: str,
        branch: str,
        acl_scope: str,
        kinds: Sequence[str],
        model_version: str,
        fts_available: bool,
        vector_available: bool,
        degraded: bool,
        fts_urns: Sequence[str],
        vector_urns: Sequence[str],
        fused_urns: Sequence[str],
        fts_latency_ms: int,
        vector_latency_ms: int,
        extra: dict[str, Any] | None = None,
    ) -> int:
        cur = self.conn.execute(
            """INSERT INTO retrieval_events
               (ts, query, query_sha, project, branch, acl_scope, kinds, model_version,
                fts_available, vector_available, degraded, fts_urns, vector_urns,
                fused_urns, fts_latency_ms, vector_latency_ms, raw_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                _now_iso(), query, query_sha, project, branch, acl_scope,
                ",".join(kinds), model_version,
                int(fts_available), int(vector_available), int(degraded),
                json.dumps(list(fts_urns), ensure_ascii=False),
                json.dumps(list(vector_urns), ensure_ascii=False),
                json.dumps(list(fused_urns), ensure_ascii=False),
                fts_latency_ms, vector_latency_ms,
                json.dumps(extra or {}, ensure_ascii=False),
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    # ── 入库管线（schema v2，FR-ING-001）────────────────────────────────
    def list_documents(self, source_system: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT urn, content_hash FROM source_documents WHERE source_system=?",
            (source_system,),
        ).fetchall()

    def get_document(self, urn: str) -> SourceDocument | None:
        row = self.conn.execute(
            "SELECT raw_json FROM source_documents WHERE urn=?", (urn,)
        ).fetchone()
        return SourceDocument.model_validate(json.loads(row["raw_json"])) if row else None

    def replace_document_index(
        self,
        doc: SourceDocument,
        chunks: list[KnowledgeChunk],
        entities: list[Entity],
        relations: list[tuple[Relation, list[str]]],
        run_id: str,
    ) -> None:
        """单事务：登记文档 → 清旧切块/FTS/实体关系 → 写新版。重跑同输入结果不变。"""
        cur = self.conn
        try:
            cur.execute("BEGIN")
            cur.execute(
                """INSERT INTO source_documents
                   (urn, source_system, uri, title, version, content_hash,
                    authority_level, acl_scope, branch, mtime, raw_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(urn) DO UPDATE SET
                     version=excluded.version, content_hash=excluded.content_hash,
                     authority_level=excluded.authority_level,
                     mtime=excluded.mtime, raw_json=excluded.raw_json""",
                (
                    doc.urn, doc.source_system, doc.uri, doc.title, doc.version,
                    doc.content_hash, str(doc.authority_level), doc.acl_scope, doc.branch,
                    doc.mtime.isoformat() if doc.mtime else None,
                    doc.model_dump_json(by_alias=True),
                ),
            )
            cur.execute(
                "DELETE FROM chunks_fts WHERE chunk_urn IN (SELECT urn FROM chunks WHERE doc_urn=?)",
                (doc.urn,),
            )
            cur.execute("DELETE FROM chunks WHERE doc_urn=?", (doc.urn,))
            cur.execute("DELETE FROM relations WHERE doc_urn=?", (doc.urn,))
            cur.execute("DELETE FROM entities WHERE doc_urn=?", (doc.urn,))

            for chunk in chunks:
                cur.execute(
                    """INSERT INTO chunks
                       (urn, doc_urn, idx, kind, text, symbol_urn, quality_score,
                        branch, valid_from, valid_to, stale, content_hash, raw_json)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        chunk.urn, chunk.doc_urn, chunk.index, chunk.kind, chunk.text,
                        chunk.symbol_urn, chunk.quality_score, chunk.branch,
                        chunk.valid_from, chunk.valid_to, str(chunk.stale),
                        chunk.content_hash, chunk.model_dump_json(by_alias=True),
                    ),
                )
                cur.execute(
                    "INSERT INTO chunks_fts(text, chunk_urn) VALUES(?,?)",
                    (chunk.text, chunk.urn),
                )
            for entity in entities:
                cur.execute(
                    """INSERT INTO entities(urn, kind, name, branch, doc_urn, raw_json)
                       VALUES (?,?,?,?,?,?)
                       ON CONFLICT(urn) DO UPDATE SET
                         kind=excluded.kind, name=excluded.name, raw_json=excluded.raw_json""",
                    (entity.urn, entity.kind, entity.name, entity.branch, doc.urn,
                     entity.model_dump_json(by_alias=True)),
                )
            for relation, evidence_lines in relations:
                cur.execute(
                    """INSERT INTO relations
                       (urn, source_urn, target_urn, kind, confidence, evidence_json, doc_urn)
                       VALUES (?,?,?,?,?,?,?)
                       ON CONFLICT(urn) DO UPDATE SET
                         confidence=excluded.confidence, evidence_json=excluded.evidence_json""",
                    (
                        relation.urn, relation.source_urn, relation.target_urn, relation.kind,
                        relation.confidence,
                        json.dumps({"lines": evidence_lines, "urns": relation.evidence_urns},
                                   ensure_ascii=False),
                        doc.urn,
                    ),
                )
            avg = round(sum(c.quality_score for c in chunks) / len(chunks), 4) if chunks else 0.0
            cur.execute(
                """INSERT INTO ingestion_status
                   (doc_urn, source_system, content_hash, stage, chunk_count, avg_quality,
                    last_run_id, error, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(doc_urn) DO UPDATE SET
                     content_hash=excluded.content_hash, stage='indexed',
                     chunk_count=excluded.chunk_count, avg_quality=excluded.avg_quality,
                     last_run_id=excluded.last_run_id, error='',
                     -- 内容变了：旧向量作废，等待嵌入阶段重建
                     embed_stage='', embed_error='',
                     updated_at=excluded.updated_at""",
                (doc.urn, doc.source_system, doc.content_hash, "indexed", len(chunks), avg,
                 run_id, "", _now_iso()),
            )
            cur.commit()
        except Exception:
            cur.rollback()
            raise

    def delete_document_cascade(self, urn: str) -> bool:
        """删除文档及其全部切块/FTS/实体/关系/加工状态。返回是否命中。"""
        if self.conn.execute("SELECT 1 FROM source_documents WHERE urn=?", (urn,)).fetchone() is None:
            return False
        cur = self.conn
        cur.execute(
            "DELETE FROM chunks_fts WHERE chunk_urn IN (SELECT urn FROM chunks WHERE doc_urn=?)",
            (urn,),
        )
        cur.execute("DELETE FROM chunks WHERE doc_urn=?", (urn,))
        cur.execute("DELETE FROM relations WHERE doc_urn=?", (urn,))
        cur.execute("DELETE FROM entities WHERE doc_urn=?", (urn,))
        cur.execute("DELETE FROM ingestion_status WHERE doc_urn=?", (urn,))
        cur.execute("DELETE FROM source_documents WHERE urn=?", (urn,))
        cur.commit()
        return True

    def mark_document_failed(
        self, doc_urn: str, source_system: str, content_hash: str, run_id: str, error: str
    ) -> None:
        self.conn.execute(
            """INSERT INTO ingestion_status
               (doc_urn, source_system, content_hash, stage, chunk_count, avg_quality,
                last_run_id, error, updated_at)
               VALUES (?,?,?,?,0,0,?,?,?)
               ON CONFLICT(doc_urn) DO UPDATE SET
                 stage='failed', error=excluded.error, last_run_id=excluded.last_run_id,
                 updated_at=excluded.updated_at""",
            (doc_urn, source_system, content_hash, "failed", run_id, error[:2000], _now_iso()),
        )
        self.conn.commit()

    # ── 向量嵌入阶段（v3）──────────────────────────────────────────────
    def list_chunk_urns(self, doc_urn: str) -> list[str]:
        """文档当前全部切块 URN（删文档/更新文档前取旧集合做向量孤儿清理）。"""
        return [
            r["urn"]
            for r in self.conn.execute(
                "SELECT urn FROM chunks WHERE doc_urn=? ORDER BY idx", (doc_urn,)
            ).fetchall()
        ]

    def list_doc_chunks_for_embed(self, doc_urn: str) -> list[dict[str, Any]]:
        """取文档切块 + 向量 metadata 所需的文档字段（只取 fresh）。"""
        rows = self.conn.execute(
            """SELECT c.urn AS urn, c.doc_urn AS doc_urn, c.kind AS kind,
                      c.text AS text, c.quality_score AS quality_score,
                      c.branch AS branch, c.content_hash AS content_hash,
                      d.acl_scope AS acl_scope, d.uri AS doc_uri,
                      d.content_hash AS doc_content_hash
               FROM chunks c JOIN source_documents d ON d.urn = c.doc_urn
               WHERE c.doc_urn=? AND c.stale='fresh' ORDER BY c.idx""",
            (doc_urn,),
        ).fetchall()
        return [dict(r) for r in rows]

    def list_docs_pending_embed(self, source_system: str) -> list[str]:
        """内容已 indexed 但向量未就绪的文档（历史失败重试 / 向量能力新启用时的补偿）。"""
        return [
            r["doc_urn"]
            for r in self.conn.execute(
                """SELECT doc_urn FROM ingestion_status
                   WHERE source_system=? AND stage='indexed'
                     AND embed_stage IS DISTINCT FROM 'embedded'""",
                (source_system,),
            ).fetchall()
        ]

    def mark_document_embedded(
        self, doc_urn: str, *, model: str, dim: int, latency_ms: int
    ) -> None:
        doc = self.conn.execute(
            "SELECT source_system, content_hash FROM source_documents WHERE urn=?",
            (doc_urn,),
        ).fetchone()
        if doc is None:
            return  # 文档已删（本轮处理中被对账删除的竞态）
        self.conn.execute(
            """INSERT INTO ingestion_status
                 (doc_urn, source_system, content_hash, stage, last_run_id,
                  embed_stage, embed_model, embed_dim, embed_latency_ms,
                  embed_error, embedded_at, updated_at)
               VALUES (?,?,?, 'indexed', '', 'embedded', ?, ?, ?, '', ?, ?)
               ON CONFLICT(doc_urn) DO UPDATE SET
                 embed_stage='embedded', embed_model=excluded.embed_model,
                 embed_dim=excluded.embed_dim,
                 embed_latency_ms=excluded.embed_latency_ms,
                 embed_error='', embedded_at=excluded.embedded_at,
                 updated_at=excluded.updated_at""",
            (doc_urn, doc["source_system"], doc["content_hash"], model, dim,
             latency_ms, _now_iso(), _now_iso()),
        )
        self.conn.commit()

    def mark_document_embed_failed(
        self, doc_urn: str, run_id: str, error: str
    ) -> None:
        # 走到嵌入阶段说明内容加工已成功，ingestion_status 行必然存在
        self.conn.execute(
            """UPDATE ingestion_status SET embed_stage='failed', embed_error=?,
                 last_run_id=?, updated_at=? WHERE doc_urn=?""",
            (error[:2000], run_id, _now_iso(), doc_urn),
        )
        self.conn.commit()

    def start_ingestion_run(self, run_id: str, source_system: str, trigger: str) -> None:
        self.conn.execute(
            """INSERT INTO ingestion_runs(run_id, source_system, trigger, status, started_at)
               VALUES (?,?,?, 'running', ?)""",
            (run_id, source_system, trigger, _now_iso()),
        )
        self.conn.commit()

    def finish_ingestion_run(
        self, run_id: str, status: str, counts: dict[str, int], report: dict[str, Any]
    ) -> None:
        self.conn.execute(
            """UPDATE ingestion_runs SET status=?, docs_seen=?, docs_created=?, docs_updated=?,
                 docs_deleted=?, docs_unchanged=?, docs_failed=?, chunks_total=?,
                 report_json=?, finished_at=? WHERE run_id=?""",
            (
                status, counts.get("seen", 0), counts.get("created", 0),
                counts.get("updated", 0), counts.get("deleted", 0),
                counts.get("unchanged", 0), counts.get("failed", 0),
                counts.get("chunks_total", 0),
                json.dumps(report, ensure_ascii=False), _now_iso(), run_id,
            ),
        )
        self.conn.commit()

    # ── 事件（幂等去重 + 死信）─────────────────────────────────────────
    def record_event_if_new(self, event: EventEnvelope) -> bool:
        """返回 True=新事件；False=重复投递（docs/08 FR-CON-005 AC1）。

        用 INSERT OR IGNORE：重复 event_id 时不能让隐式事务挂着，否则后续
        显式 BEGIN 报 "cannot start a transaction within a transaction"。
        """
        cur = self.conn.execute(
            """INSERT OR IGNORE INTO events
               (event_id, event_type, source_system, status, payload_json, occurred_at, received_at)
               VALUES (?,?,?,?,?,?,?)""",
            (
                event.event_id, event.event_type, event.source_system, "received",
                event.model_dump_json(), event.occurred_at.isoformat(), _now_iso(),
            ),
        )
        self.conn.commit()
        return cur.rowcount == 1

    def add_dead_letter(self, source: str, payload: dict[str, Any], error: str) -> None:
        self.conn.execute(
            "INSERT INTO dead_letters(source, payload_json, error, created_at) VALUES(?,?,?,?)",
            (source, json.dumps(payload, ensure_ascii=False), error, _now_iso()),
        )
        self.conn.commit()

    # ── 审批 ───────────────────────────────────────────────────────────
    def save_approval(self, approval: Approval) -> None:
        self.conn.execute(
            """INSERT INTO approvals
               (approval_id, action_level, target_system, request_summary, request_diff,
                decision, reviewer, reason, based_on, created_at, decided_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(approval_id) DO UPDATE SET
                 decision=excluded.decision, reviewer=excluded.reviewer,
                 reason=excluded.reason, decided_at=excluded.decided_at""",
            (
                approval.approval_id, str(approval.action_level), approval.target_system,
                approval.request_summary, json.dumps(approval.request_diff, ensure_ascii=False),
                str(approval.decision), approval.reviewer, approval.reason,
                json.dumps(approval.based_on_versions, ensure_ascii=False),
                approval.created_at.isoformat(),
                approval.decided_at.isoformat() if approval.decided_at else None,
            ),
        )
        self.conn.commit()

    def get_approval(self, approval_id: str) -> Approval | None:
        row = self.conn.execute(
            "SELECT * FROM approvals WHERE approval_id=?", (approval_id,)
        ).fetchone()
        if not row:
            return None
        return Approval(
            approval_id=row["approval_id"],
            action_level=row["action_level"],
            target_system=row["target_system"],
            request_summary=row["request_summary"],
            request_diff=json.loads(row["request_diff"]),
            decision=row["decision"],
            reviewer=row["reviewer"],
            reason=row["reason"],
            based_on_versions=json.loads(row["based_on"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            decided_at=datetime.fromisoformat(row["decided_at"]) if row["decided_at"] else None,
        )

    # ── Outbox（先账后事）──────────────────────────────────────────────
    def add_outbox(self, record: OutboxRecord) -> int:
        cur = self.conn.execute(
            """INSERT INTO outbox
               (idempotency_key, action_level, target_system, request_body, status,
                external_ref, approval_id, attempts, rollback_note, error, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(idempotency_key) DO NOTHING""",
            (
                record.idempotency_key, str(record.action_level), record.target_system,
                json.dumps(record.request_body, ensure_ascii=False), str(record.status),
                record.external_ref, record.approval_id, record.attempts,
                record.rollback_note, record.error,
                record.created_at.isoformat(), _now_iso(),
            ),
        )
        self.conn.commit()
        return cur.lastrowid or 0

    def list_pending_outbox(self) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM outbox WHERE status IN (?,?) ORDER BY id",
                (str(OutboxStatus.PENDING), str(OutboxStatus.FAILED)),
            ).fetchall()
        )

    def mark_outbox(self, idempotency_key: str, status: OutboxStatus, *, external_ref: str | None = None,
                    error: str = "") -> None:
        self.conn.execute(
            """UPDATE outbox SET status=?, external_ref=COALESCE(?, external_ref),
               attempts=attempts+1, error=?, updated_at=? WHERE idempotency_key=?""",
            (str(status), external_ref, error, _now_iso(), idempotency_key),
        )
        self.conn.commit()

    # ── 反馈 / 评测 ────────────────────────────────────────────────────
    def add_feedback(self, feedback: Feedback) -> int:
        cur = self.conn.execute(
            """INSERT INTO feedback
               (run_id, target_urn, kind, reason, detail, model_version, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (
                feedback.run_id, feedback.target_urn, str(feedback.kind),
                str(feedback.reason) if feedback.reason else None, feedback.detail,
                feedback.model_version, feedback.created_at.isoformat(),
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid or 0)

    def add_eval_run(self, run: EvalRun) -> None:
        self.conn.execute(
            """INSERT INTO eval_runs
               (eval_run_id, data_version, model_version, prompt_version, params_version,
                degraded_path, metrics, report_uri, created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                run.eval_run_id, run.version.data_version, run.version.model_version,
                run.version.prompt_version, run.version.params_version,
                int(run.version.degraded_path), json.dumps(run.metrics), run.report_uri,
                run.created_at.isoformat(),
            ),
        )
        self.conn.commit()
