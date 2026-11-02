"""SQLite 任务队列（QueuePort 的 offline 实现，docs/07 §3）。

生产用 Redis/arq；offline-demo 与无外部依赖测试用本实现。
至少一次投递 + 幂等键去重，由任务侧保证执行幂等。
"""

from __future__ import annotations

import json
from typing import Any

from .sqlite_store import _now_iso


class SqliteQueue:
    def __init__(self, conn) -> None:  # noqa: ANN001 - 复用 Store 的 sqlite3.Connection
        self.conn = conn

    async def enqueue(self, job_type: str, payload: dict[str, Any], idempotency_key: str) -> str:
        import sqlite3

        try:
            self.conn.execute(
                """INSERT INTO jobs(job_type, payload_json, idempotency_key, status, created_at, updated_at)
                   VALUES (?,?,?,?,?,?)""",
                (job_type, json.dumps(payload, ensure_ascii=False), idempotency_key,
                 "queued", _now_iso(), _now_iso()),
            )
            self.conn.commit()
        except sqlite3.IntegrityError:
            # 冲突时 sqlite3 模块已隐式开启事务，必须回滚，否则后续显式 BEGIN 报错
            self.conn.rollback()
        row = self.conn.execute(
            "SELECT id FROM jobs WHERE idempotency_key=?", (idempotency_key,)
        ).fetchone()
        return str(row["id"])

    async def dequeue(self, job_types: list[str]) -> dict[str, Any] | None:
        placeholders = ",".join("?" for _ in job_types)
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute(
                f"""SELECT * FROM jobs WHERE status='queued' AND job_type IN ({placeholders})
                    ORDER BY id LIMIT 1""",
                job_types,
            ).fetchone()
            if row is None:
                self.conn.execute("COMMIT")
                return None
            self.conn.execute(
                "UPDATE jobs SET status='running', attempts=attempts+1, updated_at=? WHERE id=?",
                (_now_iso(), row["id"]),
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {
            "job_id": str(row["id"]),
            "job_type": row["job_type"],
            "payload": json.loads(row["payload_json"]),
            "idempotency_key": row["idempotency_key"],
        }

    async def ack(self, job_id: str) -> None:
        self.conn.execute(
            "UPDATE jobs SET status='done', updated_at=? WHERE id=?", (_now_iso(), job_id)
        )
        self.conn.commit()

    async def fail(self, job_id: str, error: str) -> None:
        self.conn.execute(
            "UPDATE jobs SET status='failed', error=?, updated_at=? WHERE id=?",
            (error, _now_iso(), job_id),
        )
        self.conn.commit()
