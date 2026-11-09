"""FR-TD-003 oracle 挖掘 worker 与 v11 存储集成测试（S2.4）。

全链路在 tmp_path 临时库 hermetic 运行：需求抽取（S2.2）→ 用例入库 →
oracle 挖掘（S2.4），钉死录制夹具报告数字、幻觉死信（source=oracle-mine）、
增量对账、文档级防幽灵、AC3 exploratory 回写与检索降级口径。
不依赖语料仓、不碰 8765 端口进程。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from testpilot_adapters.recorded_model import RecordedModelGateway
from testpilot_adapters.requirement_docs import RequirementDocumentAdapter
from testpilot_adapters.store import bootstrap_store
from testpilot_core.enums import OracleType
from testpilot_core.models import Oracle, TestCase
from testpilot_workers.oracle_mining import run_oracle_mining
from testpilot_workers.requirement_extraction import run_requirement_extraction

FIXTURE_DOC = Path(__file__).parent / "fixtures" / "requirements" / "synthetic-requirements.md"
MODEL_FIXTURE = Path(__file__).parent / "fixtures" / "model" / "responses.json"

CASE_DOC_URN = "urn:testpilot:fs-cases:document:cases/synthetic-cases.md"
CASE_DOC_EXT = "cases/synthetic-cases.md"
OTHER_DOC_URN = "urn:testpilot:fs-cases:document:cases/other.md"


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
          lifecycle: str = "active", oracle_missing: bool = False,
          oracles=None, doc_ext: str = CASE_DOC_EXT) -> TestCase:
    return TestCase(
        urn=f"urn:testpilot:fs-cases:test_case:{doc_ext}#{cid}",
        external_id=cid, title=title, steps=["步骤一", "步骤二"],
        tags=tags or [], priority=priority, lifecycle=lifecycle,  # type: ignore[arg-type]
        oracle_missing=oracle_missing, oracles=oracles or [],
    )


def _seed_requirements(tmp_path: Path, *, model=None) -> tuple:
    req_root = tmp_path / "requirements"
    _write(req_root, "synthetic-requirements.md",
           FIXTURE_DOC.read_text(encoding="utf-8"))
    store = bootstrap_store(tmp_path / "data")
    adapter = RequirementDocumentAdapter([req_root])
    container = _Container(store, requirements=adapter, model=model)
    asyncio.run(run_requirement_extraction(container))
    return store, container


def _seed_cases(store) -> None:
    """四条用例：带断言用例 / 缺预期（录制键命中）/ 缺预期（无录制键降级）/ 取消。"""
    store.upsert_test_case(
        CASE_DOC_URN,
        _case("TC-H1", title="闹钟响铃横幅展示", tags=["FR-1"],
              oracles=[Oracle(type=OracleType.UI_ASSERTION,
                              expectation="页面展示全屏横幅", confidence=0.5)]),
        content_hash="h1",
    )
    store.upsert_test_case(
        CASE_DOC_URN,
        _case("TC-N2", title="只有步骤没有预期", tags=["FR-11"],
              oracle_missing=True),
        content_hash="h1",
    )
    store.upsert_test_case(
        OTHER_DOC_URN,
        _case("TC-MISS", title="无人值守边界操作", oracle_missing=True,
              doc_ext="cases/other.md"),
        content_hash="g1",
    )
    store.upsert_test_case(
        CASE_DOC_URN,
        _case("TC-X", title="已取消的旧响铃用例", tags=["FR-1"],
              lifecycle="cancelled"),
        content_hash="h1",
    )


# ── 全链路报告数字、死信与草稿隔离 ─────────────────────────────────────────

def test_mining_pipeline_pins_recorded_report(tmp_path):
    model = RecordedModelGateway(MODEL_FIXTURE)
    store, container = _seed_requirements(tmp_path, model=model)
    try:
        _seed_cases(store)
        report = asyncio.run(run_oracle_mining(container))
        assert report == {
            "status": "succeeded",
            "docs_seen": 2,
            "cases_seen": 4,
            "cases_skipped_unchanged": 0,
            "cases_rebuilt": 3,          # 取消用例不挖掘
            "cases_cancelled": 1,
            "suggestions_total": 4,     # TC-H1 两条确定性 + TC-N2 两条模型
            "deterministic_oracles": 2,
            "model_oracles": 2,
            "model_rejected": 2,        # bad_type + unknown_source 投毒
            "model_deduped": 0,
            "model_calls": 2,           # 预算守卫：只打两条 oracle_missing
            "degraded_cases": 1,        # TC-MISS 无录制键 → AdapterUnavailable
            "exploratory_cases": 1,     # 仅 TC-MISS 挖无可执行 oracle
            "hallucination_rate": 0.5,
            "evidence_related": 0,
            "evidence_incidents": 0,
            "evidence_symbols": 0,
            "retrieval_degraded": 0,
        }

        # 幻觉只进死信（source=oracle-mine），绝不进草稿表
        dead = store.conn.execute(
            "SELECT error FROM dead_letters WHERE source='oracle-mine'"
        ).fetchall()
        assert len(dead) == 2
        assert {row["error"].split(":", 1)[0] for row in dead} == {
            "bad_type", "unknown_source"
        }

        rows = store.list_oracle_suggestions()
        assert len(rows) == 4
        assert {r["status"] for r in rows} == {"pending"}  # 草稿恒 pending
        joined = " ".join(r["oracle"].expectation for r in rows)
        assert "投毒" not in joined and "made-up" not in joined
        model_rows = [r for r in rows if r["oracle"].provenance == "model"]
        assert len(model_rows) == 2
        assert all(r["oracle"].confidence == 0.5 for r in model_rows)
        assert all(r["oracle"].source_kind == "model_proposed" for r in model_rows)
        # 模型补的是可执行类型（state/log），TC-N2 因此摘掉探索性
        n2 = [r for r in rows if r["external_id"] == "TC-N2"]
        assert {r["oracle"].type for r in n2} == {
            OracleType.STATE_ASSERTION, OracleType.LOG_ASSERTION}
        assert all(r["exploratory"] is False for r in n2)

        # 草稿不碰正式用例库：TC-N2 正式 oracles 仍为空，exploratory 走独立回写
        cases = {c.external_id: c for c in store.list_test_cases()}
        assert cases["TC-N2"].oracles == []
        assert cases["TC-N2"].exploratory is False
        assert cases["TC-MISS"].exploratory is True   # AC3：无 oracle 不进自动化
        assert cases["TC-H1"].exploratory is False
    finally:
        store.close()


# ── 增量与文档级防幽灵 ─────────────────────────────────────────────────────

def test_incremental_skip_then_full_rebuild(tmp_path):
    model = RecordedModelGateway(MODEL_FIXTURE)
    store, container = _seed_requirements(tmp_path, model=model)
    try:
        _seed_cases(store)
        first = asyncio.run(run_oracle_mining(container))
        assert first["cases_rebuilt"] == 3
        assert store.count_oracle_suggestions() == 4

        second = asyncio.run(run_oracle_mining(container))
        assert second["cases_skipped_unchanged"] == 4 and second["cases_rebuilt"] == 0
        assert store.count_oracle_suggestions() == 4  # 跳过不重写

        full = asyncio.run(run_oracle_mining(container, full=True))
        assert full["cases_rebuilt"] == 3
        assert store.count_oracle_suggestions() == 4
    finally:
        store.close()


def test_doc_sweep_reclaims_orphan_suggestions_of_removed_case(tmp_path):
    model = RecordedModelGateway(MODEL_FIXTURE)
    store, container = _seed_requirements(tmp_path, model=model)
    try:
        _seed_cases(store)
        asyncio.run(run_oracle_mining(container))
        old_urns = {
            r["urn"] for r in store.conn.execute("SELECT urn FROM oracle_suggestions")
        }
        assert len(old_urns) == 4

        # 文档改版：TC-N2 被删除，hash 变化触发整文档重建
        store.delete_test_cases_for_doc(CASE_DOC_URN)
        store.upsert_test_case(
            CASE_DOC_URN,
            _case("TC-H1", title="闹钟响铃横幅展示", tags=["FR-1"],
                  oracles=[Oracle(type=OracleType.UI_ASSERTION,
                                  expectation="页面展示全屏横幅", confidence=0.5)]),
            content_hash="h2",
        )
        report = asyncio.run(run_oracle_mining(container, full=True))
        assert report["cases_rebuilt"] == 2  # TC-H1 + TC-MISS
        remaining = {
            r["urn"] for r in store.conn.execute("SELECT urn FROM oracle_suggestions")
        }
        # 被删 TC-N2 的草稿必须整组消失；TC-H1 内容未变，内容寻址 URN 稳定复现
        old_n2 = {u for u in old_urns if "TC-N2" in u}
        assert old_n2 and remaining.isdisjoint(old_n2)
        assert all("TC-H1" in u for u in remaining) and len(remaining) == 2
        leftover = store.conn.execute(
            "SELECT COUNT(*) AS n FROM oracle_suggestions WHERE case_urn LIKE '%TC-N2'"
        ).fetchone()["n"]
        assert leftover == 0
    finally:
        store.close()


# ── 前置守卫与降级 ─────────────────────────────────────────────────────────

def test_empty_case_store_raises_loud(tmp_path):
    store = bootstrap_store(tmp_path / "data")
    try:
        with pytest.raises(ValueError, match="test_cases 为空"):
            asyncio.run(run_oracle_mining(_Container(store)))
    finally:
        store.close()


def test_no_model_runs_pure_deterministic(tmp_path):
    store, container = _seed_requirements(tmp_path, model=None)
    try:
        _seed_cases(store)
        report = asyncio.run(run_oracle_mining(container))
        assert report["model_calls"] == 0 and report["model_oracles"] == 0
        assert report["degraded_cases"] == 0  # 纯确定性是正常模式，不算降级
        assert report["model_rejected"] == 0 and report["hallucination_rate"] == 0.0
        # 无模型时两条 oracle_missing 都是探索性（TC-N2/TC-MISS）
        assert report["exploratory_cases"] == 2
        assert all(
            r["oracle"].provenance == "deterministic"
            for r in store.list_oracle_suggestions()
        )
    finally:
        store.close()


def test_missing_skeleton_is_empty_ac_source_not_error(tmp_path):
    # 不跑需求抽取：骨架为空，确定性 AC 源跳过但管线成功
    store = bootstrap_store(tmp_path / "data")
    container = _Container(store)
    try:
        store.upsert_test_case(
            CASE_DOC_URN,
            _case("TC-H1", title="闹钟响铃横幅展示",
                  oracles=[Oracle(type=OracleType.UI_ASSERTION,
                                  expectation="页面展示全屏横幅", confidence=0.5)]),
            content_hash="h1",
        )
        report = asyncio.run(run_oracle_mining(container))
        assert report["status"] == "succeeded"
        assert report["suggestions_total"] == 1  # 自身断言仍保留
    finally:
        store.close()


def test_retrieval_failure_counted_not_fatal(tmp_path):
    model = RecordedModelGateway(MODEL_FIXTURE)
    store, container = _seed_requirements(tmp_path, model=model)
    container._adapters["search"] = _ExplodingSearch()
    try:
        _seed_cases(store)
        report = asyncio.run(run_oracle_mining(container))
        assert report["status"] == "succeeded"
        assert report["retrieval_degraded"] == 3  # 三条活跃用例各试一次
        assert report["model_rejected"] == 2      # 模型链路不受检索故障影响
    finally:
        store.close()


# ── 存储口径 ───────────────────────────────────────────────────────────────

def test_suggestion_urn_idempotent_and_case_delete(tmp_path):
    from testpilot_core.oracle_mining import MinedOracle

    store = bootstrap_store(tmp_path / "data")
    try:
        oracle = MinedOracle(
            type=OracleType.STATE_ASSERTION, expectation="落库一条 stopped 记录",
            source_kind="requirement_ac", provenance="deterministic",
            source_refs=["urn:testpilot:fs-reqs:requirement:x#AC-1"], confidence=0.7,
        )
        store.upsert_oracle_suggestion(
            CASE_DOC_URN, "urn:case:1", "TC-1", oracle, content_hash="h")
        store.upsert_oracle_suggestion(
            CASE_DOC_URN, "urn:case:1", "TC-1", oracle, content_hash="h")
        assert store.count_oracle_suggestions() == 1  # URN 冲突走替换不翻倍
        row = store.list_oracle_suggestions()[0]
        assert row["oracle"] == oracle and row["status"] == "pending"
        assert row["case_urn"] == "urn:case:1"

        assert store.delete_oracle_suggestions_for_case("urn:case:1") == 1
        assert store.count_oracle_suggestions() == 0
    finally:
        store.close()
