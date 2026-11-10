"""S2.6b FR-ING-004 知识关系 HTTP API 端到端测试（TestClient + 注入容器）。

hermetic：offline-demo profile + tmp data_dir，不读环境、不碰真实 offline 库、
不起端口。覆盖关系浏览（低置信默认隐藏/kind 过滤/422）、证据 dossier（200/404、
unresolved fail-closed）与 scope 隔离。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from testpilot_core.case_relations import (
    RELATION_SCOPE_URN,
    CaseRelation,
    case_relation_urn,
)
from testpilot_core.enums import OracleType
from testpilot_core.models import TestCase, TestPoint
from testpilot_core.oracle_mining import MinedOracle
from testpilot_core.requirement import RequirementFeature, RequirementParseResult
from testpilot_orchestrator.app import create_app
from testpilot_orchestrator.config_loader import load_profile
from testpilot_orchestrator.factory import build_container
from testpilot_workers.relation_building import run_relation_building

_SUBSYSTEMS = Path(__file__).resolve().parents[1]
_PROFILE = _SUBSYSTEMS / "config" / "profiles" / "offline-demo.yaml"
_CASE_DOC = "urn:testpilot:fs-cases:document:cases/c.md"
_REQ_DOC = "urn:testpilot:fs-reqs:document:req.md"
_CASE1 = "urn:testpilot:fs-cases:test_case:cases/c.md#TC-1"
_FR1 = "urn:testpilot:fs-reqs:requirement:req.md#FR-1"
_SYM1 = "urn:testpilot:fs-docs:symbol:a.swift#AlarmStore.arm"
_SUG1 = "urn:testpilot:fs-cases:oracle_suggestion:c.md#s1"


@pytest.fixture
def container(tmp_path):
    config = load_profile(_PROFILE)
    config.paths["data_dir"] = str(tmp_path / "data")
    return build_container(config, _SUBSYSTEMS)


def _client(container) -> TestClient:
    return TestClient(create_app(container=container))


def _seed_relation_graph(store, *, model_symbol: bool = False) -> None:
    store.upsert_requirement_skeleton(
        _REQ_DOC,
        RequirementParseResult(
            doc_urn=_REQ_DOC, external_id="req.md",
            features=[RequirementFeature(urn=_FR1, fr_id="FR-1", text="前台响铃")]),
        content_hash="h-req")
    store.upsert_test_case(
        _CASE_DOC,
        TestCase(urn=_CASE1, external_id="c.md#TC-1", title="响铃用例",
                 steps=["步骤1"], tags=["FR-1"]),
        content_hash="h-case")
    store.upsert_oracle_suggestion(
        _CASE_DOC, _CASE1, "c.md",
        MinedOracle(type=OracleType.LOG_ASSERTION, expectation="日志含 arm()",
                    source_refs=[_SYM1], source_kind="code_symbol",
                    provenance="model" if model_symbol else "deterministic"),
        content_hash="h-sug")
    store.upsert_test_point(
        _REQ_DOC, "req.md",
        TestPoint(text="验证主流程", fr_id="FR-1", source_refs=[_FR1],
                  kind="functional"),
        content_hash="h-point")


# ── 浏览口 ───────────────────────────────────────────────────────────────────

def test_empty_relations_catalog(container):
    with _client(container) as client:
        resp = client.get("/api/knowledge/relations")
        assert resp.status_code == 200
        body = resp.json()
        assert body["relations"] == []
        assert body["scope_urn"] == RELATION_SCOPE_URN
        assert body["low_confidence_threshold"] == 0.60


def test_list_filters_kind_and_low_confidence(container):
    store = container.get("store")
    _seed_relation_graph(store, model_symbol=True)  # 模型符号关系 0.50 → 低置信
    run_relation_building(container)

    with _client(container) as client:
        visible = client.get("/api/knowledge/relations").json()["relations"]
        assert {r["target_urn"] for r in visible} == {_FR1}  # 符号关系被藏

        all_rels = client.get(
            "/api/knowledge/relations", params={"include_low": "true"}
        ).json()["relations"]
        assert {r["target_urn"] for r in all_rels} == {_FR1, _SYM1}

        only_req = client.get(
            "/api/knowledge/relations",
            params={"kind": "case_validates_requirement"}).json()["relations"]
        assert {r["kind"] for r in only_req} == {"case_validates_requirement"}

        bad = client.get("/api/knowledge/relations", params={"kind": "calls"})
        assert bad.status_code == 422
        assert bad.json()["error"]["type"] == "CaseRelationError"


def test_ingestion_relations_not_visible(container):
    store = container.get("store")
    _seed_relation_graph(store)
    run_relation_building(container)
    # 摄入管线的 calls 关系：不同 doc_urn scope
    store.conn.execute(
        """INSERT INTO relations
           (urn, source_urn, target_urn, kind, confidence, evidence_json, doc_urn)
           VALUES (?,?,?,?,?, '{"lines": [], "urns": []}', ?)""",
        ("urn:testpilot:fs-docs:rel:doc#a->b", _SYM1,
         "urn:testpilot:fs-docs:symbol:a.swift#Other.arm",
         "calls", 0.9, "urn:testpilot:fs-docs:document:a.swift"))
    store.conn.commit()

    with _client(container) as client:
        kinds = {r["kind"] for r in client.get(
            "/api/knowledge/relations",
            params={"include_low": "true"}).json()["relations"]}
        assert "calls" not in kinds


# ── 证据 dossier ─────────────────────────────────────────────────────────────

def test_evidence_dossier_resolved_and_unresolved(container):
    store = container.get("store")
    _seed_relation_graph(store)
    run_relation_building(container)

    rel_urn = case_relation_urn(
        _CASE1, "case_validates_requirement", _FR1)
    with _client(container) as client:
        resp = client.get(f"/api/knowledge/relations/{rel_urn}/evidence")
        assert resp.status_code == 200
        body = resp.json()
        assert body["relation"]["urn"] == rel_urn
        items = {i["urn"]: i for i in body["dossier"]["items"]}
        # fr_tag 路径证据就是用例 URN，test_cases 可解析
        assert items[_CASE1]["status"] == "resolved"
        assert items[_CASE1]["kind"] == "test_case" and items[_CASE1]["label"]
        assert body["dossier"]["complete"] is True

        # 手工塞一条证据含不可解析 URN 的关系，fail-closed 标 unresolved
        ghost_target = "urn:testpilot:fs-docs:symbol:a.swift#Ghost.missing"
        ghost = CaseRelation(
            urn=case_relation_urn(_CASE1, "case_references_symbol", ghost_target),
            source_urn=_CASE1, target_urn=ghost_target,
            kind="case_references_symbol", confidence=0.7,
            evidence_urns=[_SUG1, ghost_target])
        store.replace_case_relations(RELATION_SCOPE_URN,
                                     store.list_case_relations(include_low=True) + [ghost])
        resp2 = client.get(
            f"/api/knowledge/relations/{ghost.urn}/evidence")
        dossier = resp2.json()["dossier"]
        statuses = {i["urn"]: i["status"] for i in dossier["items"]}
        assert statuses[_SUG1] == "unresolved"  # 库内无此 URN，不编造
        assert dossier["complete"] is False


def test_evidence_missing_relation_404(container):
    with _client(container) as client:
        resp = client.get(
            "/api/knowledge/relations/urn:testpilot:case-relations:rel:nope/evidence")
        assert resp.status_code == 404
        assert resp.json()["error"]["type"] == "NotFoundError"
