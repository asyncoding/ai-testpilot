"""SQLite 队列测试：幂等去重、领取、ack/fail（docs/07 §3 QueuePort）。"""

import pytest

from testpilot_adapters.store import SqliteQueue, bootstrap_store


async def test_enqueue_dequeue_ack(tmp_path):
    store = bootstrap_store(tmp_path)
    queue = SqliteQueue(store.conn)
    try:
        jid1 = await queue.enqueue("run-tests", {"k": 1}, "key-1")
        jid2 = await queue.enqueue("run-tests", {"k": 2}, "key-1")  # 同幂等键
        assert jid1 == jid2

        job = await queue.dequeue(["run-tests"])
        assert job and job["payload"] == {"k": 1}
        assert await queue.dequeue(["run-tests"]) is None  # 不重复领取
        await queue.ack(job["job_id"])

        job3 = await queue.enqueue("ingest", {}, "key-3")
        assert await queue.dequeue(["run-tests"]) is None  # 类型不匹配
        got = await queue.dequeue(["ingest"])
        assert got and got["job_id"] == str(job3)
        await queue.fail(got["job_id"], "boom")
        row = store.conn.execute("SELECT status, error FROM jobs WHERE id=?", (got["job_id"],)).fetchone()
        assert row["status"] == "failed" and row["error"] == "boom"
    finally:
        store.close()
