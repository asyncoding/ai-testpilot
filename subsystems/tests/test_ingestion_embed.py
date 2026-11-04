"""入库管线向量嵌入阶段契约（S1.2，docs/08 FR-ING 嵌入补偿 + FR-KNO 降级）。

对照实验覆盖：
- 首灌 → 全部文档切块批量 embed 并 upsert，ingestion_status 留痕（模型/维度/耗时）；
- unchanged 重跑 → 一次模型都不调（幂等省钱，fake 调用计数为证）；
- 删文档 → 向量同步级联删；改文档标题 → 旧 anchor 向量孤儿被清；
- embed 失败 → 单文档进死信、embed_stage='failed'、整批 partial；
  模型恢复后普通重跑（无需 --full）自动补偿；
- 向量库整体不可用 → 内容加工照常成功、嵌入逐文档失败隔离，FTS 不被拖垮；
- 离线 profile（无 model/vectors 适配器）→ 嵌入阶段整体跳过。
"""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

from testpilot_adapters.fs_docs import FileSystemDocsAdapter
from testpilot_adapters.store import bootstrap_store
from testpilot_adapters.tracers import NoopTracer
from testpilot_core.models import VectorItem
from testpilot_workers import ingestion as ing_mod
from testpilot_workers.ingestion import run_ingestion

DOC_A = "# 事故复盘\n\n## 根因\n响铃与调度冲突。\n"
DOC_B = "# 侧栏兜底\n\nDISTINCTIVE_WORD_XYZ 的特殊说明。\n"
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


class FakeEmbedModel:
    """记录调用次数与每批大小的假嵌入网关；8 维确定性向量。"""

    def __init__(self, *, fail_text: str | None = None):
        self.embed_model = "fake-embed-8d"
        self.calls = 0
        self.batch_sizes: list[int] = []
        self.fail_text = fail_text

    async def embed(self, texts):
        self.calls += 1
        self.batch_sizes.append(len(texts))
        if self.fail_text and any(self.fail_text in t for t in texts):
            raise RuntimeError("model boom")
        vectors = []
        for text in texts:
            digest = hashlib.sha256(text.encode("utf-8")).digest()
            vectors.append([(b - 128) / 128.0 for b in digest[:8]])
        return vectors


class FakeVectors:
    """内存版 VectorStorePort：按 chunk URN 存，记录 upsert/delete。"""

    def __init__(self, *, fail_doc_urn: str | None = None, always_fail: bool = False):
        self.items: dict[str, VectorItem] = {}
        self.deleted: list[str] = []
        self.upsert_calls = 0
        self.fail_doc_urn = fail_doc_urn
        self.always_fail = always_fail

    async def healthcheck(self):
        return not self.always_fail

    async def upsert(self, items):
        self.upsert_calls += 1
        for it in items:
            if self.always_fail or it.metadata_.get("doc_urn") == self.fail_doc_urn:
                raise RuntimeError("vector store boom")
        for it in items:
            self.items[it.id] = it

    async def query(self, vector, top_k, where=None):
        return []

    async def delete(self, ids):
        for i in ids:
            self.deleted.append(i)
            self.items.pop(i, None)

    def ids_for_doc(self, doc_urn: str) -> set[str]:
        return {i for i, it in self.items.items()
                if it.metadata_.get("doc_urn") == doc_urn}


def _container(tmp_path, *, model=None, vectors=None):
    docs_dir, src_dir, data_dir = tmp_path / "docs", tmp_path / "src", tmp_path / "data"
    docs_dir.mkdir()
    src_dir.mkdir()
    (docs_dir / "a.md").write_text(DOC_A, encoding="utf-8")
    (docs_dir / "b.md").write_text(DOC_B, encoding="utf-8")
    (src_dir / "MissionCoordinator.swift").write_text(SWIFT_SRC, encoding="utf-8")

    store = bootstrap_store(data_dir)
    docs_adapter = FileSystemDocsAdapter([docs_dir], code_roots=[src_dir])
    container = SimpleNamespace(
        get=lambda name: {
            "docs": docs_adapter, "store": store, "tracer": NoopTracer(),
            "model": model, "vectors": vectors,
        }.get(name),
        config=SimpleNamespace(project=SimpleNamespace(name="demo")),
    )
    return container, store, docs_dir, src_dir


