"""SQLite/FTS5 存储实现。"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Any, Sequence

from testpilot_core import SCHEMA_VERSION
from testpilot_core.approval import CaseDecisionPlan
from testpilot_core.badcase import BadCase
from testpilot_core.case_relations import (
    RELATION_SCOPE_URN,
    CaseRelation,
    EvidenceRow,
    ensure_relation_scope,
)
from testpilot_core.enums import OutboxStatus
from testpilot_core.oracle_mining import MinedOracle, suggestion_urn
from testpilot_core.requirement import RequirementParseResult
from testpilot_core.rule_assets import RuleAssetRevision
from testpilot_core.testpoint import acceptance_urn, point_urn
from testpilot_core.models import (
    Approval,
    Entity,
    EventEnvelope,
    EvalCase,
    EvalRun,
    Feedback,
    KnowledgeChunk,
    OutboxRecord,
    PreferenceSample,
    Relation,
    SourceDocument,
    TestCase,
    TestPoint,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# 老库平滑升级：表已存在时 CREATE IF NOT EXISTS 不会加列，按 PRAGMA 检查后 ALTER。
_COLUMN_MIGRATIONS = {
    "entities": {"doc_urn": "TEXT"},
    "relations": {"doc_urn": "TEXT"},
    # v6：qa_runs 持久化引用校验具体错误（老库 ALTER 补列，默认空列表）
    # v7：qa_runs 持久化意图来源（regex_bypass/model/fallback，老库补空串表 legacy）
    "qa_runs": {
        "citation_errors": "TEXT NOT NULL DEFAULT '[]'",
        "intent_source": "TEXT NOT NULL DEFAULT ''",
    },
    # v3 向量嵌入阶段（FR-ING-001：每步留痕可重跑）
    "ingestion_status": {
        "embed_stage": "TEXT NOT NULL DEFAULT ''",
        "embed_model": "TEXT NOT NULL DEFAULT ''",
        "embed_dim": "INTEGER NOT NULL DEFAULT 0",
        "embed_latency_ms": "INTEGER NOT NULL DEFAULT 0",
        "embed_error": "TEXT NOT NULL DEFAULT ''",
        "embedded_at": "TEXT",
    },
    # v12（S2.5 FR-TD-004）：oracle 草稿人审留痕四列，老库 ALTER 补列，
    # pending 草稿默认 NULL/空串，与新库 CREATE TABLE 默认值对齐
    "oracle_suggestions": {
        "approval_id": "TEXT",
        "reviewed_by": "TEXT",
        "reviewed_at": "TEXT",
        "decision_reason": "TEXT NOT NULL DEFAULT ''",
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

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            """INSERT INTO meta(key,value) VALUES(?,?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
            (key, value),
        )
        self.conn.commit()

    # ── 文档/切块 + FTS5 ────────────────────────────────────────────────
    def upsert_document(self, doc: SourceDocument) -> None:
        self.conn.execute(
            """INSERT INTO source_documents
               (urn, source_system, uri, title, version, content_hash,
                authority_level, acl_scope, branch, mtime, raw_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(urn) DO UPDATE SET
                 version=excluded.version, content_hash=excluded.content_hash,
                 mtime=excluded.mtime, raw_json=excluded.raw_json,
                 -- S1.3：branch 是可随装配/重灌变化的归属字段（旧库曾全为 main），
                 -- 冲突时必须随新值覆盖，否则 live_verify 上下文取到陈旧分支名
                 branch=excluded.branch""",
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
                 doc_urn=excluded.doc_urn, idx=excluded.idx, kind=excluded.kind,
                 text=excluded.text, symbol_urn=excluded.symbol_urn,
                 quality_score=excluded.quality_score, branch=excluded.branch,
                 valid_from=excluded.valid_from, valid_to=excluded.valid_to,
                 stale=excluded.stale, content_hash=excluded.content_hash,
                 raw_json=excluded.raw_json""",
            # S1.4：同 URN 重灌必须整行替换。旧子句只刷 text/stale/hash，
            # 而 FtsSearchAdapter 命中后靠 raw_json 重建 KnowledgeChunk——
            # FTS 表已是新文本、raw_json 还是旧块，检索到的证据与库内文本
            # 自相矛盾（stale-index E2E 对照实验变红复现）。valid_from/branch
            # 等归属位同理，与 S1.3 upsert_document 的分支覆盖修复同一口径。
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

    def lookup_symbol_chunks(self, name: str, branch: str) -> list[str]:
        """P3 精确符号路由：entities.name → chunks.symbol_urn → 代码切块 URN。

        只走精确等值（不做 LIKE/模糊），branch 严格隔离。返回 0 个=符号表无此名
        （调用方回退常规混合检索）；>1 个=跨文件重名/重载（调用方放弃置顶，避免
        在多个候选间替用户做选择）；恰好 1 个才允许确定性置顶。
        """
        rows = self.conn.execute(
            """
            SELECT DISTINCT c.urn AS urn
            FROM entities e
            JOIN chunks c
              ON c.symbol_urn = e.urn AND c.branch = e.branch
            WHERE e.kind = 'code_symbol'
              AND c.kind = 'code_symbol'
              AND e.name = ? AND e.branch = ?
            ORDER BY c.urn
            """,
            (name, branch),
        ).fetchall()
        return [r["urn"] for r in rows]

    def get_verification_context(self, urn: str) -> dict[str, Any] | None:
        """live-verify 取索引侧证据（S1.3 FR-QA-002）：chunk URN 或 symbol URN
        都能命中（symbol_urn 列）；返回切块 content_hash/valid_from/chunker 版本
        与所属文档的 file:// URI、分支。无命中返回 None（调用方按索引缺失降级）。"""
        row = self.conn.execute(
            """SELECT c.kind AS kind, c.content_hash AS content_hash,
                      c.valid_from AS valid_from, c.branch AS branch,
                      c.raw_json AS chunk_json, d.uri AS doc_uri
               FROM chunks c JOIN source_documents d ON d.urn = c.doc_urn
               WHERE c.urn = ? OR c.symbol_urn = ?
               LIMIT 1""",
            (urn, urn),
        ).fetchone()
        if row is None:
            return None
        chunk = json.loads(row["chunk_json"])
        return {
            "kind": row["kind"],
            "content_hash": row["content_hash"],
            "valid_from": row["valid_from"],
            "branch": row["branch"],
            "doc_uri": row["doc_uri"],
            "chunker_version": (chunk.get("metadata") or {}).get("chunker_version"),
        }

    def record_verification_event(self, *, result: dict[str, Any]) -> int:
        """live-verify 每次调用必写一行（含降级/失败），FR-QA-002 AC2 的 trace 可查。

        result 为 LiveVerifyResult.model_dump()；exists/body_changed 可空（降级无事实）。
        """
        cur = self.conn.execute(
            """INSERT INTO verification_events
               (ts, urn, kind, project, branch, requested_commit, head_commit,
                symbol_exists, verified, body_changed, degraded, reason, error,
                indexed_commit, indexed_chunker_version, current_chunker_version,
                latency_ms, raw_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                _now_iso(), result.get("urn", ""), result.get("kind", ""),
                result.get("project", ""), result.get("branch", ""),
                result.get("requested_commit", ""), result.get("head_commit", ""),
                None if result.get("exists") is None else int(bool(result["exists"])),
                int(bool(result.get("verified"))),
                None if result.get("body_changed") is None
                else int(bool(result["body_changed"])),
                int(bool(result.get("degraded"))), result.get("reason", ""),
                result.get("error", ""),
                result.get("indexed_commit"), result.get("indexed_chunker_version"),
                result.get("current_chunker_version", ""),
                int(result.get("latency_ms", 0) or 0),
                json.dumps(result.get("raw") or {}, ensure_ascii=False),
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

    def all_document_urns(self) -> list[str]:
        """S1.6 泄漏检查（FR-EV-001）：holdout 文档 marker 要对全量已索引 URN 比对。"""
        rows = self.conn.execute("SELECT urn FROM source_documents").fetchall()
        return [r["urn"] for r in rows]

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
                     mtime=excluded.mtime, raw_json=excluded.raw_json,
                     -- S1.3：重灌/换分支时归属字段必须随新值覆盖（旧库曾全为 main）
                     branch=excluded.branch""",
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
                         kind=excluded.kind, name=excluded.name, raw_json=excluded.raw_json,
                         branch=excluded.branch""",
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
                      c.valid_from AS valid_from,
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

    # ── 结构化用例（S2.1 FR-CON-003）────────────────────────────────────
    def upsert_test_case(
        self, doc_urn: str, case: TestCase, *, content_hash: str, branch: str = "main"
    ) -> None:
        """投影列供 SQL 盘点/过滤，完整模型以 raw_json 为唯一还原来源。"""
        self.conn.execute(
            """INSERT INTO test_cases
               (urn, external_id, case_id, doc_urn, module, title, priority, lifecycle,
                oracle_missing, step_count, oracle_count, source_line, content_hash,
                branch, raw_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(urn) DO UPDATE SET
                 module=excluded.module, title=excluded.title, priority=excluded.priority,
                 lifecycle=excluded.lifecycle, oracle_missing=excluded.oracle_missing,
                 step_count=excluded.step_count, oracle_count=excluded.oracle_count,
                 source_line=excluded.source_line, content_hash=excluded.content_hash,
                 branch=excluded.branch, raw_json=excluded.raw_json""",
            (
                case.urn, case.external_id, case.external_id, doc_urn, case.module,
                case.title, case.priority, case.lifecycle,
                1 if case.oracle_missing else 0, len(case.steps), len(case.oracles),
                case.source_line, content_hash, branch, case.model_dump_json(),
            ),
        )
        self.conn.commit()

    def delete_test_cases_for_doc(self, doc_urn: str) -> int:
        """整文档替换前清旧（重灌幂等：文档内删除的用例不残留）。"""
        cur = self.conn.execute("DELETE FROM test_cases WHERE doc_urn=?", (doc_urn,))
        self.conn.commit()
        return cur.rowcount

    def list_test_cases(self, *, include_cancelled: bool = True) -> list[TestCase]:
        sql = "SELECT raw_json FROM test_cases"
        if not include_cancelled:
            sql += " WHERE lifecycle != 'cancelled'"
        sql += " ORDER BY doc_urn, source_line"
        return [TestCase.model_validate_json(r["raw_json"])
                for r in self.conn.execute(sql).fetchall()]

    def get_test_case(self, case_urn: str) -> TestCase | None:
        """单用例读取（S2.5 人审服务输入）；URN 不存在返回 None 由上层映射 404。"""
        row = self.conn.execute(
            "SELECT raw_json FROM test_cases WHERE urn=?", (case_urn,)
        ).fetchone()
        return TestCase.model_validate_json(row["raw_json"]) if row else None

    def count_test_cases(self) -> int:
        return int(
            self.conn.execute("SELECT COUNT(*) AS n FROM test_cases").fetchone()["n"]
        )

    # ── 需求骨架（S2.2 FR-TD-001）────────────────────────────────────────
    def upsert_requirement_skeleton(
        self, doc_urn: str, result: Any, *, content_hash: str, branch: str = "main"
    ) -> None:
        """投影列供 SQL 盘点/过滤，完整结果以 raw_json 为唯一还原来源。"""
        ac_missing = sum(
            1 for g in result.gaps if g.kind == "ac_missing_for_feature"
        )
        self.conn.execute(
            """INSERT INTO requirement_skeletons
               (urn, external_id, content_hash, branch, feature_count, rule_count,
                ac_count, ac_missing_count, gap_count, dead_letter_count,
                linking_source, raw_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(urn) DO UPDATE SET
                 content_hash=excluded.content_hash, branch=excluded.branch,
                 feature_count=excluded.feature_count, rule_count=excluded.rule_count,
                 ac_count=excluded.ac_count, ac_missing_count=excluded.ac_missing_count,
                 gap_count=excluded.gap_count,
                 dead_letter_count=excluded.dead_letter_count,
                 linking_source=excluded.linking_source, raw_json=excluded.raw_json""",
            (
                doc_urn, result.external_id, content_hash, branch,
                len(result.active_features), len(result.rules),
                len(result.acceptance), ac_missing, len(result.gaps),
                len(result.dead_letters), result.linking_source,
                result.model_dump_json(),
            ),
        )
        self.conn.commit()

    def delete_requirement_skeleton(self, doc_urn: str) -> int:
        """整文档替换前清旧（重灌幂等：文档内删除的需求不残留）。"""
        cur = self.conn.execute(
            "DELETE FROM requirement_skeletons WHERE urn=?", (doc_urn,)
        )
        self.conn.commit()
        return cur.rowcount

    def list_requirement_skeletons(self) -> list[RequirementParseResult]:
        return [
            RequirementParseResult.model_validate_json(r["raw_json"])
            for r in self.conn.execute(
                "SELECT raw_json FROM requirement_skeletons ORDER BY external_id"
            ).fetchall()
        ]

    def count_requirement_skeletons(self) -> int:
        return int(
            self.conn.execute(
                "SELECT COUNT(*) AS n FROM requirement_skeletons"
            ).fetchone()["n"]
        )

    def list_requirement_skeleton_rows(self) -> list[dict[str, Any]]:
        """生成 worker 的输入：骨架投影列 + 富结构 RequirementParseResult。"""
        rows = self.conn.execute(
            "SELECT urn, external_id, content_hash, branch, raw_json "
            "FROM requirement_skeletons ORDER BY external_id"
        ).fetchall()
        return [
            {
                "doc_urn": r["urn"],
                "external_id": r["external_id"],
                "content_hash": r["content_hash"],
                "branch": r["branch"],
                "result": RequirementParseResult.model_validate_json(r["raw_json"]),
            }
            for r in rows
        ]

    # ── 测试点（S2.3 FR-TD-002）────────────────────────────────────────
    def upsert_test_point(
        self, doc_urn: str, external_id: str, point: TestPoint, *,
        content_hash: str, branch: str = "main",
    ) -> None:
        """投影列供 SQL 盘点/过滤，完整模型以 raw_json 为唯一还原来源。"""
        self.conn.execute(
            """INSERT INTO test_points
               (urn, external_id, doc_urn, fr_id, kind, priority, provenance,
                text, source_refs, content_hash, branch, raw_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(urn) DO UPDATE SET
                 fr_id=excluded.fr_id, kind=excluded.kind, priority=excluded.priority,
                 provenance=excluded.provenance, text=excluded.text,
                 source_refs=excluded.source_refs, content_hash=excluded.content_hash,
                 branch=excluded.branch, raw_json=excluded.raw_json""",
            (
                point_urn(external_id, point), external_id, doc_urn, point.fr_id,
                point.kind, point.priority, point.provenance, point.text,
                json.dumps(point.source_refs, ensure_ascii=False),
                content_hash, branch, point.model_dump_json(),
            ),
        )
        self.conn.commit()

    def delete_test_points_for_doc(self, doc_urn: str) -> int:
        """整文档替换前清旧（重灌幂等：文档内删除的测试点不残留）。"""
        cur = self.conn.execute("DELETE FROM test_points WHERE doc_urn=?", (doc_urn,))
        self.conn.commit()
        return cur.rowcount

    def list_test_points(self) -> list[TestPoint]:
        return [
            TestPoint.model_validate_json(r["raw_json"])
            for r in self.conn.execute(
                "SELECT raw_json FROM test_points ORDER BY external_id, fr_id, kind"
            ).fetchall()
        ]

    def count_test_points(self) -> int:
        return int(
            self.conn.execute("SELECT COUNT(*) AS n FROM test_points").fetchone()["n"]
        )

    # ── oracle 挖掘草稿（S2.4 FR-TD-003，schema v11）────────────────────
    def upsert_oracle_suggestion(
        self, doc_urn: str, case_urn: str, external_id: str, oracle: MinedOracle, *,
        status: str = "pending", exploratory: bool = False,
        content_hash: str, branch: str = "main",
    ) -> None:
        """投影列供 SQL 盘点/审批过滤，完整 MinedOracle 以 raw_json 还原。

        草稿恒带 status=pending：本表不是正式用例库，S2.5 HITL 批准才回写
        test_cases（FR-TD-004 AC1）。
        """
        self.conn.execute(
            """INSERT INTO oracle_suggestions
               (urn, doc_urn, case_urn, external_id, oracle_type, expectation,
                source_kind, provenance, source_refs, confidence, status, exploratory,
                content_hash, branch, raw_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(urn) DO UPDATE SET
                 doc_urn=excluded.doc_urn, oracle_type=excluded.oracle_type,
                 expectation=excluded.expectation, source_kind=excluded.source_kind,
                 provenance=excluded.provenance, source_refs=excluded.source_refs,
                 confidence=excluded.confidence, status=excluded.status,
                 exploratory=excluded.exploratory, content_hash=excluded.content_hash,
                 branch=excluded.branch, raw_json=excluded.raw_json""",
            (
                suggestion_urn(external_id, oracle), doc_urn, case_urn, external_id,
                oracle.type.value, oracle.expectation, oracle.source_kind,
                oracle.provenance,
                json.dumps(oracle.source_refs, ensure_ascii=False),
                oracle.confidence, status, 1 if exploratory else 0,
                content_hash, branch, oracle.model_dump_json(),
            ),
        )
        self.conn.commit()

    def delete_oracle_suggestions_for_doc(self, doc_urn: str) -> int:
        """整文档替换前清旧（重灌幂等：文档内删除的用例草稿不残留，防幽灵）。"""
        cur = self.conn.execute(
            "DELETE FROM oracle_suggestions WHERE doc_urn=?", (doc_urn,)
        )
        self.conn.commit()
        return cur.rowcount

    def delete_oracle_suggestions_for_case(self, case_urn: str) -> int:
        """单用例草稿清理（取消态清理/S2.5 单点回滚预留）。"""
        cur = self.conn.execute(
            "DELETE FROM oracle_suggestions WHERE case_urn=?", (case_urn,)
        )
        self.conn.commit()
        return cur.rowcount

    def list_oracle_suggestions(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT case_urn, external_id, status, exploratory, raw_json "
            "FROM oracle_suggestions ORDER BY external_id, indexed_at"
        ).fetchall()
        return [
            {
                "case_urn": r["case_urn"],
                "external_id": r["external_id"],
                "status": r["status"],
                "exploratory": bool(r["exploratory"]),
                "oracle": MinedOracle.model_validate_json(r["raw_json"]),
            }
            for r in rows
        ]

    def count_oracle_suggestions(self) -> int:
        return int(
            self.conn.execute(
                "SELECT COUNT(*) AS n FROM oracle_suggestions"
            ).fetchone()["n"]
        )

    def list_case_doc_rows(self) -> list[dict[str, Any]]:
        """oracle worker 的增量输入：用例→所属文档/hash/分支投影（含取消态）。"""
        rows = self.conn.execute(
            "SELECT urn, doc_urn, content_hash, branch, lifecycle "
            "FROM test_cases ORDER BY doc_urn, source_line"
        ).fetchall()
        return [dict(r) for r in rows]

    # ── S2.5 人审留痕（FR-TD-004 / FR-WR-001）────────────────────────────
    @staticmethod
    def _suggestion_row_to_dict(r: sqlite3.Row) -> dict[str, Any]:
        return {
            "urn": r["urn"],
            "doc_urn": r["doc_urn"],
            "case_urn": r["case_urn"],
            "external_id": r["external_id"],
            "status": r["status"],
            "exploratory": bool(r["exploratory"]),
            "approval_id": r["approval_id"],
            "reviewed_by": r["reviewed_by"],
            "reviewed_at": r["reviewed_at"],
            "decision_reason": r["decision_reason"],
            "oracle": MinedOracle.model_validate_json(r["raw_json"]),
        }

    def list_pending_suggestions_for_case(self, case_urn: str) -> list[dict[str, Any]]:
        """单用例待审草稿（审批服务的输入）；已评审草稿不返回。"""
        rows = self.conn.execute(
            "SELECT * FROM oracle_suggestions WHERE case_urn=? AND status='pending' "
            "ORDER BY urn",
            (case_urn,),
        ).fetchall()
        return [self._suggestion_row_to_dict(r) for r in rows]

    def list_review_queue_rows(self) -> list[dict[str, Any]]:
        """全库待审草稿（审批 API 队列端点），按用例 URN + 草稿 URN 排序。"""
        rows = self.conn.execute(
            "SELECT * FROM oracle_suggestions WHERE status='pending' "
            "ORDER BY case_urn, urn"
        ).fetchall()
        return [self._suggestion_row_to_dict(r) for r in rows]

    def mark_suggestion_reviewed(
        self, urn: str, *, status: str, approval_id: str,
        reviewer: str, reason: str, reviewed_at: str,
    ) -> None:
        """评审落账：status 翻 approved/rejected 并补四人审列。

        只允许从 pending 翻转——对已评审草稿重复决策是接线 bug，响亮失败而非
        静默覆盖（审批页并发双击由本守卫挡住）。
        """
        cur = self.conn.execute(
            """UPDATE oracle_suggestions
               SET status=?, approval_id=?, reviewed_by=?, reviewed_at=?,
                   decision_reason=?
               WHERE urn=? AND status='pending'""",
            (status, approval_id, reviewer, reviewed_at, reason, urn),
        )
        if cur.rowcount == 0:
            raise ValueError(f"草稿不存在或已评审，无法落账: {urn}")
        self.conn.commit()

    def save_preference_sample(self, sample: PreferenceSample) -> None:
        """人工修改 diff 偏好样本（仅 modify 产生）；sample_id 内容寻址，重放不翻倍。"""
        self.conn.execute(
            """INSERT INTO preference_samples
               (sample_id, case_urn, suggestion_urn, reviewer, reason,
                original_json, revised_json, diff_json, created_at)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(sample_id) DO NOTHING""",
            (
                sample.sample_id, sample.case_urn, sample.suggestion_urn,
                sample.reviewer, sample.reason,
                json.dumps(sample.original, ensure_ascii=False),
                json.dumps(sample.revised, ensure_ascii=False),
                json.dumps(sample.diff, ensure_ascii=False),
                sample.created_at.isoformat(),
            ),
        )
        self.conn.commit()

    def list_preference_samples(self) -> list[PreferenceSample]:
        rows = self.conn.execute(
            "SELECT * FROM preference_samples ORDER BY created_at, sample_id"
        ).fetchall()
        return [
            PreferenceSample(
                sample_id=r["sample_id"],
                case_urn=r["case_urn"],
                suggestion_urn=r["suggestion_urn"],
                reviewer=r["reviewer"],
                reason=r["reason"],
                original=json.loads(r["original_json"]),
                revised=json.loads(r["revised_json"]),
                diff=json.loads(r["diff_json"]),
                created_at=datetime.fromisoformat(r["created_at"]),
            )
            for r in rows
        ]

    # ── L3 规则资产版本链（schema v13，FR-KNO-002 S2.6a）──────────────────
    @staticmethod
    def _asset_row_to_model(r: sqlite3.Row) -> RuleAssetRevision:
        return RuleAssetRevision(
            asset_id=r["asset_id"],
            version=r["version"],
            kind=r["kind"],
            name=r["name"],
            content=json.loads(r["content_json"]),
            content_hash=r["content_hash"],
            author=r["author"],
            note=r["note"],
            created_at=datetime.fromisoformat(r["created_at"]),
        )

    def append_asset_revision(self, revision: RuleAssetRevision) -> bool:
        """不可变版本只 INSERT；同 (asset_id, content_hash) 冲突 DO NOTHING。

        返回 True=新写入，False=同内容版本已存在（幂等重放）。版本号由 core
        按历史链计算，PK(asset_id, version) 撞键是接线 bug，不放行静默覆盖。
        """
        cur = self.conn.execute(
            """INSERT INTO rule_assets
               (asset_id, version, kind, name, content_hash, content_json,
                author, note, created_at)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(asset_id, content_hash) DO NOTHING""",
            (
                revision.asset_id, revision.version, str(revision.kind),
                revision.name, revision.content_hash,
                json.dumps(revision.content, ensure_ascii=False),
                revision.author, revision.note,
                revision.created_at.isoformat(),
            ),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def list_asset_revisions(self, asset_id: str) -> list[RuleAssetRevision]:
        rows = self.conn.execute(
            "SELECT * FROM rule_assets WHERE asset_id=? ORDER BY version",
            (asset_id,),
        ).fetchall()
        return [self._asset_row_to_model(r) for r in rows]

    def get_asset_revision(
        self, asset_id: str, version: int
    ) -> RuleAssetRevision | None:
        row = self.conn.execute(
            "SELECT * FROM rule_assets WHERE asset_id=? AND version=?",
            (asset_id, version),
        ).fetchone()
        return self._asset_row_to_model(row) if row else None

    def list_latest_assets(self) -> list[RuleAssetRevision]:
        """每个 asset_id 只取最大版本行（资产清单视图），按 asset_id 排序稳定。"""
        rows = self.conn.execute(
            """SELECT a.* FROM rule_assets a
               JOIN (SELECT asset_id, MAX(version) AS v
                     FROM rule_assets GROUP BY asset_id) m
                 ON a.asset_id=m.asset_id AND a.version=m.v
               ORDER BY a.asset_id"""
        ).fetchall()
        return [self._asset_row_to_model(r) for r in rows]

    def persist_case_decision(self, plan: CaseDecisionPlan, *, reviewed_at: str) -> None:
        """单事务落人审四件套：Approval + outbox + 偏好样本 + 草稿状态翻转。

        任一步失败整体回滚（含并发双击撞上 pending 守卫时），绝不留「outbox
        已记账但草稿仍 pending」的交错态。注意本方法**不写 test_cases**——
        正式用例库只由摄入管线改写（FR-TD-004 AC1），回写走 outbox 派发落独立写根。
        """
        try:
            self.conn.execute("BEGIN")
            self.conn.execute(
                """INSERT INTO approvals
                   (approval_id, action_level, target_system, request_summary,
                    request_diff, decision, reviewer, reason, based_on,
                    created_at, decided_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(approval_id) DO UPDATE SET
                     decision=excluded.decision, reviewer=excluded.reviewer,
                     reason=excluded.reason, decided_at=excluded.decided_at""",
                (
                    plan.approval.approval_id, str(plan.approval.action_level),
                    plan.approval.target_system, plan.approval.request_summary,
                    json.dumps(plan.approval.request_diff, ensure_ascii=False),
                    str(plan.approval.decision), plan.approval.reviewer,
                    plan.approval.reason,
                    json.dumps(plan.approval.based_on_versions, ensure_ascii=False),
                    plan.approval.created_at.isoformat(),
                    plan.approval.decided_at.isoformat()
                    if plan.approval.decided_at else None,
                ),
            )
            if plan.outbox is not None:
                # 内容寻址幂等键：同内容重复评审不产生第二条 outbox（DO NOTHING）
                self.conn.execute(
                    """INSERT INTO outbox
                       (idempotency_key, action_level, target_system, request_body,
                        status, external_ref, approval_id, attempts, rollback_note,
                        error, created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(idempotency_key) DO NOTHING""",
                    (
                        plan.outbox.idempotency_key, str(plan.outbox.action_level),
                        plan.outbox.target_system,
                        json.dumps(plan.outbox.request_body, ensure_ascii=False),
                        str(plan.outbox.status), plan.outbox.external_ref,
                        plan.outbox.approval_id, plan.outbox.attempts,
                        plan.outbox.rollback_note, plan.outbox.error,
                        plan.outbox.created_at.isoformat(),
                        plan.outbox.updated_at.isoformat(),
                    ),
                )
            for sample in plan.preference_samples:
                self.conn.execute(
                    """INSERT INTO preference_samples
                       (sample_id, case_urn, suggestion_urn, reviewer, reason,
                        original_json, revised_json, diff_json, created_at)
                       VALUES (?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(sample_id) DO NOTHING""",
                    (
                        sample.sample_id, sample.case_urn, sample.suggestion_urn,
                        sample.reviewer, sample.reason,
                        json.dumps(sample.original, ensure_ascii=False),
                        json.dumps(sample.revised, ensure_ascii=False),
                        json.dumps(sample.diff, ensure_ascii=False),
                        sample.created_at.isoformat(),
                    ),
                )
            for update in plan.suggestion_updates:
                cur = self.conn.execute(
                    """UPDATE oracle_suggestions
                       SET status=?, approval_id=?, reviewed_by=?, reviewed_at=?,
                           decision_reason=?
                       WHERE urn=? AND status='pending'""",
                    (update.status, plan.approval.approval_id,
                     plan.approval.reviewer, reviewed_at, update.reason, update.urn),
                )
                if cur.rowcount == 0:
                    # 双击/并发评审：整条事务回滚，前序审批/outbox 一并撤销
                    raise ValueError(f"草稿不存在或已评审，无法落账: {update.urn}")
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise

    def search_test_cases(
        self, fr_ids: Sequence[str], like_terms: Sequence[str] = (), *, limit: int = 10
    ) -> list[dict[str, Any]]:
        """影响面检索的相似用例一路（FR-TD-002）：FR 标签精确命中为主、标题
        短词 LIKE 兜底。只返活跃用例；优先级 P0→P3 确定性排序，同名按 URN。"""
        clauses: list[str] = []
        params: list[Any] = []
        if fr_ids:
            placeholders = ",".join("?" for _ in fr_ids)
            clauses.append(
                f"EXISTS (SELECT 1 FROM json_each(json_extract(raw_json,'$.tags')) "
                f"WHERE value IN ({placeholders}))"
            )
            params.extend(fr_ids)
        like_clauses = []
        for term in like_terms:
            if not term:
                continue
            like_clauses.append("title LIKE ?")
            params.append(f"%{term}%")
        if like_clauses:
            clauses.append("(" + " OR ".join(like_clauses) + ")")
        if not clauses:
            return []
        sql = (
            "SELECT urn, raw_json FROM test_cases WHERE lifecycle='active' AND ("
            + " OR ".join(clauses)
            + ") ORDER BY CASE priority WHEN 'P0' THEN 0 WHEN 'P1' THEN 1 "
            "WHEN 'P2' THEN 2 ELSE 3 END, urn LIMIT ?"
        )
        params.append(limit)
        out: list[dict[str, Any]] = []
        for r in self.conn.execute(sql, params).fetchall():
            case = TestCase.model_validate_json(r["raw_json"])
            out.append({
                "urn": case.urn,
                "external_id": case.external_id,
                "title": case.title,
                "module": case.module,
                "priority": case.priority,
                "tags": case.tags,
                "text": " ".join([case.title, *case.steps])[:300],
            })
        return out

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

    @staticmethod
    def _row_to_approval(row: sqlite3.Row) -> Approval:
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

    def get_approval(self, approval_id: str) -> Approval | None:
        row = self.conn.execute(
            "SELECT * FROM approvals WHERE approval_id=?", (approval_id,)
        ).fetchone()
        return self._row_to_approval(row) if row else None

    def list_approvals(self, *, limit: int = 100) -> list[Approval]:
        """审批动作账本（FR-WR-001 AC2：评审人/时间/结论可浏览），新审批在前。"""
        rows = self.conn.execute(
            "SELECT * FROM approvals ORDER BY rowid DESC LIMIT ?", (limit,)
        ).fetchall()
        return [self._row_to_approval(r) for r in rows]

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

    def pending_outbox_records(self) -> list[OutboxRecord]:
        """待派发队列（pending+failed）的模型化读口，S2.5 派发 worker 使用。"""
        return [self._row_to_outbox(r) for r in self.list_pending_outbox()]

    def mark_outbox(self, idempotency_key: str, status: OutboxStatus, *, external_ref: str | None = None,
                    error: str = "") -> None:
        self.conn.execute(
            """UPDATE outbox SET status=?, external_ref=COALESCE(?, external_ref),
               attempts=attempts+1, error=?, updated_at=? WHERE idempotency_key=?""",
            (str(status), external_ref, error, _now_iso(), idempotency_key),
        )
        self.conn.commit()

    def _row_to_outbox(self, r: sqlite3.Row) -> OutboxRecord:
        return OutboxRecord(
            id=r["id"],
            idempotency_key=r["idempotency_key"],
            action_level=r["action_level"],
            target_system=r["target_system"],
            request_body=json.loads(r["request_body"]),
            status=OutboxStatus(r["status"]),
            external_ref=r["external_ref"],
            approval_id=r["approval_id"],
            attempts=r["attempts"],
            rollback_note=r["rollback_note"],
            error=r["error"],
            created_at=datetime.fromisoformat(r["created_at"]),
            updated_at=datetime.fromisoformat(r["updated_at"]),
        )

    def list_outbox_records(self) -> list[OutboxRecord]:
        """全量 outbox（审批页审计面板，FR-WR-001 AC1 四要素可浏览）。"""
        rows = self.conn.execute("SELECT * FROM outbox ORDER BY id").fetchall()
        return [self._row_to_outbox(r) for r in rows]

    def get_outbox_record(self, idempotency_key: str) -> OutboxRecord | None:
        r = self.conn.execute(
            "SELECT * FROM outbox WHERE idempotency_key=?", (idempotency_key,)
        ).fetchone()
        return self._row_to_outbox(r) if r else None

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

    # ── 问答运行（schema v5 起，v6/v7 各增一列，FR-QA-001～004）─────────
    def insert_qa_run(self, *, record: dict[str, Any]) -> int:
        """qa_runs 落一行。record 是图 state 投影出的全原语 dict（见 QaService）；
        run_id UNIQUE，重复写入直接抛 IntegrityError——服务层承诺一次 run 一行，
        双写是接线 bug，不能静默覆盖。
        """
        cur = self.conn.execute(
            """INSERT INTO qa_runs
               (run_id, ts, project, branch, acl_scope, question, intent, status,
                answer_payload, citations, evidence, verify_results, retrieval_event_id,
                unknown_reason, citation_errors, intent_source, degraded, attempts, regens,
                model_version, prompt_version, params_version, data_version, tokens_in,
                tokens_out, latency_ms, thread_id, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                record["run_id"], record.get("ts", _now_iso()),
                record.get("project", ""), record.get("branch", "main"),
                record.get("acl_scope", "default"), record["question"],
                record.get("intent", ""), record["status"],
                json.dumps(record.get("answer_payload") or {}, ensure_ascii=False),
                json.dumps(record.get("citations") or [], ensure_ascii=False),
                json.dumps(record.get("evidence") or [], ensure_ascii=False),
                json.dumps(record.get("verify_results") or [], ensure_ascii=False),
                record.get("retrieval_event_id"),
                record.get("unknown_reason", ""),
                json.dumps(record.get("citation_errors") or [], ensure_ascii=False),
                record.get("intent_source", ""),
                json.dumps(record.get("degraded") or [], ensure_ascii=False),
                int(record.get("attempts", 0)), int(record.get("regens", 0)),
                record.get("model_version", ""), record.get("prompt_version", ""),
                record.get("params_version", ""), record.get("data_version", ""),
                int(record.get("tokens_in", 0)), int(record.get("tokens_out", 0)),
                int(record.get("latency_ms", 0)), record.get("thread_id", ""),
                _now_iso(),
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    @staticmethod
    def _qa_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        for key in ("answer_payload", "citations", "evidence", "verify_results",
                    "degraded", "citation_errors"):
            d[key] = json.loads(d[key])
        return d

    def get_retrieval_event(self, event_id: int) -> dict[str, Any] | None:
        """按主键取检索留痕（trace 时间线反查命中通道用）；不存在返回 None。"""
        row = self.conn.execute(
            "SELECT * FROM retrieval_events WHERE id=?", (event_id,)
        ).fetchone()
        if row is None:
            return None
        d = dict(row)
        for key in ("fts_urns", "vector_urns", "fused_urns", "raw_json"):
            d[key] = json.loads(d[key])
        return d

    def get_qa_run(self, run_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM qa_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        return self._qa_row_to_dict(row) if row else None

    def list_qa_runs(self, *, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        if status:
            rows = self.conn.execute(
                "SELECT * FROM qa_runs WHERE status=? ORDER BY id DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM qa_runs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._qa_row_to_dict(r) for r in rows]

    def feedback_summary(self) -> list[dict[str, Any]]:
        """FR-QA-004 AC2 看板备料：按 kind/reason 聚合计数（端点留 S1.5）。"""
        rows = self.conn.execute(
            """SELECT kind, COALESCE(reason, '') AS reason, COUNT(*) AS n
               FROM feedback GROUP BY kind, reason ORDER BY kind, n DESC"""
        ).fetchall()
        return [{"kind": r["kind"], "reason": r["reason"], "n": r["n"]} for r in rows]

    def list_feedback(self) -> list[dict[str, Any]]:
        """S2.6c bad case 摄入扫描：全量反馈行（含自增 id 幂等键），按 id 稳定排序。"""
        rows = self.conn.execute(
            """SELECT id, run_id, target_urn, kind, COALESCE(reason, '') AS reason,
                      detail, model_version, created_at
               FROM feedback ORDER BY id"""
        ).fetchall()
        return [dict(r) for r in rows]

    def get_eval_run(self, eval_run_id: str) -> dict[str, Any] | None:
        """S2.6c 回归关联前的存在性校验：拒绝把坏例挂到幽灵评测运行上。"""
        r = self.conn.execute(
            "SELECT eval_run_id, metrics, created_at FROM eval_runs WHERE eval_run_id=?",
            (eval_run_id,)).fetchone()
        return dict(r) if r else None

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

    def upsert_eval_case(self, case: EvalCase, locked_version: str) -> None:
        """冻结题集落库（FR-EV-001 AC2）：同一题集重跑幂等，locked_version 记录题集版本。"""
        self.conn.execute(
            """INSERT INTO eval_cases
               (case_id, kind, question, ground_truth, required_evidence,
                holdout, locked_version)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(case_id) DO UPDATE SET
                 kind=excluded.kind, question=excluded.question,
                 ground_truth=excluded.ground_truth,
                 required_evidence=excluded.required_evidence,
                 holdout=excluded.holdout, locked_version=excluded.locked_version""",
            (
                case.case_id, case.kind, case.question,
                json.dumps(case.ground_truth, ensure_ascii=False),
                json.dumps(case.required_evidence, ensure_ascii=False),
                int(case.holdout), locked_version,
            ),
        )
        self.conn.commit()

    # ── S2.6b 用例关系（FR-ING-004：复用 relations 表，独立 scope 命名空间）─────
    def list_test_point_rows(self) -> list[dict[str, Any]]:
        """关系构建 worker 输入：external_id 投影 + TestPoint（URN 由 point_urn 重算）。"""
        rows = self.conn.execute(
            "SELECT external_id, raw_json FROM test_points "
            "ORDER BY external_id, fr_id, kind"
        ).fetchall()
        return [
            {"external_id": r["external_id"],
             "point": TestPoint.model_validate_json(r["raw_json"])}
            for r in rows
        ]

    @staticmethod
    def _row_to_case_relation(r: sqlite3.Row) -> CaseRelation:
        payload = json.loads(r["evidence_json"])
        return CaseRelation(
            urn=r["urn"],
            source_urn=r["source_urn"],
            target_urn=r["target_urn"],
            kind=r["kind"],
            confidence=r["confidence"],
            evidence_urns=payload.get("urns", []),
            provenance=payload.get("provenance", []),
        )

    def replace_case_relations(
        self, scope_urn: str, relations: Sequence[CaseRelation]
    ) -> int:
        """单事务整组替换本 scope 关系（文档级清扫防幽灵）。

        scope 守卫确保只删 ``urn:testpilot:case-relations:`` 命名空间，摄入管线
        的 calls 等关系 doc_urn 是真实文档 URN，物理隔离永不误伤。URN 撞车直接
        报错回滚（fail-closed），不做静默 DO NOTHING。
        """
        ensure_relation_scope(scope_urn)
        cur = self.conn
        try:
            cur.execute("BEGIN")
            cur.execute("DELETE FROM relations WHERE doc_urn=?", (scope_urn,))
            for rel in relations:
                cur.execute(
                    """INSERT INTO relations
                       (urn, source_urn, target_urn, kind, confidence,
                        evidence_json, doc_urn)
                       VALUES (?,?,?,?,?,?,?)""",
                    (
                        rel.urn, rel.source_urn, rel.target_urn, rel.kind,
                        rel.confidence,
                        json.dumps(
                            {"urns": rel.evidence_urns, "lines": [],
                             "provenance": rel.provenance},
                            ensure_ascii=False),
                        scope_urn,
                    ),
                )
            cur.commit()
        except Exception:
            cur.rollback()
            raise
        return len(relations)

    def list_case_relations(
        self,
        *,
        kind: str | None = None,
        include_low: bool = False,
        scope_urn: str = RELATION_SCOPE_URN,
    ) -> list[CaseRelation]:
        """浏览口：默认藏低置信（FR-ING-004：低置信不面向用户，仅检索降权）。"""
        sql = "SELECT * FROM relations WHERE doc_urn=?"
        params: list[Any] = [scope_urn]
        if kind is not None:
            sql += " AND kind=?"
            params.append(kind)
        sql += " ORDER BY source_urn, kind, target_urn"
        relations = [
            self._row_to_case_relation(r) for r in self.conn.execute(sql, params).fetchall()
        ]
        if not include_low:
            relations = [r for r in relations if not r.is_low_confidence]
        return relations

    def get_case_relation(self, urn: str) -> CaseRelation | None:
        """单关系读取；WHERE 带 scope 前缀，绝不会返回摄入管线的同名资源。"""
        row = self.conn.execute(
            "SELECT * FROM relations WHERE urn=? "
            "AND doc_urn LIKE 'urn:testpilot:case-relations:%'",
            (urn,),
        ).fetchone()
        return self._row_to_case_relation(row) if row else None

    def count_case_relations(self, *, scope_urn: str = RELATION_SCOPE_URN) -> int:
        return int(self.conn.execute(
            "SELECT COUNT(*) AS n FROM relations WHERE doc_urn=?", (scope_urn,)
        ).fetchone()["n"])

    def get_evidence_rows(self, urns: Sequence[str]) -> list[EvidenceRow]:
        """证据 URN 批量解析为可读行（FR-ING-004 AC2：点开证据原文）。

        依次查 chunks/entities/test_cases/test_points/oracle_suggestions/
        requirement_skeletons 六个来源；查不到的 URN 不返回，由 core
        assemble_evidence_dossier 标 unresolved（fail-closed，不编造原文）。
        """
        if not urns:
            return []
        wanted = list(dict.fromkeys(urns))  # 去重保序
        placeholders = ",".join("?" for _ in wanted)
        resolved: dict[str, EvidenceRow] = {}

        chunk_rows = self.conn.execute(
            f"""SELECT c.urn AS urn, c.raw_json AS raw_json, d.uri AS doc_uri
                FROM chunks c JOIN source_documents d ON d.urn = c.doc_urn
                WHERE c.urn IN ({placeholders})""",
            wanted,
        ).fetchall()
        for r in chunk_rows:
            chunk = KnowledgeChunk.model_validate_json(r["raw_json"])
            resolved[r["urn"]] = EvidenceRow(
                urn=r["urn"], status="resolved", kind=chunk.kind,
                label=chunk.metadata_.get("title") or chunk.text[:40],
                excerpt=chunk.text[:200], doc_uri=r["doc_uri"] or "")

        entity_rows = self.conn.execute(
            f"SELECT urn, raw_json FROM entities WHERE urn IN ({placeholders})",
            wanted,
        ).fetchall()
        for r in entity_rows:
            entity = Entity.model_validate_json(r["raw_json"])
            resolved[r["urn"]] = EvidenceRow(
                urn=r["urn"], status="resolved", kind=f"entity:{entity.kind}",
                label=entity.name,
                excerpt=str(entity.metadata_.get("signature", ""))[:200])

        case_rows = self.conn.execute(
            f"SELECT urn, raw_json FROM test_cases WHERE urn IN ({placeholders})",
            wanted,
        ).fetchall()
        for r in case_rows:
            case = TestCase.model_validate_json(r["raw_json"])
            resolved[r["urn"]] = EvidenceRow(
                urn=r["urn"], status="resolved", kind="test_case",
                label=f"{case.external_id} {case.title}".strip(),
                excerpt=" / ".join(case.steps[:2])[:200])

        point_rows = self.conn.execute(
            f"SELECT urn, raw_json FROM test_points WHERE urn IN ({placeholders})",
            wanted,
        ).fetchall()
        for r in point_rows:
            point = TestPoint.model_validate_json(r["raw_json"])
            resolved[r["urn"]] = EvidenceRow(
                urn=r["urn"], status="resolved", kind="test_point",
                label=point.text[:80], excerpt=point.text[:200])

        suggestion_rows = self.conn.execute(
            f"SELECT urn, raw_json FROM oracle_suggestions WHERE urn IN ({placeholders})",
            wanted,
        ).fetchall()
        for r in suggestion_rows:
            oracle = MinedOracle.model_validate_json(r["raw_json"])
            resolved[r["urn"]] = EvidenceRow(
                urn=r["urn"], status="resolved", kind="oracle_suggestion",
                label=oracle.expectation[:80],
                excerpt=f"[{oracle.source_kind}] {oracle.expectation}"[:200])

        # 需求条目 URN 在骨架 raw_json 内，重建 FR/NFR/AC 三类 URN 索引后匹配
        for skel_row in self.conn.execute(
            "SELECT external_id, raw_json FROM requirement_skeletons"
        ).fetchall():
            external_id = skel_row["external_id"]
            skeleton = RequirementParseResult.model_validate_json(skel_row["raw_json"])
            for feature in skeleton.features:
                if feature.urn in wanted and feature.urn not in resolved:
                    resolved[feature.urn] = EvidenceRow(
                        urn=feature.urn, status="resolved", kind="requirement",
                        label=f"{feature.fr_id} {feature.text[:60]}".strip(),
                        excerpt=feature.text[:200])
            for rule in skeleton.rules:
                if rule.urn in wanted and rule.urn not in resolved:
                    resolved[rule.urn] = EvidenceRow(
                        urn=rule.urn, status="resolved", kind="requirement_rule",
                        label=f"{rule.rule_id} {rule.text[:60]}".strip(),
                        excerpt=rule.text[:200])
            for ac in skeleton.acceptance:
                ac_urn = acceptance_urn(external_id, ac)
                if ac_urn in wanted and ac_urn not in resolved:
                    resolved[ac_urn] = EvidenceRow(
                        urn=ac_urn, status="resolved", kind="requirement_ac",
                        label=f"AC-{ac.index + 1} {ac.text[:60]}",
                        excerpt=ac.text[:200])

        return [resolved[u] for u in wanted if u in resolved]

    # ── S2.6c bad case 闭环（FR-EV-005：schema v14 bad_cases 表）──────────────
    @staticmethod
    def _row_to_bad_case(r: sqlite3.Row) -> BadCase:
        # 完整模型以 raw_json 为唯一还原来源，投影列只服务 SQL 盘点/过滤
        return BadCase.model_validate_json(r["raw_json"])

    def insert_bad_case(self, case: BadCase) -> None:
        """新坏例入账；撞 UNIQUE(source, source_ref)/feedback_id 由调用方先查重，
        唯一约束是兜底防线——IntegrityError 直接抛穿代表幂等预检失效的接线 bug。"""
        self.conn.execute(
            """INSERT INTO bad_cases
               (bad_case_id, source, source_ref, feedback_id, status, title,
                question, detail, reason, data_version, model_version,
                prompt_version, params_version, triaged_by, triaged_at,
                decision_note, golden_case_id, regression_eval_run_id,
                created_at, updated_at, closed_at, history_json, raw_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                case.bad_case_id, case.source, case.source_ref, case.feedback_id,
                case.status, case.title, case.question, case.detail, case.reason,
                case.snapshot.data_version, case.snapshot.model_version,
                case.snapshot.prompt_version, case.snapshot.params_version,
                case.triaged_by,
                case.triaged_at.isoformat() if case.triaged_at else None,
                case.decision_note, case.golden_case_id,
                case.regression_eval_run_id, case.created_at.isoformat(),
                case.updated_at.isoformat(),
                case.closed_at.isoformat() if case.closed_at else None,
                json.dumps([e.model_dump(mode="json") for e in case.history],
                           ensure_ascii=False),
                case.model_dump_json(),
            ),
        )
        self.conn.commit()

    def get_bad_case(self, bad_case_id: str) -> BadCase | None:
        r = self.conn.execute(
            "SELECT raw_json FROM bad_cases WHERE bad_case_id=?",
            (bad_case_id,)).fetchone()
        return self._row_to_bad_case(r) if r else None

    def get_bad_case_by_feedback(self, feedback_id: str) -> BadCase | None:
        """负反馈幂等查询：ingest 扫描时跳过已入账的 feedback 行。"""
        if not feedback_id:
            return None
        r = self.conn.execute(
            "SELECT raw_json FROM bad_cases WHERE feedback_id=?",
            (feedback_id,)).fetchone()
        return self._row_to_bad_case(r) if r else None

    def list_bad_cases(
        self, *, status: str | None = None, source: str | None = None,
    ) -> list[BadCase]:
        sql = "SELECT raw_json FROM bad_cases"
        clauses: list[str] = []
        params: list[str] = []
        if status is not None:
            clauses.append("status=?")
            params.append(status)
        if source is not None:
            clauses.append("source=?")
            params.append(source)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at, bad_case_id"
        return [self._row_to_bad_case(r)
                for r in self.conn.execute(sql, params).fetchall()]

    def update_bad_case(self, case: BadCase) -> None:
        """状态机推进后的整行回写；rowcount=0 双击守卫（条目被删/ID 不存在）。"""
        cur = self.conn.execute(
            """UPDATE bad_cases SET
                 status=?, title=?, question=?, detail=?, reason=?,
                 data_version=?, model_version=?, prompt_version=?, params_version=?,
                 triaged_by=?, triaged_at=?, decision_note=?,
                 golden_case_id=?, regression_eval_run_id=?,
                 updated_at=?, closed_at=?, history_json=?, raw_json=?
               WHERE bad_case_id=?""",
            (
                case.status, case.title, case.question, case.detail, case.reason,
                case.snapshot.data_version, case.snapshot.model_version,
                case.snapshot.prompt_version, case.snapshot.params_version,
                case.triaged_by,
                case.triaged_at.isoformat() if case.triaged_at else None,
                case.decision_note, case.golden_case_id,
                case.regression_eval_run_id, case.updated_at.isoformat(),
                case.closed_at.isoformat() if case.closed_at else None,
                json.dumps([e.model_dump(mode="json") for e in case.history],
                           ensure_ascii=False),
                case.model_dump_json(), case.bad_case_id,
            ),
        )
        if cur.rowcount == 0:
            raise ValueError(f"bad case 不存在，无法回写状态: {case.bad_case_id}")
        self.conn.commit()
