"""入库管线契约测试（docs/08 FR-CON-002 / FR-ING-001/002/003）。

对照实验覆盖：首灌 → 单篇变更只重处理该篇 → 删除级联 → 重跑幂等 → 坏文件隔离，
并验证 Swift 符号独立可召回、调用关系落库、URN 跨版本稳定。
"""

from __future__ import annotations

from types import SimpleNamespace

from testpilot_adapters.fs_docs import FileSystemDocsAdapter
from testpilot_adapters.store import bootstrap_store
from testpilot_adapters.tracers import NoopTracer
from testpilot_core.urn import parse_urn
from testpilot_workers.ingestion import run_ingestion

SWIFT_SRC = '''import Foundation

/// 协调器
final class MissionCoordinator {
    /// 调度主任务
    func schedulePrimary(notification: String, at date: Date) {
        self.cancelExisting()
        armTimer()
    }

    func cancelExisting() {}
}

func armTimer() {}
'''


def _container(tmp_path):
    docs_dir, src_dir, data_dir = tmp_path / "docs", tmp_path / "src", tmp_path / "data"
    docs_dir.mkdir()
    src_dir.mkdir()
    (docs_dir / "a.md").write_text("# 事故复盘\n\n## 根因\n响铃与调度冲突。\n", encoding="utf-8")
    (docs_dir / "b.md").write_text("# 侧栏兜底\n\nDISTINCTIVE_WORD_XYZ 的特殊说明。\n", encoding="utf-8")
    (src_dir / "MissionCoordinator.swift").write_text(SWIFT_SRC, encoding="utf-8")

    store = bootstrap_store(data_dir)
    docs_adapter = FileSystemDocsAdapter([docs_dir], code_roots=[src_dir])
    container = SimpleNamespace(
        get=lambda name: {
            "docs": docs_adapter, "store": store, "tracer": NoopTracer(),
        }.get(name),
        config=SimpleNamespace(project=SimpleNamespace(name="demo")),
    )
    return container, store, docs_adapter, docs_dir, src_dir


async def test_first_ingestion_indexes_chunks_symbols_and_relations(tmp_path):
    container, store, adapter, _, src = _container(tmp_path)
    report = await run_ingestion(container)

    assert report.status == "succeeded"
    assert report.created == 3 and report.failed == 0
    assert report.chunks_total >= 4  # 2 md + 类型/2 方法/全局函数等多个 swift 块

    # FR-ING-002 AC1：符号独立可召回（BM25 命中符号块）
    rows = store.search_fts("schedulePrimary", limit=10)
    assert rows, "schedulePrimary 应能被 FTS 召回"
    kinds = {r["kind"] for r in rows}
    assert "code_symbol" in kinds

    n_chunks = store.conn.execute("SELECT count(*) n FROM chunks").fetchone()["n"]
    n_symbols = store.conn.execute(
        "SELECT count(*) n FROM entities WHERE kind='code_symbol'"
    ).fetchone()["n"]
    n_calls = store.conn.execute(
        "SELECT count(*) n FROM relations WHERE kind='calls'"
    ).fetchone()["n"]
    assert n_symbols >= 3
    assert n_calls >= 2  # self.cancelExisting + armTimer

    # 状态表留痕
    stages = {r["stage"]: r["n"] for r in store.conn.execute(
        "SELECT stage, count(*) n FROM ingestion_status GROUP BY stage"
    ).fetchall()}
    assert stages.get("indexed") == 3

    # 加工批次落库
    run = store.conn.execute(
        "SELECT status, docs_created, chunks_total FROM ingestion_runs WHERE run_id=?",
        (report.run_id,),
    ).fetchone()
    assert run["status"] == "succeeded" and run["docs_created"] == 3


