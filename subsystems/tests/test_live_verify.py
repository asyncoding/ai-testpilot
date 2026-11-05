"""LiveVerifier 测试（docs/08 FR-QA-002，S1.3）。

真 tmp git 仓 + bootstrap Store：索引侧证据手工精确写入（不经过入库管线），
才能构造 stale-index / 版本漂移 / 删除符号这些真实重灌难以稳定复现的场景。
核心对照实验（FR-QA-002 AC1）：索引写入旧 commit 的函数体、不重灌，
live_verify 当场读 HEAD 判 body_changed=True；把 content_hash 更新成 HEAD 后
同 URN 再验 → verified=True（假绿抓不住这个翻转）。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from git_helpers import GIT_BIN, HEAD_B_SWIFT, ROOT_A_SWIFT, build_demo_repo
from testpilot_adapters.git_repo import GitCodeRepository
from testpilot_adapters.live_verify import LiveVerifier
from testpilot_adapters.store.sqlite_store import bootstrap_store
from testpilot_core.chunking import (
    MARKDOWN_CHUNKER_VERSION,
    SWIFT_CHUNKER_VERSION,
    chunk_markdown,
    chunk_swift,
    select_chunk_by_anchor,
)
from testpilot_core.errors import AdapterUnavailableError
from testpilot_core.models import KnowledgeChunk, RepoRef, SourceDocument
from testpilot_core.urn import make_urn

pytestmark = pytest.mark.skipif(GIT_BIN is None, reason="环境无 git 二进制")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.fixture
def world(tmp_path: Path):
    cwd, root, head = build_demo_repo(tmp_path / "repo-parent")
    store = bootstrap_store(tmp_path / "data")
    repo = RepoRef(project="demo", repo_path=str(cwd), default_branch="main")
    scm = GitCodeRepository(repo, git_path=GIT_BIN)
    verifier = LiveVerifier(scm, store, repo, project="demo")
    return type("W", (), {
        "cwd": cwd, "root": root, "head": head,
        "store": store, "repo": repo, "verifier": verifier,
    })()


def _index(
    world, git_rel: str, anchor: str, text: str, *,
    kind: str = "code_symbol", branch: str = "main",
    valid_from: str | None = None, chunker_version: str | None = SWIFT_CHUNKER_VERSION,
    symbol_urn: bool = False, idx: int = 0, uri: str | None = None,
) -> str:
    doc_urn = make_urn("fs-docs", "document", git_rel)
    world.store.upsert_document(SourceDocument(
        urn=doc_urn, source_system="fs-docs",
        uri=uri or f"file://{world.cwd / git_rel}",
        content_hash=_sha(f"doc-{git_rel}"), branch=branch,
    ))
    external = f"{git_rel}#{anchor}"
    urn = make_urn("fs-docs", "symbol" if symbol_urn else "chunk", external)
    chunk_urn = make_urn("fs-docs", "chunk", external)
    world.store.upsert_chunk(KnowledgeChunk(
        urn=chunk_urn, doc_urn=doc_urn, index=idx, kind=kind, text=text,
        symbol_urn=urn if symbol_urn else None,
        content_hash=_sha(text), branch=branch, valid_from=valid_from,
        metadata={"chunker_version": chunker_version} if chunker_version else {},
    ))
    return urn if symbol_urn else chunk_urn


def _events(world) -> list[dict]:
    rows = world.store.conn.execute(
        "SELECT * FROM verification_events ORDER BY id"
    ).fetchall()
    return [dict(r) for r in rows]


# ── 正常路径 ────────────────────────────────────────────────────────────────

async def test_head_unchanged_verifies(world):
    fresh = select_chunk_by_anchor(chunk_swift(HEAD_B_SWIFT), "C.fresh()")
    urn = _index(world, "Sources/B.swift", "C.fresh()", fresh.text,
                 valid_from=world.head)
    r = await world.verifier.verify(urn)
    assert r.verified is True and r.exists is True
    assert r.body_changed is False and r.reason == "unchanged"
    assert r.head_commit == world.head and r.requested_commit == ""
    assert r.current_chunker_version == SWIFT_CHUNKER_VERSION
    assert r.indexed_chunker_version == SWIFT_CHUNKER_VERSION
    assert r.indexed_commit == world.head and r.degraded is False
    assert r.kind == "code_symbol" and r.latency_ms >= 0


async def test_symbol_urn_also_resolves(world):
    fresh = select_chunk_by_anchor(chunk_swift(HEAD_B_SWIFT), "C.fresh()")
    urn = _index(world, "Sources/B.swift", "C.fresh()", fresh.text,
                 valid_from=world.head, symbol_urn=True)
    r = await world.verifier.verify(urn)
    assert r.verified is True


async def test_old_commit_explicitly_requested(world):
    # 指定 commit=root：读 A.swift@root，索引内容也是 root → 仍 verified
    keep_root = select_chunk_by_anchor(chunk_swift(ROOT_A_SWIFT), "C.keep()")
    urn = _index(world, "Sources/A.swift", "C.keep()", keep_root.text,
                 valid_from=world.root)
    r = await world.verifier.verify(urn, world.root)
    assert r.verified is True and r.reason == "unchanged"
    assert r.requested_commit == world.root


async def test_markdown_anchor_verifies(world):
    body = "# 指南\n\n这是告警指南正文。\n"
    guide = select_chunk_by_anchor(chunk_markdown(body), "指南")
    assert guide is not None  # 前置：带正文标题产出锚点为「指南」的文本块
    urn = _index(world, "Docs/guide.md", "指南", guide.text, kind="text",
                 chunker_version=MARKDOWN_CHUNKER_VERSION,
                 valid_from=world.head)
    r = await world.verifier.verify(urn)
    assert r.verified is True
    assert r.current_chunker_version == MARKDOWN_CHUNKER_VERSION


# ── FR-QA-002 AC1：stale-index 当场抓现行 ───────────────────────────────────

async def test_stale_index_body_changed_then_reindex_verifies(world):
    # 索引停在 root：C.keep() 旧函数体（interval=90），文件路径是 HEAD 的 B.swift
    keep_root = select_chunk_by_anchor(chunk_swift(ROOT_A_SWIFT), "C.keep()")
    keep_head = select_chunk_by_anchor(chunk_swift(HEAD_B_SWIFT), "C.keep()")
    assert keep_root.text != keep_head.text  # 前置：两版函数体确实不同
    urn = _index(world, "Sources/B.swift", "C.keep()", keep_root.text,
                 valid_from=world.root)

    stale = await world.verifier.verify(urn)
    assert stale.verified is False and stale.body_changed is True
    assert stale.exists is True and stale.reason == "body_changed"

    # 模拟重灌：content_hash 刷成 HEAD 版本（不重建 Store，同 URN）
    world.store.upsert_chunk(KnowledgeChunk(
        urn=urn, doc_urn=make_urn("fs-docs", "document", "Sources/B.swift"),
        index=0, kind="code_symbol", text=keep_head.text,
        content_hash=_sha(keep_head.text), branch="main",
        valid_from=world.head,
        metadata={"chunker_version": SWIFT_CHUNKER_VERSION},
    ))
    fresh = await world.verifier.verify(urn)
    assert fresh.verified is True and fresh.body_changed is False


async def test_symbol_deleted_is_missing_not_degraded(world):
    urn = _index(world, "Sources/B.swift", "C.ghost()", "func ghost() {}\n",
                 valid_from=world.head)
    r = await world.verifier.verify(urn)
    assert r.exists is False and r.verified is False
    assert r.body_changed is None and r.reason == "symbol_missing"
    assert r.degraded is False  # 仓库可达、符号确实没了：事实结论，不是降级


async def test_file_missing_at_commit_is_symbol_missing(world):
    # B.swift 在 root 不存在 → PathMissingAtCommit 内部归因为 symbol_missing
    fresh = select_chunk_by_anchor(chunk_swift(HEAD_B_SWIFT), "C.fresh()")
    urn = _index(world, "Sources/B.swift", "C.fresh()", fresh.text)
    r = await world.verifier.verify(urn, world.root)
    assert r.reason == "symbol_missing" and r.exists is False
    assert r.degraded is False


async def test_chunker_drift_never_reports_body_changed(world):
    # 旧索引没有 chunker 版本位，且内容确实不同 → 不可归因，不许误报变更
    keep_root = select_chunk_by_anchor(chunk_swift(ROOT_A_SWIFT), "C.keep()")
    urn = _index(world, "Sources/B.swift", "C.keep()", keep_root.text,
                 chunker_version=None)
    r = await world.verifier.verify(urn)
    assert r.reason == "chunker_version_drift"
    assert r.verified is False and r.body_changed is None and r.exists is True


# ── 降级路径：永不抛穿 + 100% 留痕 ──────────────────────────────────────────

class _DeadSCM:
    async def head_sha(self, repo):
        raise AdapterUnavailableError("git 不可达：连接超时")

    async def read_file_at_commit(self, repo, path, commit):
        raise AdapterUnavailableError("git 不可达")


async def test_scm_unavailable_degrades_without_raising(world):
    dead = LiveVerifier(_DeadSCM(), world.store, world.repo, project="demo")
    fresh = select_chunk_by_anchor(chunk_swift(HEAD_B_SWIFT), "C.fresh()")
    urn = _index(world, "Sources/B.swift", "C.fresh()", fresh.text)
    r = await dead.verify(urn)
    assert r.degraded is True and r.reason == "unavailable"
    assert r.exists is None and r.body_changed is None
    assert "git 不可达" in r.error


async def test_path_outside_repo_degrades(world):
    fresh = select_chunk_by_anchor(chunk_swift(HEAD_B_SWIFT), "C.fresh()")
    # upsert_document 冲突时不更新 uri 列，仓外路径必须在建索引时就给
    urn = _index(world, "Sources/B.swift", "C.fresh()", fresh.text,
                 uri="file:///elsewhere/Sources/B.swift")
    r = await world.verifier.verify(urn)
    assert r.degraded is True and r.reason == "path_outside_repo"
    assert r.exists is None


async def test_unparseable_urn_degrades(world):
    r = await world.verifier.verify("not-a-urn")
    assert r.degraded is True and r.reason == "unparseable_urn"

    r2 = await world.verifier.verify("urn:testpilot:fs-docs:chunk:no-anchor")
    assert r2.degraded is True and r2.reason == "unparseable_urn"


async def test_index_missing_degrades(world):
    urn = make_urn("fs-docs", "chunk", "Sources/B.swift#C.unknown()")
    r = await world.verifier.verify(urn)
    assert r.degraded is True and r.reason == "index_missing"


async def test_every_call_persists_one_row(world):
    fresh = select_chunk_by_anchor(chunk_swift(HEAD_B_SWIFT), "C.fresh()")
    urn_ok = _index(world, "Sources/B.swift", "C.fresh()", fresh.text)
    urn_gone = _index(world, "Sources/B.swift", "C.ghost()", "func ghost() {}\n", idx=1)

    assert _events(world) == []
    await world.verifier.verify(urn_ok)
    await world.verifier.verify(urn_gone)
    await world.verifier.verify("urn:testpilot:fs-docs:chunk:missing-anchor")
    dead = LiveVerifier(_DeadSCM(), world.store, world.repo, project="demo")
    await dead.verify(urn_ok)

    rows = _events(world)
    assert len(rows) == 4  # 成功/缺失/URN 坏/scm 挂：各一行，无一漏痕
    assert [r["reason"] for r in rows] == [
        "unchanged", "symbol_missing", "unparseable_urn", "unavailable",
    ]
    assert rows[0]["verified"] == 1 and rows[0]["degraded"] == 0
    assert rows[0]["symbol_exists"] == 1 and rows[0]["body_changed"] == 0
    assert rows[1]["symbol_exists"] == 0 and rows[1]["body_changed"] is None
    assert rows[2]["symbol_exists"] is None  # 降级行：没有事实，None 不冒充 False
    assert rows[3]["degraded"] == 1 and rows[3]["head_commit"] == ""
    assert rows[0]["urn"] == urn_ok and rows[0]["project"] == "demo"
