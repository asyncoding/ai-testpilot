"""S3.1 队列租约/取消/回收测试（FR-EX-002，tmp 库 hermetic，hostless）。

覆盖：dequeue 写租约与 attempts、未过期不抢、过期重领、达上限死信、
协作式取消标记、ack/fail 清租约、幂等 enqueue。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from testpilot_adapters.store import SqliteQueue, bootstrap_store

_T0 = datetime(2026, 11, 10, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def queue(tmp_path):
    store = bootstrap_store(tmp_path)
    yield SqliteQueue(store.conn)
    store.close()


async def test_dequeue_writes_lease_and_attempts(queue):
    await queue.enqueue("test_execution", {"request_id": "r1"}, "idem-1")
    job = await queue.dequeue(["test_execution"], lease_seconds=60)
    assert job and job["job_type"] == "test_execution"
    assert job["cancel_requested"] is False
    row = queue.conn.execute("SELECT * FROM jobs WHERE id=?", (job["job_id"],)).fetchone()
    assert row["status"] == "running" and row["attempts"] == 1
    assert row["lease_expires_at"] is not None
    # 没有第二个 queued 任务时返回 None
    assert await queue.dequeue(["test_execution"]) is None


async def test_enqueue_idempotent_returns_same_job_id(queue):
    jid1 = await queue.enqueue("test_execution", {"v": 1}, "idem-x")
    jid2 = await queue.enqueue("test_execution", {"v": 2}, "idem-x")
    assert jid1 == jid2
    row = queue.conn.execute("SELECT payload_json FROM jobs WHERE id=?", (jid1,)).fetchone()
    assert '"v": 1' in row["payload_json"]  # 首次写入不被覆盖


async def test_reclaim_skips_unexpired_and_requeues_expired(queue):
    await queue.enqueue("test_execution", {}, "idem-1")
    job = await queue.dequeue(["test_execution"], lease_seconds=60)
    # 冻结时钟口径：先把租约拨到 _T0 之后，验未过期不回收（dequeue 写的是真实时钟）
    queue.conn.execute(
        "UPDATE jobs SET lease_expires_at=? WHERE id=?",
        ((_T0 + timedelta(seconds=60)).isoformat(), job["job_id"]))
    queue.conn.commit()
    assert await queue.reclaim_stale(now=_T0) == []
    # 把租约拨到过去模拟 worker 被杀
    queue.conn.execute(
        "UPDATE jobs SET lease_expires_at=? WHERE id=?",
        ((_T0 - timedelta(seconds=1)).isoformat(), job["job_id"]))
    queue.conn.commit()
    decisions = await queue.reclaim_stale(now=_T0)
    assert decisions == [{"job_id": job["job_id"],
                          "action": "requeue", "reason": "lease_expired"}]
    row = queue.conn.execute("SELECT * FROM jobs WHERE id=?", (job["job_id"],)).fetchone()
    assert row["status"] == "queued" and row["lease_expires_at"] is None
    # 重领：attempts 累加，证明至少一次投递
    redelivered = await queue.dequeue(["test_execution"])
    assert redelivered["job_id"] == job["job_id"]
    assert queue.conn.execute(
        "SELECT attempts FROM jobs WHERE id=?", (job["job_id"],)).fetchone()["attempts"] == 2
    # 回收幂等：正常 running 且未过期时再调无动作（重领租约按冻结时钟重拨）
    queue.conn.execute(
        "UPDATE jobs SET lease_expires_at=? WHERE id=?",
        ((_T0 + timedelta(seconds=60)).isoformat(), job["job_id"]))
    queue.conn.commit()
    assert await queue.reclaim_stale(now=_T0) == []


async def test_reclaim_dead_letters_after_max_attempts(queue):
    await queue.enqueue("test_execution", {}, "idem-1")
    job = await queue.dequeue(["test_execution"])  # attempts=1
    queue.conn.execute(
        "UPDATE jobs SET attempts=3, lease_expires_at=? WHERE id=?",
        ((_T0 - timedelta(seconds=1)).isoformat(), job["job_id"]))
    queue.conn.commit()
    decisions = await queue.reclaim_stale(max_attempts=3, now=_T0)
    assert decisions[0]["action"] == "dead_letter"
    row = queue.conn.execute("SELECT * FROM jobs WHERE id=?", (job["job_id"],)).fetchone()
    assert row["status"] == "failed" and "max_attempts_exceeded" in row["error"]
    # 死信任务不会再被认领
    assert await queue.dequeue(["test_execution"]) is None


async def test_cooperative_cancel_marking(queue):
    await queue.enqueue("test_execution", {}, "idem-1")
    assert await queue.request_cancel("idem-1") is True
    job = await queue.dequeue(["test_execution"])
    assert job["cancel_requested"] is True
    assert await queue.is_cancel_requested(job["job_id"]) is True
    # 终态任务不可再标记
    await queue.ack(job["job_id"])
    assert await queue.request_cancel("idem-1") is False
    assert await queue.request_cancel("unknown-key") is False


async def test_ack_and_fail_clear_lease(queue):
    await queue.enqueue("test_execution", {}, "idem-ok")
    await queue.enqueue("test_execution", {}, "idem-bad")
    ok = await queue.dequeue(["test_execution"])
    await queue.ack(ok["job_id"])
    row = queue.conn.execute("SELECT * FROM jobs WHERE id=?", (ok["job_id"],)).fetchone()
    assert row["status"] == "done" and row["lease_expires_at"] is None

    bad = await queue.dequeue(["test_execution"])
    await queue.fail(bad["job_id"], "boom")
    row = queue.conn.execute("SELECT * FROM jobs WHERE id=?", (bad["job_id"],)).fetchone()
    assert row["status"] == "failed" and row["error"] == "boom"
    assert row["lease_expires_at"] is None