async def test_urn_stable_across_versions_and_single_doc_update(tmp_path):
    container, store, _, docs_dir, _ = _container(tmp_path)
    await run_ingestion(container)
    doc = store.get_document("urn:testpilot:fs-docs:document:docs/a.md")
    assert doc is not None
    first_version = doc.version

    (docs_dir / "a.md").write_text(
        "# 事故复盘\n\n## 根因\n响铃与调度冲突（补充：已验证于新版本）。\n", encoding="utf-8"
    )
    report = await run_ingestion(container)
    assert report.updated == 1 and report.unchanged == 2 and report.created == 0

    # 同一 URN、新版本 hash（文档身份不随内容变）
    again = store.get_document(doc.urn)
    assert again is not None and again.version != first_version
    parse_urn(doc.urn)  # URN 仍合法

    # 只重切该篇：旧切块被替换，无残留重复
    n = store.conn.execute(
        "SELECT count(*) n FROM chunks WHERE doc_urn=?", (doc.urn,)
    ).fetchone()["n"]
    n_fts = store.conn.execute(
        """SELECT count(*) n FROM chunks_fts f JOIN chunks c ON c.urn=f.chunk_urn
           WHERE c.doc_urn=?""",
        (doc.urn,),
    ).fetchone()["n"]
    assert n == n_fts  # FTS 与主表一致，无旧切块残留


async def test_deleted_file_cascades_from_index(tmp_path):
    container, store, _, docs_dir, _ = _container(tmp_path)
    await run_ingestion(container)
    assert store.search_fts("DISTINCTIVE_WORD_XYZ")

    (docs_dir / "b.md").unlink()
    report = await run_ingestion(container)
    assert report.deleted == 1 and report.unchanged == 2

    # FR-CON-002 AC2：删除事件而非残留索引
    assert not store.search_fts("DISTINCTIVE_WORD_XYZ")
    assert store.get_document("urn:testpilot:fs-docs:document:docs/b.md") is None
    evt = store.conn.execute(
        "SELECT event_type FROM events WHERE event_type='DocumentDeleted'"
    ).fetchall()
    assert evt
    assert store.conn.execute(
        "SELECT count(*) n FROM ingestion_status WHERE doc_urn=?",
        ("urn:testpilot:fs-docs:document:docs/b.md",),
    ).fetchone()["n"] == 0


async def test_rerun_is_idempotent(tmp_path):
    container, store, _, _, _ = _container(tmp_path)
    await run_ingestion(container)
    chunks_before = store.conn.execute("SELECT count(*) n FROM chunks").fetchone()["n"]
    fts_before = store.conn.execute("SELECT count(*) n FROM chunks_fts").fetchone()["n"]

    report = await run_ingestion(container)
    assert report.unchanged == 3 and report.updated == 0 and report.chunks_total == 0
    assert store.conn.execute("SELECT count(*) n FROM chunks").fetchone()["n"] == chunks_before
    assert store.conn.execute("SELECT count(*) n FROM chunks_fts").fetchone()["n"] == fts_before

    # --full 重切：数量不变、内容不变（同输入幂等）
    full = await run_ingestion(container, full=True, trigger="full")
    assert full.updated == 3 and full.failed == 0
    assert store.conn.execute("SELECT count(*) n FROM chunks").fetchone()["n"] == chunks_before


async def test_bad_file_isolated_to_dead_letter(tmp_path):
    container, store, _, docs_dir, _ = _container(tmp_path)
    (docs_dir / "broken.md").write_bytes(b"\xff\xfe\x00not-utf8-\xc3\x28")
    report = await run_ingestion(container)

    # 坏文档不阻断其他三篇
    assert report.status == "partial" and report.failed == 1
    assert report.created == 3
    failed = [d for d in report.details if d["outcome"] == "failed"]
    assert failed and failed[0]["stage"] == "scan"
    assert store.conn.execute("SELECT count(*) n FROM dead_letters").fetchone()["n"] == 1
    stage = store.conn.execute(
        "SELECT stage, error FROM ingestion_status WHERE doc_urn LIKE '%broken.md'"
    ).fetchone()
    assert stage["stage"] == "failed" and stage["error"]

    # 修复后重跑：失败状态被 indexed 覆盖，不残留失败标记
    (docs_dir / "broken.md").write_text("# 修复\n\n正常内容。\n", encoding="utf-8")
    report2 = await run_ingestion(container)
    assert report2.failed == 0
    assert store.conn.execute(
        "SELECT stage FROM ingestion_status WHERE doc_urn LIKE '%broken.md'"
    ).fetchone()["stage"] == "indexed"