def _embed_status(store, suffix: str) -> dict:
    row = store.conn.execute(
        """SELECT embed_stage, embed_model, embed_dim, embed_latency_ms, embed_error
           FROM ingestion_status WHERE doc_urn LIKE ?""",
        (f"%{suffix}",),
    ).fetchone()
    assert row is not None
    return dict(row)


async def test_first_ingestion_embeds_all_docs_with_status_trace(tmp_path):
    model, vectors = FakeEmbedModel(), FakeVectors()
    container, store, _, _ = _container(tmp_path, model=model, vectors=vectors)

    report = await run_ingestion(container)
    assert report.status == "succeeded"
    assert report.embedded == 3 and report.embed_failed == 0
    assert model.calls >= 1

    # 向量条数 == sqlite 切块数，id 全部用 chunk URN
    n_chunks = store.conn.execute("SELECT count(*) n FROM chunks").fetchone()["n"]
    assert len(vectors.items) == n_chunks == report.chunks_total
    chunk_urns = {r["urn"] for r in store.conn.execute("SELECT urn FROM chunks")}
    assert set(vectors.items) == chunk_urns

    # metadata 白名单字段随向量落库（检索过滤/展示用）
    sample = next(iter(vectors.items.values()))
    assert {"doc_urn", "kind", "branch", "acl_scope", "content_hash",
            "doc_content_hash", "doc_uri", "quality"} <= set(sample.metadata_)
    assert sample.embedding and len(sample.embedding) == 8

    # ingestion_status 嵌入留痕：模型版本/维度/耗时
    for suffix in ("docs/a.md", "docs/b.md", "MissionCoordinator.swift"):
        st = _embed_status(store, suffix)
        assert st["embed_stage"] == "embedded"
        assert st["embed_model"] == "fake-embed-8d"
        assert st["embed_dim"] == 8 and st["embed_latency_ms"] >= 0
        assert st["embed_error"] == ""

    detail = [d for d in report.details if d.get("outcome") == "embedded"]
    assert len(detail) == 3 and all(d["dim"] == 8 for d in detail)


async def test_unchanged_rerun_never_calls_model(tmp_path):
    model, vectors = FakeEmbedModel(), FakeVectors()
    container, store, _, _ = _container(tmp_path, model=model, vectors=vectors)
    await run_ingestion(container)
    calls_after_first = model.calls
    upserts_after_first = vectors.upsert_calls
    ids_after_first = dict(vectors.items)

    report = await run_ingestion(container)
    assert report.unchanged == 3 and report.embedded == 0 and report.embed_failed == 0
    assert model.calls == calls_after_first  # 一次模型都不再调
    assert vectors.upsert_calls == upserts_after_first  # 也不再写向量库
    assert set(vectors.items) == set(ids_after_first)


async def test_delete_cascades_to_vector_store(tmp_path):
    model, vectors = FakeEmbedModel(), FakeVectors()
    container, store, docs_dir, _ = _container(tmp_path, model=model, vectors=vectors)
    await run_ingestion(container)

    b_urn = "urn:testpilot:fs-docs:document:docs/b.md"
    b_ids = vectors.ids_for_doc(b_urn)
    assert b_ids

    (docs_dir / "b.md").unlink()
    report = await run_ingestion(container)
    assert report.deleted == 1 and report.vector_deleted_docs == 1
    assert vectors.ids_for_doc(b_urn) == set()
    assert set(vectors.deleted) >= b_ids  # 删除请求带上了全部旧切块 URN


