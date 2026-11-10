"""SQLite 任务队列（QueuePort 的 offline 实现，docs/07 §3）。

生产用 Redis/arq；offline-demo 与无外部依赖测试用本实现。
至少一次投递 + 幂等键去重，由任务侧保证执行幂等。

v15（S3.1 FR-EX-002）补齐独立 runner-worker 语义：

- **租约**：dequeue 即写 ``lease_expires_at``，worker 崩溃后过期 running 任务
  由 :meth:`reclaim_stale` 回收（重领或死信），未过期不抢，防并发双跑；
- **协作式取消**：``request_cancel`` 只打标记，worker 在认领/执行检查点读标记，
  队列本身不强杀进程（强杀在 worker 的超时/取消逻辑里做）。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from testpilot_core.execution import JobLease, decide_reclaim

from .sqlite_store import _now_iso

# 默认租约 5 分钟：长于单条 XCUITest 的常规检查点间隔，短于崩溃人工发现时间。
# worker 正常完成时 ack 会清状态，租约只在进程被杀等异常路径生效。
DEFAULT_LEASE_SECONDS = 300
DEFAULT_MAX_ATTEMPTS = 3


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


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

    async def dequeue(
        self, job_types: list[str], *, lease_seconds: int = DEFAULT_LEASE_SECONDS
    ) -> dict[str, Any] | None:
        placeholders = ",".join("?" for _ in job_types)
        lease_expires = (_utcnow() + timedelta(seconds=lease_seconds)).isoformat()
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
                """UPDATE jobs
                      SET status='running', attempts=attempts+1, lease_expires_at=?,
                          updated_at=?
                    WHERE id=?""",
                (lease_expires, _now_iso(), row["id"]),
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
            "cancel_requested": bool(row["cancel_requested"]),
        }

    async def ack(self, job_id: str) -> None:
        self.conn.execute(
            """UPDATE jobs
                  SET status='done', lease_expires_at=NULL, updated_at=?
                WHERE id=?""",
            (_now_iso(), job_id),
        )
        self.conn.commit()

    async def fail(self, job_id: str, error: str) -> None:
        self.conn.execute(
            """UPDATE jobs
                  SET status='failed', error=?, lease_expires_at=NULL, updated_at=?
                WHERE id=?""",
            (error, _now_iso(), job_id),
        )
        self.conn.commit()

    async def reclaim_stale(
        self, *, max_attempts: int = DEFAULT_MAX_ATTEMPTS, now: datetime | None = None
    ) -> list[dict[str, str]]:
        """回收过期租约（FR-EX-002 AC1：worker 重启后任务可被重新领取）。

        纯 SQL 原子翻转，且 WHERE 带 status='running' 守卫，重复调用不会
        把已被正常 ack 的任务误重领。返回每条回收任务的动作与原因。
        """
        now = now or _utcnow()
        rows = self.conn.execute(
            "SELECT id, status, attempts, lease_expires_at FROM jobs WHERE status='running'"
        ).fetchall()
        decisions: list[dict[str, str]] = []
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            for row in rows:
                lease_expires = (
                    datetime.fromisoformat(row["lease_expires_at"])
                    if row["lease_expires_at"]
                    else None
                )
                decision = decide_reclaim(
                    JobLease(
                        job_id=str(row["id"]),
                        status=row["status"],
                        attempts=row["attempts"],
                        lease_expires_at=lease_expires,
                    ),
                    now=now,
                    max_attempts=max_attempts,
                )
                if decision.action == "none":
                    continue
                if decision.action == "requeue":
                    cur = self.conn.execute(
                        """UPDATE jobs SET status='queued', lease_expires_at=NULL,
                              updated_at=? WHERE id=? AND status='running'""",
                        (_now_iso(), row["id"]),
                    )
                else:  # dead_letter
                    cur = self.conn.execute(
                        """UPDATE jobs SET status='failed', lease_expires_at=NULL,
                              error=?, updated_at=? WHERE id=? AND status='running'""",
                        (f"reclaim: {decision.reason}", _now_iso(), row["id"]),
                    )
                if cur.rowcount == 1:
                    decisions.append(
                        {"job_id": str(row["id"]),
                         "action": decision.action, "reason": decision.reason}
                    )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return decisions

    async def request_cancel(self, idempotency_key: str) -> bool:
        """打协作式取消标记；只对 queued/running 有效，返回是否成功标记。"""
        cur = self.conn.execute(
            """UPDATE jobs SET cancel_requested=1, updated_at=?
                WHERE idempotency_key=? AND status IN ('queued','running')""",
            (_now_iso(), idempotency_key),
        )
        self.conn.commit()
        return cur.rowcount == 1

    async def is_cancel_requested(self, job_id: str) -> bool:
        row = self.conn.execute(
            "SELECT cancel_requested FROM jobs WHERE id=?", (job_id,)
        ).fetchone()
        return bool(row and row["cancel_requested"])

    async def get_job(self, job_id: str) -> dict[str, Any] | None:
        """按 jobs 主键查单条（崩溃恢复时从 job 反查幂等键用）。"""
        row = self.conn.execute(
            "SELECT * FROM jobs WHERE id=?", (job_id,)
        ).fetchone()
        if row is None:
            return None
        return {
            "job_id": str(row["id"]),
            "job_type": row["job_type"],
            "payload": json.loads(row["payload_json"]),
            "idempotency_key": row["idempotency_key"],
            "status": row["status"],
            "attempts": row["attempts"],
            "cancel_requested": bool(row["cancel_requested"]),
        }