def test_inventory_export_and_authority_overrides(tmp_path):
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "x.md").write_text("# x\n", encoding="utf-8")
    adapter = FileSystemDocsAdapter(
        [docs_dir],
        authority_overrides=[{"glob": "x.md", "level": "L1_authoritative"}],
    )
    inventory = adapter.inventory()
    assert len(inventory) == 1
    row = inventory[0]
    assert {"urn", "uri", "mtime", "content_hash", "authority_level", "kind"} <= set(row)
    assert row["authority_level"] == "L1_authoritative"


# ── S1.3：HEAD sha 透传 valid_from / scm 不可达不拖垮入库 ─────────────────────

from testpilot_core.chunking import MARKDOWN_CHUNKER_VERSION, SWIFT_CHUNKER_VERSION  # noqa: E402
from testpilot_core.errors import AdapterUnavailableError  # noqa: E402
from testpilot_core.models import RepoRef  # noqa: E402


def _container_with_scm(tmp_path, scm):
    container, store, _adapter, _docs_dir, _src_dir = _container(tmp_path)
    old_get = container.get
    docs = old_get("docs")
    repo = RepoRef(project="demo", repo_path=str(tmp_path))

    def get(name):
        if name == "scm":
            return scm
        if name == "repo":
            return repo
        return {
            "docs": docs, "store": store, "tracer": NoopTracer(),
        }.get(name)

    container.get = get
    return container, store


class _FakeScm:
    def __init__(self, sha="abc123deadbeef" * 3 + "aa"):
        self.sha = (sha[:40]).ljust(40, "0")

    async def head_sha(self, repo):
        return self.sha[:40]


class _DeadScm:
    async def head_sha(self, repo):
        raise AdapterUnavailableError("git 不可达：超时")


async def test_head_sha_threaded_into_valid_from_and_chunker_version(tmp_path):
    scm = _FakeScm()
    container, store = _container_with_scm(tmp_path, scm)
    report = await run_ingestion(container)

    assert report.head_commit == scm.sha[:40]
    rows = store.conn.execute(
        "SELECT kind, valid_from, raw_json FROM chunks"
    ).fetchall()
    assert rows and all(r["valid_from"] == scm.sha[:40] for r in rows)
    import json
    for r in rows:
        version = json.loads(r["raw_json"])["metadata"]["chunker_version"]
        expected = SWIFT_CHUNKER_VERSION if r["kind"] == "code_symbol" else MARKDOWN_CHUNKER_VERSION
        assert version == expected


async def test_unchanged_rerun_keeps_valid_from_but_head_moves(tmp_path):
    scm = _FakeScm()
    container, store = _container_with_scm(tmp_path, scm)
    first = await run_ingestion(container)

    # HEAD 前进但文件内容没变：unchanged 不重切，valid_from 保持首灌 sha（不写放大）
    scm.sha = "f" * 40
    report = await run_ingestion(container)
    assert report.unchanged == 3 and report.created == 0
    row = store.conn.execute("SELECT DISTINCT valid_from v FROM chunks").fetchall()
    assert [r["v"] for r in row] == [first.head_commit]


async def test_scm_unavailable_does_not_fail_run(tmp_path):
    container, store = _container_with_scm(tmp_path, _DeadScm())
    report = await run_ingestion(container)

    assert report.status == "succeeded" and report.head_commit is None
    assert report.created == 3  # 文档照常入库
    assert any(d.get("outcome") == "head_unavailable" for d in report.details)
    # 死信留痕
    dead = store.conn.execute("SELECT count(*) n FROM dead_letters").fetchone()["n"]
    assert dead == 1
    # valid_from 留空（无版本可标），但不阻断
    n_null = store.conn.execute(
        "SELECT count(*) n FROM chunks WHERE valid_from IS NULL"
    ).fetchone()["n"]
    assert n_null > 0