async def test_updated_doc_orphan_vectors_are_reaped(tmp_path):
    model, vectors = FakeEmbedModel(), FakeVectors()
    container, store, docs_dir, _ = _container(tmp_path, model=model, vectors=vectors)
    await run_ingestion(container)
    a_urn = "urn:testpilot:fs-docs:document:docs/a.md"
    old_ids = vectors.ids_for_doc(a_urn)

    # 改标题 → heading slug 变 → chunk URN 变（旧 anchor 成孤儿）
    (docs_dir / "a.md").write_text(
        "# 事故复盘\n\n## 根因分析\n响铃与调度冲突（补充：已验证于新版本）。\n",
        encoding="utf-8",
    )
    report = await run_ingestion(container)
    assert report.updated == 1 and report.embed_failed == 0

    new_ids = set(store.list_chunk_urns(a_urn))
    assert vectors.ids_for_doc(a_urn) == new_ids  # 无孤儿、无缺失
    assert old_ids - new_ids  # 确实发生过 anchor 变更
    assert set(vectors.deleted) >= (old_ids - new_ids)
    embedded = [d for d in report.details
                if d.get("outcome") == "embedded" and d["urn"] == a_urn]
    assert embedded and embedded[0]["orphans_deleted"] == len(old_ids - new_ids)


async def test_embed_failure_dead_letter_then_backfill_without_full(tmp_path):
    # b.md 嵌入必失败；另外两篇正常
    model, vectors = FakeEmbedModel(fail_text="DISTINCTIVE_WORD_XYZ"), FakeVectors()
    container, store, _, _ = _container(tmp_path, model=model, vectors=vectors)

    report = await run_ingestion(container)
    assert report.status == "partial" and report.embed_failed == 1
    assert report.embedded == 2
    b_urn = "urn:testpilot:fs-docs:document:docs/b.md"
    assert vectors.ids_for_doc(b_urn) == set()  # 失败文档不写半成品向量

    st = _embed_status(store, "docs/b.md")
    assert st["embed_stage"] == "failed" and "model boom" in st["embed_error"]
    dl = store.conn.execute(
        "SELECT source, payload_json FROM dead_letters WHERE source='ingestion-embed'"
    ).fetchall()
    assert len(dl) == 1 and b_urn in dl[0]["payload_json"]

    # 内容加工仍成功：FTS 照常可召回（向量降级不拖垮主链路）
    assert store.search_fts("DISTINCTIVE_WORD_XYZ")

    # 模型恢复，普通重跑（内容 unchanged、非 --full）→ 失败补偿
    model.fail_text = None
    report2 = await run_ingestion(container)
    assert report2.status == "succeeded"
    assert report2.embedded == 1 and report2.embed_failed == 0
    # 三篇内容全 unchanged（b.md 内容 hash 未变）——补偿靠 pending-embed
    # 列表而非变更对账，这正是"无需 --full 自动补偿"的证据
    assert report2.unchanged == 3 and report2.updated == 0
    assert vectors.ids_for_doc(b_urn)
    assert _embed_status(store, "docs/b.md")["embed_stage"] == "embedded"


async def test_vector_store_down_isolates_embed_but_indexing_survives(tmp_path):
    model, vectors = FakeEmbedModel(), FakeVectors(always_fail=True)
    container, store, _, _ = _container(tmp_path, model=model, vectors=vectors)

    report = await run_ingestion(container)
    assert report.status == "partial"
    assert report.created == 3 and report.embed_failed == 3
    assert report.chunks_total > 0
    # 死信逐文档可追，FTS 数据完整 → 检索可降级为 BM25-only
    n_dl = store.conn.execute(
        "SELECT count(*) n FROM dead_letters WHERE source='ingestion-embed'"
    ).fetchone()["n"]
    assert n_dl == 3
    assert store.search_fts("schedulePrimary")


async def test_embed_respects_batch_size(tmp_path, monkeypatch):
    monkeypatch.setattr(ing_mod, "_EMBED_BATCH", 1)
    model, vectors = FakeEmbedModel(), FakeVectors()
    container, store, _, _ = _container(tmp_path, model=model, vectors=vectors)

    report = await run_ingestion(container)
    assert report.status == "succeeded"
    # 批大小 1 → 调用次数恰等于切块总数
    assert model.calls == report.chunks_total
    assert all(size == 1 for size in model.batch_sizes)


async def test_offline_profile_without_model_and_vectors_skips_embed(tmp_path):
    container, store, _, _ = _container(tmp_path)  # model=None, vectors=None
    report = await run_ingestion(container)
    assert report.status == "succeeded"
    assert report.embedded == 0 and report.embed_failed == 0
    assert report.vector_deleted_docs == 0
    # 内容入库一切照常
    assert store.search_fts("schedulePrimary")
