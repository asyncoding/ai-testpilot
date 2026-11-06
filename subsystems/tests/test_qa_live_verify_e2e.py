"""stale-index 问答端到端（docs/08 FR-QA-002，真 tmp git 仓，无 mock 接线）。

构造与 test_live_verify 同款场景，但走完整问答图而不是直调 LiveVerifier：
索引停在 root（旧函数体 interval=90 口径）、不重灌，直接发起代码类提问 →
verify 当场判 body_changed → head_refresh 读 HEAD 重切重锚 → 引用三态标注
verified_at:<短 sha>，且引用 quote 与 HEAD 文本逐字一致。

反假绿：
- verification_events 用「前后行数差 == 代码证据块数」断言，不是存在性断言；
- 期望 hash 直接对 HEAD 重切块文本现场 sha256，不经过被测代码；
- 无 git 二进制时整文件 skip（本地外环境差异如实标注，不伪造绿）。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from git_helpers import GIT_BIN, HEAD_B_SWIFT, ROOT_A_SWIFT, build_demo_repo
from testpilot_adapters.fts_search import FtsSearchAdapter
from testpilot_adapters.git_repo import GitCodeRepository
from testpilot_adapters.recorded_model import RecordedModelGateway
from testpilot_adapters.store.sqlite_store import bootstrap_store
from testpilot_core.chunking import SWIFT_CHUNKER_VERSION, chunk_swift, select_chunk_by_anchor
from testpilot_core.enums import QaStatus, VersionState
from testpilot_core.models import KnowledgeChunk, RepoRef, SourceDocument
from testpilot_core.urn import make_urn
from testpilot_orchestrator.qa_graph.graph import build_qa_graph, recursion_limit
from testpilot_orchestrator.qa_graph.nodes import QaDeps

pytestmark = pytest.mark.skipif(GIT_BIN is None, reason="环境无 git 二进制")

FIXTURES = Path(__file__).parent / "fixtures" / "model" / "responses.json"
GIT_REL = "Sources/B.swift"
ANCHOR = "C.keep()"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.fixture
def world(tmp_path: Path):
    cwd, root, head = build_demo_repo(tmp_path / "repo-parent")
    store = bootstrap_store(tmp_path / "data")
    repo = RepoRef(project="demo", repo_path=str(cwd), default_branch="main")
    scm = GitCodeRepository(repo, git_path=GIT_BIN)

    # 索引停在 root：写入旧版 keep() 函数体（valid_from=root，带 chunker 版本位）。
    # 与 test_live_verify 同口径：root 函数体挂在 HEAD 路径 B.swift 下，模拟未重灌的陈旧索引。
    keep_root = select_chunk_by_anchor(chunk_swift(ROOT_A_SWIFT), ANCHOR)
    doc_urn = make_urn("fs-docs", "document", GIT_REL)
    chunk_urn = make_urn("fs-docs", "chunk", f"{GIT_REL}#{ANCHOR}")
    store.upsert_document(SourceDocument(
        urn=doc_urn, source_system="fs-docs",
        uri=f"file://{cwd / GIT_REL}",
        content_hash=_sha("doc-stale"), branch="main",
    ))
    store.upsert_chunk(KnowledgeChunk(
        urn=chunk_urn, doc_urn=doc_urn, index=0, kind="code_symbol",
        text=keep_root.text, content_hash=_sha(keep_root.text),
        branch="main", valid_from=root,
        metadata={"chunker_version": SWIFT_CHUNKER_VERSION},
    ))

    search = FtsSearchAdapter(
        store, project="demo", scm=scm, repo=repo, default_branch="main",
    )
    model = RecordedModelGateway(FIXTURES)
    deps = QaDeps(model=model, search=search, scm=scm, repo=repo, store=store)
    return type("W", (), {
        "cwd": cwd, "root": root, "head": head, "store": store,
        "repo": repo, "scm": scm, "search": search, "deps": deps,
        "doc_urn": doc_urn, "chunk_urn": chunk_urn,
    })()


def _event_count(world) -> int:
    return world.store.conn.execute(
        "SELECT COUNT(*) AS n FROM verification_events"
    ).fetchone()["n"]


async def test_stale_index_question_refreshes_head_and_answers_verified_at(world):
    keep_head = select_chunk_by_anchor(chunk_swift(HEAD_B_SWIFT), ANCHOR)
    before = _event_count(world)
    assert before == 0  # 前置：干净库，无校验留痕

    graph = build_qa_graph(world.deps)
    out = await graph.ainvoke(
        {
            "run_id": "run-e2e-stale", "case_key": "stale_code",
            "question": "C.keep() 方法的实现逻辑是什么？",
            "project": "demo", "branch": "main", "acl_scope": "default",
        },
        config={"configurable": {"thread_id": "run-e2e-stale"},
                "recursion_limit": recursion_limit()},
    )

    # 1) 图结论：answered，意图来自录制模型而非兜底
    assert out["status"] == QaStatus.ANSWERED.value
    assert out["intent"] == "code" and out["intent_degraded"] is False

    # 2) live-verify 留痕：恰好 1 行（只有 1 个 code_symbol 证据块），裁决 body_changed
    after = _event_count(world)
    assert after - before == 1
    event = dict(world.store.conn.execute(
        "SELECT * FROM verification_events"
    ).fetchone())
    assert event["urn"] == world.chunk_urn
    assert event["body_changed"] == 1 and event["verified"] == 0
    assert event["head_commit"] == world.head

    # 3) HEAD 刷新：证据文本/hash 已换成 HEAD 重切块版本
    assert out["head_refreshed_urns"] == [world.chunk_urn]
    refreshed = next(e for e in out["evidence"] if e["urn"] == world.chunk_urn)
    assert refreshed["text"] == keep_head.text
    assert refreshed["content_hash"] == _sha(keep_head.text)
    assert refreshed["metadata"]["head_refreshed"] is True

    # 4) 三态标注 verified_at + 短 sha；quote 与 HEAD 文本逐字子串一致
    cite = out["final_answer"]["citations"][0]
    assert cite["version_state"] == VersionState.VERIFIED_AT.value
    assert cite["version_ref"] == world.head[:7]
    assert cite["quote"] in keep_head.text
    assert cite["content_hash"] == _sha(keep_head.text)


async def test_fresh_index_same_question_answers_consistent(world):
    """对照实验：把索引刷成 HEAD 版本后同问，应判 consistent 且零刷新、零 body_changed。"""
    keep_head = select_chunk_by_anchor(chunk_swift(HEAD_B_SWIFT), ANCHOR)
    world.store.upsert_chunk(KnowledgeChunk(
        urn=world.chunk_urn, doc_urn=world.doc_urn, index=0, kind="code_symbol",
        text=keep_head.text, content_hash=_sha(keep_head.text),
        branch="main", valid_from=world.head,
        metadata={"chunker_version": SWIFT_CHUNKER_VERSION},
    ))

    graph = build_qa_graph(world.deps)
    out = await graph.ainvoke(
        {
            "run_id": "run-e2e-fresh", "case_key": "stale_code",
            "question": "C.keep() 方法的实现逻辑是什么？",
            "project": "demo", "branch": "main", "acl_scope": "default",
        },
        config={"configurable": {"thread_id": "run-e2e-fresh"},
                "recursion_limit": recursion_limit()},
    )

    assert out["status"] == QaStatus.ANSWERED.value
    # 无 body_changed 时 route_after_verify 直连 generation、head_refresh 节点不执行，
    # 该字段按 total=False 契约保持缺失（assembly 侧也以 .get(...) or [] 读法容忍）
    assert (out.get("head_refreshed_urns") or []) == []
    cite = out["final_answer"]["citations"][0]
    assert cite["version_state"] == VersionState.CONSISTENT.value
    assert cite["version_ref"] == ""
    event = dict(world.store.conn.execute(
        "SELECT * FROM verification_events"
    ).fetchone())
    assert event["body_changed"] == 0 and event["verified"] == 1
