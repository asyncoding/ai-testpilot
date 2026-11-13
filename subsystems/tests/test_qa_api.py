"""S1.5 问答 HTTP API 端到端测试（starlette TestClient + 注入 container）。

与服务层测试的分工：test_qa_service 用 fake 网关验证 QaService 分支；本文件走真实
offline-demo 装配（recorded 模型 + FTS + run_ingestion 真入库 + AsyncSqliteSaver
lifespan），验证 HTTP 契约：状态码、异常映射、错误体形状、白名单 404、静态挂载守卫。

hermetic 约定：conftest 不清 TESTPILOT_PROFILE，故一律走 create_app(container=...)
注入接缝 + tmp data_dir，不读环境、不碰真实 store/offline-demo 库。
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from testpilot_core.enums import AuthorityLevel, VersionState
from testpilot_core.errors import (
    AdapterUnavailableError,
    EvidenceInsufficientError,
    NotFoundError,
    PolicyDeniedError,
    SchemaValidationError,
    TestPilotError,
    UnsupportedCapabilityError,
)
from testpilot_core.models import KnowledgeChunk, SourceDocument
from testpilot_orchestrator.app import create_app
from testpilot_orchestrator.config_loader import load_profile
from testpilot_orchestrator.factory import build_container
from testpilot_orchestrator.qa_api.errors import register_exception_handlers
from testpilot_workers.ingestion import run_ingestion

_SUBSYSTEMS = Path(__file__).resolve().parents[1]
_PROFILE = _SUBSYSTEMS / "config" / "profiles" / "offline-demo.yaml"

CONTROL_DOC = "urn:testpilot:fs-docs:document:docs/control-only.md"
CONTROL_CHUNK = "urn:testpilot:fs-docs:chunk:docs/control-only.md#section-one"


@pytest.fixture
def container(tmp_path):
    config = load_profile(_PROFILE)
    config.paths["data_dir"] = str(tmp_path / "data")
    return build_container(config, _SUBSYSTEMS)


def _client(container, **kw) -> TestClient:
    return TestClient(create_app(container=container, **kw))


def _ask_body(container, question, **extra) -> dict:
    return {
        "question": question,
        "project": container.config.project.name,
        "branch": container.config.project.default_branch,
        **extra,
    }


def _source_url(run_id: str, urn: str) -> str:
    # URN 含 '/' 与 '#'（chunk 锚点）：'#' 不编码会被当成 URL fragment 截断，
    # 必须全量百分号编码，服务端 :path 转换器会原样解回
    return f"/api/qa/runs/{run_id}/sources/{quote(urn, safe='')}"


def _count(container, table: str) -> int:
    row = container.get("store").conn.execute(
        f"SELECT COUNT(*) AS n FROM {table}"
    ).fetchone()
    return int(row["n"])


async def _ingest(container):
    # full=True：与 qa_probe 同范式，测试重复跑结果确定
    await run_ingestion(container, trigger="pytest-qa-api", full=True)


# ── 1. chitchat：无入库依赖的确定性未知降级 ──────────────────────────────────

async def test_ask_chitchat_returns_unknown_out_of_scope(container):
    with _client(container) as client:
        resp = client.post(
            "/api/qa/ask",
            json=_ask_body(container, "你好，讲个笑话", case_key="chitchat"),
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "unknown"
    assert data["unknown_reason"] == "out_of_scope"
    assert data["run_id"]


# ── 2. kill_backup 预设：answered + 三态标注合法 + 事故双段 ───────────────────

async def test_ask_preset_incident_answered_with_tri_state_citations(container):
    await _ingest(container)
    with _client(container) as client:
        resp = client.post("/api/qa/ask", json=_ask_body(
            container,
            "侧边键停钟失效的兜底机制是怎么演进的？现在 kill-backup 多少秒？",
            case_key="kill_backup",
        ))
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "answered"
    legal_states = {s.value for s in VersionState}
    citations = data["answer"]["citations"]
    assert citations
    for cite in citations:
        assert cite["version_state"] in legal_states
    # 事故双段 markdown（FR-QA-003）经 HTTP 原样可达
    markdown = data["answer"]["markdown"]
    assert "## 历史证词（逐字引用，未经模型改写）" in markdown
    assert "## 当前结论" in markdown
    assert data["answer"]["history"]  # L2 历史证词非空


# ── 3. 请求体校验：空 question / 非法枚举原生 422 ─────────────────────────────

async def test_ask_and_feedback_validation_return_native_422(container):
    with _client(container) as client:
        blank = client.post("/api/qa/ask", json=_ask_body(container, "   "))
        assert blank.status_code == 422
        missing = client.post("/api/qa/ask", json={"project": "demo"})
        assert missing.status_code == 422

        # 先造一条真实 run 再验证 feedback 枚举
        ok = client.post("/api/qa/ask", json=_ask_body(
            container, "你好", case_key="chitchat"))
        run_id = ok.json()["run_id"]
        bad_kind = client.post(
            f"/api/qa/runs/{run_id}/feedback", json={"kind": "thumbs_sideways"})
        assert bad_kind.status_code == 422
        bad_reason = client.post(
            f"/api/qa/runs/{run_id}/feedback",
            json={"kind": "not_useful", "reason": "not_a_reason"})
        assert bad_reason.status_code == 422


# ── 4. run 详情：200 与幽灵 404 错误体 ───────────────────────────────────────

async def test_get_run_detail_and_ghost_404(container):
    with _client(container) as client:
        run_id = client.post("/api/qa/ask", json=_ask_body(
            container, "你好", case_key="chitchat")).json()["run_id"]

        detail = client.get(f"/api/qa/runs/{run_id}")
        assert detail.status_code == 200
        assert detail.json()["run_id"] == run_id

        ghost = client.get("/api/qa/runs/run-never-exists")
        assert ghost.status_code == 404
        body = ghost.json()
        assert body["error"]["type"] == "NotFoundError"
        assert body["error"]["message"]


# ── 5. 反馈：快乐路径行数差 +1；幽灵 run 404 且行数差 0 ──────────────────────

async def test_feedback_row_delta_and_ghost_rejected(container):
    with _client(container) as client:
        run_id = client.post("/api/qa/ask", json=_ask_body(
            container, "你好", case_key="chitchat")).json()["run_id"]
        before = _count(container, "feedback")

        ok = client.post(f"/api/qa/runs/{run_id}/feedback",
                         json={"kind": "useful"})
        assert ok.status_code == 200
        assert isinstance(ok.json()["id"], int) and ok.json()["id"] > 0
        assert _count(container, "feedback") - before == 1

        ghost_before = _count(container, "feedback")
        ghost = client.post("/api/qa/runs/run-ghost-http/feedback",
                            json={"kind": "not_useful", "reason": "off_topic"})
        assert ghost.status_code == 404
        assert ghost.json()["error"]["type"] == "NotFoundError"
        # 反假绿：拒绝必须零留痕，不只是返回了 404
        assert _count(container, "feedback") == ghost_before


# ── 6. 原文代理：白名单内 200 整句等值；同库对照文档/穿越形态 404 ─────────────

async def test_source_whitelist_with_same_db_control_doc(container):
    await _ingest(container)
    store = container.get("store")
    # 同库真实存在、但不属于任何 run 白名单的对照文档（证明不是「全 404」假绿）
    store.upsert_document(SourceDocument(
        urn=CONTROL_DOC, source_system="fs-docs",
        uri="file:///repo/tests/fixtures/docs/control-only.md",
        title="control-only.md", version="h1", content_hash="doc-hash-ctrl",
        authority_level=AuthorityLevel.L2_EXPERIENCE, acl_scope="default",
    ))
    store.upsert_chunk(KnowledgeChunk(
        urn=CONTROL_CHUNK, doc_urn=CONTROL_DOC, index=0, kind="text",
        text="对照文档原文，不应经任何 run 取到", branch="main",
        content_hash="ch-hash-ctrl",
    ))

    with _client(container) as client:
        run_id = client.post("/api/qa/ask", json=_ask_body(
            container,
            "侧边键停钟失效的兜底机制是怎么演进的？现在 kill-backup 多少秒？",
            case_key="kill_backup",
        )).json()["run_id"]
        run = client.get(f"/api/qa/runs/{run_id}").json()
        evidence = run["evidence"]
        assert evidence
        target = evidence[0]

        src = client.get(_source_url(run_id, target["urn"]))
        assert src.status_code == 200
        src_data = src.json()
        # 整句等值防子串假绿：原文与 hash 必须与落盘证据逐字一致
        assert src_data["text"] == target["text"]
        assert src_data["content_hash"] == target["content_hash"]
        assert src_data["urn"] == target["urn"]

        control = client.get(_source_url(run_id, CONTROL_DOC))
        assert control.status_code == 404
        assert control.json()["error"]["type"] == "NotFoundError"

        # 穿越形态经编码后仍是白名单外 URN：本端点不接收任何文件路径，天然不可达
        traversal = client.get(_source_url(run_id, "../../etc/passwd"))
        assert traversal.status_code == 404

        ghost_run = client.get(_source_url("run-ghost", target["urn"]))
        assert ghost_run.status_code == 404


# ── 7. trace 时间线：结构断言 + noop tracer 下 langfuse=null ─────────────────

async def test_trace_timeline_langfuse_null_under_noop_tracer(container):
    await _ingest(container)
    with _client(container) as client:
        run_id = client.post("/api/qa/ask", json=_ask_body(
            container,
            "侧边键停钟失效的兜底机制是怎么演进的？现在 kill-backup 多少秒？",
            case_key="kill_backup",
        )).json()["run_id"]
        trace = client.get(f"/api/qa/runs/{run_id}/trace")
        assert trace.status_code == 200
        data = trace.json()
        assert client.get("/api/qa/runs/run-ghost/trace").status_code == 404
    assert data["run_id"] == run_id
    assert data["status"] == "answered"
    assert set(data["tokens"]) == {"in", "out"}
    # 四版本号键位固定；非 code 意图不跑 live-verify，data 版本允许为空（不造数）
    assert set(data["versions"]) == {"model", "prompt", "params", "data"}
    assert data["versions"]["model"]
    first_evidence = data["evidence"][0]
    # trace 证据面向排查：排名+命中通道取代裸 RRF 分值的主位（分值保留在 score）
    assert set(first_evidence) == {
        "urn", "doc_urn", "kind", "rank", "channels", "score", "authority_level"}
    assert first_evidence["rank"] == 1
    assert isinstance(first_evidence["channels"], list)
    assert data["citations"]
    # v6 起 citation_errors 是固定键位：成功 run 为空列表，失败 run 装具体错误
    assert data["citation_errors"] == []
    # FTS 通道可能不写 retrieval_events：允许 null 或整数，绝不造数
    assert data["retrieval_event_id"] is None or isinstance(
        data["retrieval_event_id"], int)
    # P3 符号置顶三键为固定键位：非符号题面如实 False/空串，不缺键
    assert data["symbol_pinned"] is False
    assert data["symbol_pin_urn"] == "" and data["symbol_pin_name"] == ""
    # v7 意图来源固定键位：中文题面走模型分类
    assert data["intent_source"] == "model"
    # offline profile tracer=noop：深链必须如实为 null
    assert data["langfuse"] is None


# ── 8. /api/config：录制夹具横幅的数据源 ─────────────────────────────────────

async def test_config_reports_recorded_profile(container):
    with _client(container) as client:
        resp = client.get("/api/config")
    assert resp.status_code == 200
    data = resp.json()
    assert data == {
        "profile": "offline-demo",
        "project": "AlarmKitSpike-demo",
        "branch": "alarmkit-spike",
        "model_adapter": "recorded",
        "search_adapter": "sqlite-fts",
        "recorded": True,
        "external_write_enabled": False,
        "tracer": "noop",
    }


# ── 9. 反馈聚合 + run 列表状态过滤（FR-QA-004 AC2）──────────────────────────

async def test_feedback_summary_and_run_list_filter(container):
    # 同一 container 只能起一次 lifespan（退出即关 store），全部断言收在一个 client 块
    with _client(container) as client:
        chat_run = client.post("/api/qa/ask", json=_ask_body(
            container, "你好", case_key="chitchat")).json()["run_id"]
        client.post(f"/api/qa/runs/{chat_run}/feedback",
                    json={"kind": "not_useful", "reason": "off_topic"})

        summary = client.get("/api/qa/feedback/summary")
        assert summary.status_code == 200
        flat = {(r["kind"], r["reason"]): r["n"] for r in summary.json()}
        assert flat[("not_useful", "off_topic")] == 1

        unknowns = client.get("/api/qa/runs", params={"status": "unknown"})
        assert unknowns.status_code == 200
        runs = unknowns.json()["runs"]
        assert runs and all(r["status"] == "unknown" for r in runs)
        assert any(r["run_id"] == chat_run for r in runs)

        limited = client.get("/api/qa/runs", params={"limit": 1})
        assert len(limited.json()["runs"]) == 1


# ── 10. 异常映射表：核心错误类型 → HTTP 状态码 + 统一错误体 ──────────────────

def test_error_mapping_table():
    app = FastAPI()
    register_exception_handlers(app)
    cases = [
        (NotFoundError("n"), 404),
        (PolicyDeniedError("p"), 403),
        (AdapterUnavailableError("a"), 503),
        (UnsupportedCapabilityError("u"), 501),
        (SchemaValidationError("s"), 502),
        (EvidenceInsufficientError("e"), 422),
        (TestPilotError("t"), 500),
    ]
    for idx, (exc, _status) in enumerate(cases):
        def _route(_request: Request, exc=exc):
            raise exc

        app.add_api_route(f"/boom-{idx}", _route, methods=["GET"])

    with TestClient(app, raise_server_exceptions=False) as client:
        for idx, (exc, status) in enumerate(cases):
            resp = client.get(f"/boom-{idx}")
            assert resp.status_code == status
            assert resp.json() == {
                "error": {"type": type(exc).__name__, "message": str(exc)}}


# ── 11. 静态挂载守卫：缺失不阻断 API；存在则同端口托管 ───────────────────────

async def test_missing_web_dist_keeps_api_and_health(container, tmp_path):
    # 显式指向不存在的目录：hermetic，不依赖仓内 web/out 是否已构建
    with _client(container, web_dist=tmp_path / "no-such-web") as client:
        assert client.get("/health").status_code == 200
        assert client.get("/api/config").status_code == 200
        assert client.get("/").status_code == 404


async def test_existing_web_dist_serves_index_without_shadowing_api(
    container, tmp_path
):
    dist = tmp_path / "out"
    dist.mkdir()
    (dist / "index.html").write_text(
        "<html><body>testpilot-web-marker</body></html>", encoding="utf-8")
    with _client(container, web_dist=dist) as client:
        root = client.get("/")
        assert root.status_code == 200
        assert "testpilot-web-marker" in root.text
        # 路由注册顺序保证 /api 不被根挂载吞掉
        assert client.get("/api/config").status_code == 200
        assert client.get("/health").status_code == 200


async def test_flat_export_clean_urls_map_to_dot_html(container, tmp_path):
    """Next output:"export" 扁平产物（approvals.html 而非 approvals/index.html）：
    干净 URL /approvals 必须回退命中同名 .html；2026-11-13 线上 404 回归红证。"""
    dist = tmp_path / "out"
    (dist / "runs").mkdir(parents=True)
    (dist / "index.html").write_text("ROOT", encoding="utf-8")
    (dist / "approvals.html").write_text("APPROVALS-PAGE", encoding="utf-8")
    (dist / "runs" / "detail.html").write_text("RUN-DETAIL", encoding="utf-8")
    (dist / "404.html").write_text("NEXT-NOT-FOUND", encoding="utf-8")
    with _client(container, web_dist=dist) as client:
        # 红证核心：无扩展名干净 URL
        resp = client.get("/approvals", follow_redirects=False)
        assert resp.status_code == 200 and "APPROVALS-PAGE" in resp.text
        nested = client.get("/runs/detail", follow_redirects=False)
        assert nested.status_code == 200 and "RUN-DETAIL" in nested.text
        # 显式 .html 与根路径行为不变
        assert client.get("/approvals.html").status_code == 200
        assert client.get("/").status_code == 200
        # 未知路由仍走 Next 404.html（404 状态不被回退吞掉）
        missing = client.get("/no-such-route")
        assert missing.status_code == 404 and "NEXT-NOT-FOUND" in missing.text
        # 防穿越：lookup_path 的 commonpath 守卫仍在，绝不逃出 dist
        traversal = client.get("/../../etc/passwd")
        assert traversal.status_code == 404
