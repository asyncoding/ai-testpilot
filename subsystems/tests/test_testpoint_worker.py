"""FR-TD-002 测试点生成 worker 与 v10 存储集成测试（S2.3）。

全链路在 tmp_path 临时库上 hermetic 运行：需求抽取（S2.2）→ 用例入库 →
测试点生成（S2.3），钉死录制夹具报告数字、幻觉死信、增量对账、整文档替换
与检索降级口径。不依赖语料仓、不碰 8765 端口进程。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from testpilot_adapters.recorded_model import RecordedModelGateway
from testpilot_adapters.requirement_docs import RequirementDocumentAdapter
from testpilot_adapters.store import bootstrap_store
from testpilot_core.models import TestCase, TestPoint
from testpilot_core.testpoint import point_urn
from testpilot_workers.requirement_extraction import run_requirement_extraction
from testpilot_workers.testpoint_generation import run_testpoint_generation

FIXTURE_DOC = Path(__file__).parent / "fixtures" / "requirements" / "synthetic-requirements.md"
MODEL_FIXTURE = Path(__file__).parent / "fixtures" / "model" / "responses.json"

_GOOD_DOC = """# 合成需求

## 1. 功能需求（FR）

### 1.1 模块一

| ID | 需求 | 优先级 |
|---|---|---|
| FR-1 | 功能一 | P0 |

## 2. 非功能需求（NFR）

| ID | 需求 |
|---|---|
| NFR-1 | 规则一 |

## 3. 验收标准

