"""CommitPushed 保鲜服务 hermetic 测试（FR-FRS-002，S4.2）。

tempfile 真 sqlite + 合成 Swift 语料 + fake scm/docs，不起端口、不碰真实仓：
- rename 配对 → STALE → 高价值 reindex → rename 链补挂（AC1/AC2 机制）
- 低价值 lazy 且 stale 切块在检索通道不可见（AC3）
- 文档缺失跳过、未配对 removed、引用度高价值、缺 base 响亮失败
- 经 event_dispatch 端到端派发 + 重放幂等 + handler 异常进死信
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from testpilot_adapters.fts_search import FtsSearchAdapter
from testpilot_adapters.json_events import JsonReplayEventSource
from testpilot_adapters.store import bootstrap_store
from testpilot_adapters.tracers import NoopTracer
from testpilot_core.enums import AuthorityLevel, ChangeType
from testpilot_core.errors import AdapterUnavailableError
from testpilot_core.models import (
    ChangeSet,
    EventEnvelope,
    KnowledgeChunk,
    RepoRef,
    SourceDocument,
    SymbolChange,
)
from testpilot_core.urn import make_urn
from testpilot_workers.event_dispatch import (
    dispatch_events,
    register_handler,
    reset_handlers,
)
from testpilot_workers.freshness import handle_commit_pushed
from testpilot_workers.handler_wiring import wire_default_handlers

_SOURCE = "fs-docs"
GIT_PATH = "Sources/Alarm.swift"
EXT = f"code/{GIT_PATH}"
DOC_URN = make_urn(_SOURCE, "document", EXT)
BASE = "1" * 40
HEAD = "2" * 40
OLD_ANCHOR = "AlarmScheduler.schedulePrimary()"
NEW_ANCHOR = "AlarmScheduler.schedulePrimaryV2()"
LEGACY_ANCHOR = "AlarmScheduler.legacyAlarmBell()"

BASE_SWIFT = """final class AlarmScheduler {
  /// 主调度入口
  func schedulePrimary() {
    legacyAlarmBell()
  }
  func legacyAlarmBell() { print("ring") }
}
"""
HEAD_SWIFT = """final class AlarmScheduler {
  /// 主调度入口（V2）
  func schedulePrimaryV2() {
    legacyAlarmBell()
  }
  func legacyAlarmBell() { print("ring") }
}
"""


def _symbol_urn(anchor: str) -> str:
    return make_urn(_SOURCE, "symbol", f"{EXT}#{anchor}")


def _chunk_urn(anchor: str) -> str:
    return make_urn(_SOURCE, "chunk", f"{EXT}#{anchor}")


OLD_URN = _symbol_urn(OLD_ANCHOR)
NEW_URN = _symbol_urn(NEW_ANCHOR)


class _Config:
    def __init__(self, freshness=None):
        self.freshness = freshness or {}


class _FakeDocs:
    def __init__(self, *, text=BASE_SWIFT, missing: set[str] | None = None):
        self._text = text
        self._missing = missing or set()

    def read_content(self, urn: str) -> str:
        if urn in self._missing:
            raise AdapterUnavailableError(f"文档已不存在: {urn}")
        return self._text


class _FakeScm:
    def __init__(self, change_set: ChangeSet, head_sha: str = HEAD):
        self._cs = change_set
        self._head = head_sha

    async def head_sha(self, repo):
        return self._head

    async def get_change_set(self, repo, base, head):
        return self._cs


class _Container:
    def __init__(self, store, scm, docs, *, config=None, event_source=None):
        self.config = config or _Config()
        self.adapters = {
            "store": store,
            "tracer": NoopTracer(),
            "scm": scm,
            "repo": RepoRef(project="demo", repo_path="/tmp/demo-repo",
                            default_branch="main"),
            "docs": docs,
        }
        if event_source is not None:
            self.adapters["ci_events"] = event_source

    def get(self, name: str):
        return self.adapters.get(name)


def _seed_doc(store, *, quality: float = 0.9) -> None:
    store.upsert_document(SourceDocument(
        urn=DOC_URN, source_system=_SOURCE, uri="file:///repo/Alarm.swift",
        title="Alarm", version="v1", content_hash="h1",
        authority_level=AuthorityLevel.L1_AUTHORITATIVE, acl_scope="default",
        metadata_={"kind_hint": "swift"},
    ))
    for idx, anchor in enumerate(
        ("AlarmScheduler", OLD_ANCHOR, LEGACY_ANCHOR)
    ):
        # 文本只含本切块锚点词：stale 抑制断言才能用唯一 token 定位单块
        store.upsert_chunk(KnowledgeChunk(
            urn=_chunk_urn(anchor), doc_urn=DOC_URN, index=idx,
            kind="code_symbol",
            text=f"func {anchor} implementation body {anchor}",
            symbol_urn=_symbol_urn(anchor), quality_score=quality,
            valid_from=BASE, content_hash=f"ch-{idx}",
        ))


def _rename_changeset() -> ChangeSet:
    return ChangeSet(
        repo=RepoRef(project="demo", repo_path="/tmp/demo-repo"),
        base_commit=BASE, head_commit=HEAD,
        changed_files=[GIT_PATH],
        symbol_changes=[
            SymbolChange(symbol_id=f"{GIT_PATH}#{OLD_ANCHOR}",
                         change_type=ChangeType.REMOVED),
            SymbolChange(symbol_id=f"{GIT_PATH}#{NEW_ANCHOR}",
                         change_type=ChangeType.ADDED),
        ],
    )


def _event(event_id: str = "evt-1", **payload_overrides) -> EventEnvelope:
    payload = {"base_commit": BASE, "head_commit": HEAD}
    payload.update(payload_overrides)
    return EventEnvelope(
        event_id=event_id, event_type="CommitPushed",
        source_system="json-git",
        occurred_at=datetime(2026, 11, 11, 8, 0, tzinfo=timezone.utc),
        project="demo", subject_ref=HEAD,
        idempotency_key=f"idem-{event_id}", payload=payload,
    )


def _event_reindex_runs(store) -> int:
    return store.conn.execute(
        "SELECT COUNT(*) n FROM ingestion_runs WHERE trigger='event-reindex'"
    ).fetchone()["n"]


@pytest.fixture(autouse=True)
def _clean_handlers():
    yield
    reset_handlers()


# ── 主路径：selector rename → STALE → reindex → 补链 ─────────────────────────

async def test_rename_marks_stale_reindexes_and_closes_chain(tmp_path):
    store = bootstrap_store(tmp_path / "data")
    _seed_doc(store)
    container = _Container(store, _FakeScm(_rename_changeset()),
                           _FakeDocs(text=HEAD_SWIFT))

    report = await handle_commit_pushed(container, _event())

    assert report.removed == 1 and report.added == 1
    assert report.stale_rows_marked >= 1
    assert len(report.renamed_pairs) == 1
    assert report.renamed_pairs[0]["reason"] == "selector_rename"
    assert report.renamed_pairs[0]["resolved"] is True
    assert report.rename_relations_new == 1
    assert len(report.reindexed) == 1
    assert report.reindexed[0]["chunks"] == 3  # head 文本重切：类+两方法
    assert not report.unresolved_anchors
    # reindex 后全量回 fresh，rename 链跨级联存活（relations.doc_urn 恒 NULL）
    assert store.count_stale_chunks() == 0
    assert store.get_rename_target(OLD_URN) == NEW_URN

    # AC2：旧名提问仍能命中，且命中的是含新符号名的 fresh 切块
    hits = await FtsSearchAdapter(store, project="demo").search(
        "schedulePrimary", project="demo")
    assert hits, "旧名问题在 rename 后应通过新切块召回"
    assert all("schedulePrimaryV2" in c.text for c in hits)
    assert _event_reindex_runs(store) == 1


async def test_lazy_low_value_doc_marks_stale_without_reindex(tmp_path):
    store = bootstrap_store(tmp_path / "data")
    _seed_doc(store, quality=0.2)
    config = _Config(freshness={
        "high_value_quality": 0.95, "high_value_min_refs": 5,
    })
    container = _Container(
        store, _FakeScm(_rename_changeset()), _FakeDocs(), config=config,
    )

    report = await handle_commit_pushed(container, _event())

    assert report.lazy_docs == [DOC_URN]
    assert report.reindexed == []
    assert _event_reindex_runs(store) == 0
    assert store.count_stale_chunks() >= 1

    # AC3：stale 切块在真实检索通道不可见；显式 include_stale 才能查到
    adapter = FtsSearchAdapter(store, project="demo")
    assert await adapter.search("schedulePrimary", project="demo") == []
    raw = store.search_fts_matches(
        "schedulePrimary", [], include_stale=True, branch="main",
        acl_scope="default",
    )
    assert raw and all(r["urn"] for r in raw)


async def test_missing_doc_is_skipped_not_dead_lettered(tmp_path):
    store = bootstrap_store(tmp_path / "data")
    _seed_doc(store)
    docs = _FakeDocs(missing={DOC_URN})
    container = _Container(store, _FakeScm(_rename_changeset()), docs)

    report = await handle_commit_pushed(container, _event())

    assert report.reindex_skipped_missing == [DOC_URN]
    assert report.reindexed == []
    # STALE 已先于 reindex 落账：检索安全不依赖 reindex 成功
    assert store.count_stale_chunks() >= 1
    assert _event_reindex_runs(store) == 0  # 缺失文档不开批次账


async def test_unpaired_removed_marks_stale_without_relation(tmp_path):
    store = bootstrap_store(tmp_path / "data")
    _seed_doc(store)
    changeset = ChangeSet(
        repo=RepoRef(project="demo", repo_path="/tmp/demo-repo"),
        base_commit=BASE, head_commit=HEAD, changed_files=[GIT_PATH],
        symbol_changes=[
            SymbolChange(symbol_id=f"{GIT_PATH}#{OLD_ANCHOR}",
                         change_type=ChangeType.REMOVED),
        ],
    )
    # lazy 配置：不触发 reindex，STALE 行保留可直接核验
    config = _Config(freshness={
        "high_value_quality": 0.95, "high_value_min_refs": 5,
    })
    container = _Container(store, _FakeScm(changeset), _FakeDocs(),
                           config=config)

    report = await handle_commit_pushed(container, _event())

    assert report.renamed_pairs == []
    assert report.rename_relations_new == 0
    assert report.stale_rows_marked >= 1
    assert store.count_stale_chunks() >= 1
    assert report.lazy_docs == [DOC_URN]
    # 锚点在库内解析成功：只是没有 added 侧可配对，不属于未解析挂账
    assert not report.unresolved_anchors


async def test_reference_count_alone_makes_doc_high_value(tmp_path):
    store = bootstrap_store(tmp_path / "data")
    _seed_doc(store, quality=0.2)
    # 两条 calls 入边：引用度达默认阈值 2，质量分低也立即 reindex
    for i in range(2):
        store.conn.execute(
            "INSERT INTO relations(urn, source_urn, target_urn, kind,"
            " confidence, evidence_json, doc_urn)"
            " VALUES (?,?,?, 'calls', 0.9, '[]', NULL)",
            (f"urn:testpilot:fs-docs:rel:call-{i}",
             f"urn:testpilot:fs-docs:symbol:caller-{i}", OLD_URN),
        )
    store.conn.commit()
    container = _Container(store, _FakeScm(_rename_changeset()),
                           _FakeDocs(text=HEAD_SWIFT))

    report = await handle_commit_pushed(container, _event())

    assert report.reindexed and report.reindexed[0]["doc_urn"] == DOC_URN
    assert report.lazy_docs == []


async def test_missing_base_commit_raises_loudly(tmp_path):
    store = bootstrap_store(tmp_path / "data")
    _seed_doc(store)
    container = _Container(store, _FakeScm(_rename_changeset()), _FakeDocs())

    with pytest.raises(ValueError, match="base_commit"):
        await handle_commit_pushed(container, _event(base_commit=""))


# ── 经 event_dispatch 的端到端行为 ──────────────────────────────────────────

def _write_event(events_dir: Path, name: str, data: dict) -> None:
    (events_dir / f"{name}.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8")


async def test_dispatch_routes_freshness_and_replay_is_idempotent(tmp_path):
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    _write_event(events_dir, "e0", _event().model_dump(mode="json"))

    store = bootstrap_store(tmp_path / "data")
    _seed_doc(store)
    container = _Container(
        store, _FakeScm(_rename_changeset()), _FakeDocs(text=HEAD_SWIFT),
        event_source=JsonReplayEventSource(events_dir),
    )
    register_handler("CommitPushed", handle_commit_pushed)

    first = await dispatch_events(container)
    second = await dispatch_events(container)

    assert first.new == 1 and first.processed == 1 and first.dead == 0
    assert second.replayed == 1 and second.processed == 0
    # handler 不重跑：批次账与 rename 链都不增生
    assert _event_reindex_runs(store) == 1
    relation_count = store.conn.execute(
        "SELECT COUNT(*) n FROM relations WHERE kind='renamed_to'"
    ).fetchone()["n"]
    assert relation_count == 1
    assert store.get_rename_target(OLD_URN) == NEW_URN


async def test_handler_failure_turns_into_handler_error_dead_letter(tmp_path):
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    _write_event(events_dir, "bad",
                 _event("evt-bad", base_commit="").model_dump(mode="json"))

    store = bootstrap_store(tmp_path / "data")
    _seed_doc(store)
    container = _Container(
        store, _FakeScm(_rename_changeset()), _FakeDocs(),
        event_source=JsonReplayEventSource(events_dir),
    )
    register_handler("CommitPushed", handle_commit_pushed)

    report = await dispatch_events(container)

    assert report.dead == 1
    assert report.dead_reasons == {"handler_error": 1}
    assert store.list_events(status="dead")[0]["event_id"] == "evt-bad"
    assert store.list_dead_letters()[0]["source"] == "event-dispatch:handler_error"


def test_wire_default_handlers_registers_commit_pushed():
    from testpilot_workers import event_dispatch

    wire_default_handlers()
    assert event_dispatch._HANDLERS["CommitPushed"] is handle_commit_pushed
