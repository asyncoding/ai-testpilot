"""S4.5 知识管理页盘点/治理测试（FR-UI-004）。

两层：① 真 sqlite 存储口（聚合投影/治理落账/reindex 存活）；② FastAPI
TestClient 全 HTTP 面（200/404/422/导出头）。hermetic tmp 库，不起端口、
不碰 offline-demo，schema 仍 v15（治理数据只进 raw_json.metadata）。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from testpilot_core.enums import AuthorityLevel
from testpilot_core.governance import GOVERNANCE_KEY, apply_governance
from testpilot_core.models import Entity, KnowledgeChunk, SourceDocument
from testpilot_adapters.store import bootstrap_store
from testpilot_orchestrator.app import create_app
from testpilot_orchestrator.config_loader import load_profile
from testpilot_orchestrator.factory import build_container

_SUBSYSTEMS = Path(__file__).resolve().parents[1]
_PROFILE = _SUBSYSTEMS / "config" / "profiles" / "offline-demo.yaml"
_DOC = "urn:testpilot:fs-docs:document:guides/onboarding.md"
_T0 = "2026-11-11T08:00:00+00:00"


def _doc(content_hash: str = "h1") -> SourceDocument:
    return SourceDocument(
        urn=_DOC, source_system="fs-docs",
        uri="file://fixtures/docs/guides/onboarding.md",
        title="onboarding", version="v1", content_hash=content_hash,
        authority_level=AuthorityLevel.L2_EXPERIENCE,
        metadata_={"relative_in_root": "guides/onboarding.md", "kind_hint": "markdown"},
    )


def _chunk(idx: int, *, stale: str = "fresh", quality: float = 0.7) -> KnowledgeChunk:
    return KnowledgeChunk(
        urn=f"urn:testpilot:fs-docs:chunk:guides/onboarding.md#{idx}",
        doc_urn=_DOC, index=idx, kind="text", text=f"block {idx}",
        quality_score=quality, valid_from="1" * 40,
        valid_to="2" * 40 if stale == "stale" else None,
        stale=stale, content_hash=f"ch-{idx}",
    )


# ── 存储口 ────────────────────────────────────────────────────────────────────


def test_list_knowledge_documents_aggregates_counts_and_owner(tmp_path):
    store = bootstrap_store(tmp_path / "data")
    governed = apply_governance(
        _doc(), owner="alice",
        authority_level=AuthorityLevel.L1_AUTHORITATIVE,
        edited_by="web-console",
        edited_at=datetime.fromisoformat(_T0),
    )
    store.upsert_document(governed)
    store.upsert_chunk(_chunk(0))
    store.upsert_chunk(_chunk(1, stale="stale", quality=0.4))

    rows = store.list_knowledge_documents()
    assert len(rows) == 1
    row = rows[0]
    assert row["urn"] == _DOC
    assert row["chunk_count"] == 2 and row["stale_count"] == 1
    assert row["max_quality"] == 0.7
    assert row["owner"] == "alice"
    assert row["authority_level"] == "L1_authoritative"
    assert row["governance"]["history"][0]["by"] == "web-console"

    # branch 过滤：不匹配返回空，不偷偷放宽
    assert store.list_knowledge_documents(branch="ghost-branch") == []


def test_update_governance_rejects_ghost_urn(tmp_path):
    store = bootstrap_store(tmp_path / "data")
    assert store.update_document_governance(_doc()) is False


def test_reindex_preserves_human_governance(tmp_path):
    store = bootstrap_store(tmp_path / "data")
    governed = apply_governance(
        _doc(), owner="alice",
        authority_level=AuthorityLevel.L1_AUTHORITATIVE,
        edited_by="web-console",
        edited_at=datetime.fromisoformat(_T0),
    )
    store.upsert_document(governed)
    store.upsert_chunk(_chunk(0))

    # 内容变更触发 reindex：适配器产物带回默认 L2、新 hash、无治理字段
    incoming = _doc(content_hash="h2")
    store.replace_document_index(
        incoming, [_chunk(0, quality=0.8)], [], [], run_id="reidx-1"
    )

    refreshed = store.get_document(_DOC)
    assert refreshed.content_hash == "h2"  # 新内容不回退
    assert refreshed.metadata_[GOVERNANCE_KEY]["owner"] == "alice"
    assert refreshed.authority_level is AuthorityLevel.L1_AUTHORITATIVE
    # 列与 raw_json 同源，盘点投影一致
    assert store.list_knowledge_documents()[0]["owner"] == "alice"


def test_browse_chunks_and_entities(tmp_path):
    store = bootstrap_store(tmp_path / "data")
    store.upsert_document(_doc())
    store.upsert_chunk(_chunk(0))
    store.upsert_chunk(_chunk(1, stale="stale"))
    store.conn.execute(
        "INSERT INTO entities(urn, kind, name, branch, doc_urn, raw_json)"
        " VALUES (?,?,?,?,?,?)",
        ("urn:e1", "symbol", "AlarmStore.arm", "main", _DOC, "{}"),
    )
    store.conn.commit()

    chunks = store.list_doc_chunks(_DOC)
    assert [c["idx"] for c in chunks] == [0, 1]
    assert {c["stale"] for c in chunks} == {"fresh", "stale"}
    entities = store.list_entities_browse()
    assert entities[0]["name"] == "AlarmStore.arm"
    stale = store.list_stale_chunks()
    assert len(stale) == 1 and stale[0]["valid_to"] == "2" * 40


# ── HTTP 面 ───────────────────────────────────────────────────────────────────


@pytest.fixture
def client(tmp_path):
    config = load_profile(_PROFILE)
    config.paths["data_dir"] = str(tmp_path / "data")
    container = build_container(config, _SUBSYSTEMS)
    store = container.get("store")
    store.upsert_document(_doc())
    store.upsert_chunk(_chunk(0))
    store.upsert_chunk(_chunk(1, stale="stale", quality=0.4))
    with TestClient(create_app(container=container)) as c:
        yield c


def test_documents_and_chunks_endpoints(client):
    body = client.get("/api/knowledge/documents").json()
    assert body["total"] == 1
    doc = body["documents"][0]
    assert doc["chunk_count"] == 2 and doc["stale_count"] == 1
    assert doc["owner"] == ""  # 未治理时空串而非 null/缺键

    chunks = client.get(f"/api/knowledge/documents/{_DOC}/chunks").json()
    assert len(chunks["chunks"]) == 2

    ghost = client.get(
        "/api/knowledge/documents/urn:testpilot:fs-docs:document:ghost/chunks"
    )
    assert ghost.status_code == 404
    assert ghost.json()["error"]["type"] == "NotFoundError"


def test_stale_entities_and_inventory_export(client):
    stale = client.get("/api/knowledge/stale").json()
    assert len(stale["stale_chunks"]) == 1
    assert client.get("/api/knowledge/entities").status_code == 200

    resp = client.get("/api/knowledge/inventory-export")
    assert resp.status_code == 200
    assert "attachment" in resp.headers["content-disposition"]
    payload = resp.json()
    assert payload["exported_at"] and len(payload["documents"]) == 1


def test_governance_edit_full_http_lifecycle(client):
    # ① owner + 权威等级一起改
    resp = client.post(
        f"/api/knowledge/documents/{_DOC}/governance",
        json={"owner": "alice", "authority_level": "L1_authoritative",
              "edited_by": "web-console"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["owner"] == "alice"
    assert body["authority_level"] == "L1_authoritative"
    assert len(body["governance"]["history"]) == 1

    # ② 只改 owner，权威覆盖保留，历史追加
    resp2 = client.post(
        f"/api/knowledge/documents/{_DOC}/governance",
        json={"owner": "bob"},
    )
    assert resp2.status_code == 200
    g = resp2.json()["governance"]
    assert g["owner"] == "bob"
    assert g["authority_level"] == "L1_authoritative"
    assert len(g["history"]) == 2

    # ③ 盘点口立即可见
    doc = client.get("/api/knowledge/documents").json()["documents"][0]
    assert doc["owner"] == "bob"
    assert doc["authority_level"] == "L1_authoritative"


def test_governance_edit_validation_and_404(client):
    ghost = client.post(
        "/api/knowledge/documents/urn:testpilot:fs-docs:document:ghost/governance",
        json={"owner": "x"},
    )
    assert ghost.status_code == 404

    empty = client.post(
        f"/api/knowledge/documents/{_DOC}/governance", json={}
    )
    assert empty.status_code == 422  # 至少一个字段
    blank = client.post(
        f"/api/knowledge/documents/{_DOC}/governance",
        json={"owner": "   "},
    )
    assert blank.status_code == 422
    bad_enum = client.post(
        f"/api/knowledge/documents/{_DOC}/governance",
        json={"authority_level": "L9_legacy"},
    )
    assert bad_enum.status_code == 422