1. FR-1 必须可观测、可回滚。
"""


class _Container:
    def __init__(self, store, *, requirements=None, model=None, search=None,
                 project="demo"):
        self._adapters = {"store": store}
        if requirements is not None:
            self._adapters["requirements"] = requirements
        if model is not None:
            self._adapters["model"] = model
        if search is not None:
            self._adapters["search"] = search
        self.config = SimpleNamespace(project=SimpleNamespace(name=project))

    def get(self, name: str):
        return self._adapters.get(name)


class _ExplodingSearch:
    async def search(self, *args, **kwargs):
        raise RuntimeError("模拟检索通道故障")


def _write(root: Path, name: str, text: str) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _case(cid: str, *, title: str, tags=None, priority: str = "P1",
          lifecycle: str = "active") -> TestCase:
    return TestCase(
        urn=f"urn:testpilot:fs-cases:test_case:cases/x.md#{cid}",
        external_id=cid, title=title, steps=["步骤一", "步骤二"],
        tags=tags or [], priority=priority, lifecycle=lifecycle,  # type: ignore[arg-type]
    )


def _seed_requirements(tmp_path: Path, *, model=None) -> tuple:
    req_root = tmp_path / "requirements"
    _write(req_root, "synthetic-requirements.md", FIXTURE_DOC.read_text(encoding="utf-8"))
    store = bootstrap_store(tmp_path / "data")
    adapter = RequirementDocumentAdapter([req_root])
    container = _Container(store, requirements=adapter, model=model)
    asyncio.run(run_requirement_extraction(container))
    return store, container


# ── 全链路报告数字与幻觉死信 ────────────────────────────────────────────────

def test_generation_pipeline_pins_recorded_report(tmp_path):
    model = RecordedModelGateway(MODEL_FIXTURE)
    store, container = _seed_requirements(tmp_path, model=model)
    try:
        # 直接构造带 FR tag 的活跃/取消用例：tag 精确命中确定性可对账
        doc_urn = "urn:testpilot:fs-cases:document:cases/x.md"
        store.upsert_test_case(
            doc_urn, _case("TC-T1", title="响铃中锁屏", tags=["FR-1"], priority="P0"),
            content_hash="h1",
        )
        store.upsert_test_case(
            doc_urn, _case("TC-X", title="取消用例", tags=["FR-1"],
                           lifecycle="cancelled"),
            content_hash="h1",
        )

        report = asyncio.run(run_testpoint_generation(container))
        assert report == {
            "status": "succeeded",
            "docs_seen": 1,
            "skipped_unchanged": 0,
            "docs_rebuilt": 1,
            "points_total": 16,       # 13 脊柱 + 3 模型补点
            "spine_points": 13,
            "model_points": 3,
            "model_rejected": 2,     # 投毒：unknown_fr + unknown_source
            "model_deduped": 1,
            "hallucination_rate": 0.4,
            "degraded_docs": 0,
            "evidence_cases": 1,     # 取消用例被 lifecycle 过滤
            "evidence_incidents": 0,
            "evidence_symbols": 0,
            "retrieval_degraded": 0,
            "p0_points": 9,
            "p1_points": 4,
        }

        # 幻觉只进死信（source=tp-gen），绝不进 test_points
        dead = store.conn.execute(
            "SELECT error FROM dead_letters WHERE source='tp-gen'"
        ).fetchall()
        assert len(dead) == 2
        assert {row["error"].split(":", 1)[0] for row in dead} == {
            "unknown_fr", "unknown_source"
        }

        points = store.list_test_points()
        assert len(points) == 16
        provenance = [p.provenance for p in points]
        assert provenance.count("deterministic") == 13
        assert provenance.count("model") == 3
        joined = " ".join(p.text for p in points)
        assert "FR-999" not in joined and "made-up" not in joined
        for p in points:  # 无编造 URN：来源只回链需求域
            assert all(ref.startswith("urn:testpilot:fs-reqs:") for ref in p.source_refs)
    finally:
        store.close()


# ── 增量与整文档替换 ────────────────────────────────────────────────────────

def test_incremental_skip_then_full_rebuild(tmp_path):
    model = RecordedModelGateway(MODEL_FIXTURE)
    store, container = _seed_requirements(tmp_path, model=model)
    try:
        first = asyncio.run(run_testpoint_generation(container))
        assert first["docs_rebuilt"] == 1

        second = asyncio.run(run_testpoint_generation(container))
        assert second["skipped_unchanged"] == 1 and second["docs_rebuilt"] == 0
        assert store.count_test_points() == 16

        full = asyncio.run(run_testpoint_generation(container, full=True))
        assert full["docs_rebuilt"] == 1 and full["points_total"] == 16
        assert store.count_test_points() == 16
    finally:
        store.close()


def test_whole_doc_replacement_leaves_no_ghost_points(tmp_path):
    # 第一版：完整合成需求（带模型），16 点
    model = RecordedModelGateway(MODEL_FIXTURE)
    store, model_container = _seed_requirements(tmp_path, model=model)
    try:
        asyncio.run(run_testpoint_generation(model_container))
        # 同文不同 fr_id 的验收点 URN 不同，按 URN 而非 text 对账（text 集合会塌缩）
        old_urns = {
            r["urn"] for r in store.conn.execute("SELECT urn FROM test_points")
        }
        assert len(old_urns) == 16
        doc_urn = store.list_requirement_skeleton_rows()[0]["doc_urn"]

        # 文档改版为单 FR 文档；无模型容器跑纯脊柱（1 FR + 1 NFR + 1 AC = 3 点）
        _write(tmp_path / "requirements", "synthetic-requirements.md", _GOOD_DOC)
        plain_container = _Container(
            store, requirements=model_container.get("requirements")
        )
        asyncio.run(run_requirement_extraction(plain_container, full=True))
        report = asyncio.run(run_testpoint_generation(plain_container, full=True))
        assert report["points_total"] == 3 and report["model_points"] == 0

        remaining = {
            r["urn"] for r in store.conn.execute("SELECT urn FROM test_points")
        }
        assert remaining.isdisjoint(old_urns)  # 旧版测试点整组消失，无幽灵残留
        assert store.count_test_points() == 3
        assert store.conn.execute(
            "SELECT COUNT(*) AS n FROM test_points WHERE doc_urn=?", (doc_urn,)
        ).fetchone()["n"] == 3
    finally:
        store.close()


def test_point_urn_upsert_is_idempotent_replace(tmp_path):
    store = bootstrap_store(tmp_path / "data")
    try:
        point = TestPoint(text="同一文本", priority="P0",
                          source_refs=["urn:testpilot:fs-reqs:x#FR-1"],
                          fr_id="FR-1", kind="functional")
        urn = point_urn("doc-x", point)
        store.upsert_test_point("urn:doc", "doc-x", point, content_hash="h1")
        store.upsert_test_point("urn:doc", "doc-x", point, content_hash="h1")
        assert store.count_test_points() == 1  # URN 冲突走替换不翻倍

        assert store.delete_test_points_for_doc("urn:doc") == 1
        assert store.count_test_points() == 0
    finally:
        store.close()


# ── 前置守卫与降级 ──────────────────────────────────────────────────────────

def test_missing_skeletons_raises_loud(tmp_path):
    store = bootstrap_store(tmp_path / "data")
    try:
        with pytest.raises(ValueError, match="requirement_skeletons"):
            asyncio.run(run_testpoint_generation(_Container(store)))
    finally:
        store.close()


def test_no_model_runs_pure_deterministic_spine(tmp_path):
    store, container = _seed_requirements(tmp_path, model=None)
    try:
        report = asyncio.run(run_testpoint_generation(container))
        assert report["points_total"] == 11  # 6 functional + 2 rule + 3 acceptance
        assert report["spine_points"] == 11 and report["model_points"] == 0
        assert report["degraded_docs"] == 0  # 纯确定性是正常模式，不算降级
        assert report["hallucination_rate"] == 0.0
        assert all(p.provenance == "deterministic" for p in store.list_test_points())
    finally:
        store.close()


def test_missing_recorded_key_degrades_to_spine(tmp_path):
    req_root = tmp_path / "requirements"
    _write(req_root, "other.md", _GOOD_DOC)  # 无 tp_gen:requirements/other.md 录制
    store = bootstrap_store(tmp_path / "data")
    adapter = RequirementDocumentAdapter([req_root])
    model = RecordedModelGateway(MODEL_FIXTURE)
    container = _Container(store, requirements=adapter, model=model)
    try:
        asyncio.run(run_requirement_extraction(container))
        report = asyncio.run(run_testpoint_generation(container))
        assert report["degraded_docs"] == 1
        assert report["points_total"] == 3 and report["model_points"] == 0
        assert all(p.provenance == "deterministic" for p in store.list_test_points())
    finally:
        store.close()


def test_retrieval_failure_counted_not_fatal_and_no_fabricated_urn(tmp_path):
    store, container = _seed_requirements(
        tmp_path, model=RecordedModelGateway(MODEL_FIXTURE)
    )
    container._adapters["search"] = _ExplodingSearch()
    try:
        report = asyncio.run(run_testpoint_generation(container))
        assert report["status"] == "succeeded"
        assert report["retrieval_degraded"] == 1
        # 检索故障不编造证据：所有来源 URN 仍限需求域
        for p in store.list_test_points():
            assert all(ref.startswith("urn:testpilot:fs-reqs:") for ref in p.source_refs)
    finally:
        store.close()


# ── search_test_cases 存储口径 ──────────────────────────────────────────────

def test_search_test_cases_tag_like_order_and_active_filter(tmp_path):
    store = bootstrap_store(tmp_path / "data")
    doc_urn = "urn:testpilot:fs-cases:document:cases/x.md"
    try:
        store.upsert_test_case(
            doc_urn, _case("TC-1", title="普通标题", tags=["FR-1"], priority="P2"),
            content_hash="h",
        )
        store.upsert_test_case(
            doc_urn, _case("TC-2", title="响铃中锁屏", tags=["FR-9"], priority="P0"),
            content_hash="h",
        )
        store.upsert_test_case(
            doc_urn, _case("TC-3", title="全屏提醒接管", tags=["FR-2"], priority="P1"),
            content_hash="h",
        )
        store.upsert_test_case(
            doc_urn, _case("TC-4", title="已取消的 FR-1 用例", tags=["FR-1"],
                           lifecycle="cancelled"),
            content_hash="h",
        )

        # tag 精确命中 + 取消态过滤
        rows = store.search_test_cases(["FR-1"])
        assert [r["external_id"] for r in rows] == ["TC-1"]

        # 标题 LIKE 兜底（无 tag 交集时）
        rows = store.search_test_cases([], ["响铃"])
        assert [r["external_id"] for r in rows] == ["TC-2"]

        # 优先级 P0→P3 排序，同名按 URN 稳定
        rows = store.search_test_cases(["FR-1", "FR-9", "FR-2"])
        assert [r["external_id"] for r in rows] == ["TC-2", "TC-3", "TC-1"]

        # 空条件不扫全表
        assert store.search_test_cases([]) == []
        assert store.search_test_cases([], []) == []
    finally:
        store.close()
