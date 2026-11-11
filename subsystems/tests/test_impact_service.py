"""影响面分析服务 hermetic 测试（FR-FRS-003，S4.3）。

tempfile 真 sqlite + 合成 Swift 语料 + fake scm/tickets，不起端口、不碰真实仓：
- 影响链端到端：符号 URN 解析 → 关系 → 三分类 → 工单历史缺陷链接
- 裸锚点 ChangeSet（GitCodeRepository 真实产物形态）可解析（S4.2 路径过滤缺口回归）
- 未入库符号进 unresolved、需求入口、无票源安全降级
- MR 摘要确定性脊柱 + 模型白名单拒入
- 精度以合成 TestRun 回算，门限 0.8
"""

from __future__ import annotations

import pytest

from testpilot_adapters.store import bootstrap_store
from testpilot_adapters.tracers import NoopTracer
from testpilot_core.case_relations import (
    RELATION_SCOPE_URN,
    CaseRelation,
    case_relation_urn,
)
from testpilot_core.enums import AuthorityLevel, CaseStatus, ChangeType, RunStatus
from testpilot_core.errors import NotFoundError
from testpilot_core.models import (
    CaseResult,
    CaseSelector,
    ChangeSet,
    EnvironmentInfo,
    Issue,
    KnowledgeChunk,
    RepoRef,
    SourceDocument,
    SymbolChange,
    TestCase,
    TestExecutionRequest,
    TestRun,
)
from testpilot_core.urn import make_urn
from testpilot_workers.impact_service import (
    analyze_impact,
    build_mr_summary,
    precision_from_run,
)

_SOURCE = "fs-docs"
GIT_PATH = "Sources/Alarm.swift"
EXT = f"code/{GIT_PATH}"
DOC_URN = make_urn(_SOURCE, "document", EXT)
BASE = "1" * 40
HEAD = "2" * 40
MOD_ANCHOR = "AlarmScheduler.schedulePrimary()"
REM_ANCHOR = "AlarmScheduler.legacyAlarmBell()"
MOD_URN = make_urn(_SOURCE, "symbol", f"{EXT}#{MOD_ANCHOR}")
REM_URN = make_urn(_SOURCE, "symbol", f"{EXT}#{REM_ANCHOR}")
REQ_URN = "urn:testpilot:fs-req:requirement:FR-FRS-003"

CASE_RUN = make_urn("fs-cases", "case", "TC-RUN")
CASE_CONF = make_urn("fs-cases", "case", "TC-CONF")
CASE_EXP = make_urn("fs-cases", "case", "TC-EXP")
CASE_CANCEL = make_urn("fs-cases", "case", "TC-CANCEL")


def _case(urn: str, *, exploratory: bool = False,
          lifecycle: str = "active") -> TestCase:
    return TestCase(
        urn=urn, external_id=urn.rsplit(":", 1)[-1],
        title=f"用例 {urn[-6:]}", steps=["步骤"], exploratory=exploratory,
        lifecycle=lifecycle,  # type: ignore[arg-type]
    )


def _refs(case_urn: str, symbol_urn: str,
          confidence: float = 0.7) -> CaseRelation:
    return CaseRelation(
        urn=case_relation_urn(case_urn, "case_references_symbol", symbol_urn),
        source_urn=case_urn, target_urn=symbol_urn,
        kind="case_references_symbol", confidence=confidence,
        provenance=["oracle_suggestion"],
    )


def _validates(case_urn: str, requirement_urn: str) -> CaseRelation:
    return CaseRelation(
        urn=case_relation_urn(case_urn, "case_validates_requirement",
                              requirement_urn),
        source_urn=case_urn, target_urn=requirement_urn,
        kind="case_validates_requirement", confidence=0.9,
        provenance=["fr_tag"],
    )


class _FakeScm:
    def __init__(self, change_set: ChangeSet):
        self._cs = change_set

    async def head_sha(self, repo):
        return HEAD

    async def get_change_set(self, repo, base, head):
        return self._cs


class _FakeTickets:
    def __init__(self, issues: list[Issue] | None = None):
        self._issues = issues or []

    async def search(self, query: str, *, project: str, limit: int = 10):
        return list(self._issues)[:limit]


class _Container:
    def __init__(self, store, scm, *, tickets=None):
        self.adapters = {
            "store": store,
            "tracer": NoopTracer(),
            "scm": scm,
            "repo": RepoRef(project="demo", repo_path="/tmp/demo-repo",
                            default_branch="main"),
        }
        if tickets is not None:
            self.adapters["tickets"] = tickets

    def get(self, name: str):
        return self.adapters.get(name)


def _seed_index(store) -> None:
    """文档 + 两个符号切块（find_symbol_urns_by_anchor 走 chunks 表）。"""
    store.upsert_document(SourceDocument(
        urn=DOC_URN, source_system=_SOURCE, uri="file:///repo/Alarm.swift",
        title="Alarm", version="v1", content_hash="h1",
        authority_level=AuthorityLevel.L1_AUTHORITATIVE, acl_scope="default",
        metadata_={"kind_hint": "swift"},
    ))
    for idx, (anchor, urn) in enumerate(
        ((MOD_ANCHOR, MOD_URN), (REM_ANCHOR, REM_URN))
    ):
        store.upsert_chunk(KnowledgeChunk(
            urn=make_urn(_SOURCE, "chunk", f"{EXT}#{anchor}"),
            doc_urn=DOC_URN, index=idx, kind="code_symbol",
            text=f"func {anchor} body {anchor}", symbol_urn=urn,
            quality_score=0.9, valid_from=BASE, content_hash=f"ch-{idx}",
        ))


def _seed_cases_and_relations(store, *, extra_relations=None) -> None:
    doc = "urn:testpilot:fs-cases:document:synthetic-cases"
    for urn, exploratory, lifecycle in (
        (CASE_RUN, False, "active"),
        (CASE_CONF, False, "active"),
        (CASE_EXP, True, "active"),
        (CASE_CANCEL, False, "cancelled"),
    ):
        store.upsert_test_case(
            doc, _case(urn, exploratory=exploratory, lifecycle=lifecycle),
            content_hash="hc-1",
        )
    relations = [
        _refs(CASE_RUN, MOD_URN),
        _refs(CASE_CONF, REM_URN),
        _refs(CASE_EXP, MOD_URN),
    ]
    relations.extend(extra_relations or [])
    store.replace_case_relations(RELATION_SCOPE_URN, relations)


def _change_set(*, bare: bool = False) -> ChangeSet:
    def sid(anchor: str) -> str:
        return anchor if bare else f"{GIT_PATH}#{anchor}"

    return ChangeSet(
        repo=RepoRef(project="demo", repo_path="/tmp/demo-repo"),
        base_commit=BASE, head_commit=HEAD, changed_files=[GIT_PATH],
        symbol_changes=[
            SymbolChange(symbol_id=sid(MOD_ANCHOR),
                         change_type=ChangeType.MODIFIED),
            SymbolChange(symbol_id=sid(REM_ANCHOR),
                         change_type=ChangeType.REMOVED),
        ],
    )


def _issue(external_id: str, related: list[str]) -> Issue:
    return Issue(
        urn=make_urn("json-tickets", "issue", external_id),
        external_id=external_id, title=f"缺陷 {external_id}",
        status="closed", severity="P1", related_case_ids=related,
    )


# ── 影响链端到端 ─────────────────────────────────────────────────────────────


async def test_analyze_impact_three_way_classification_with_defect_link(tmp_path):
    store = bootstrap_store(tmp_path / "data")
    _seed_index(store)
    _seed_cases_and_relations(store)
    tickets = _FakeTickets([
        _issue("TKT-1", ["TC-RUN", "TC-NOPE"]),
        _issue("TKT-2", ["TC-NOPE"]),
    ])
    container = _Container(store, _FakeScm(_change_set()), tickets=tickets)

    analysis = await analyze_impact(
        container, base_commit=BASE, head_commit=HEAD)

    buckets = {c.external_id: c.classification for c in analysis.cases}
    assert buckets == {
        "TC-RUN": "runnable",
        "TC-CONF": "conflict_update",
        "TC-EXP": "no_automation",
    }
    # 取消态用例不进清单，只登记
    assert "TC-CANCEL" not in buckets
    assert analysis.cancelled_cases_skipped == ["TC-CANCEL"]
    # 冲突原因码闭集正确
    conf = next(c for c in analysis.cases if c.external_id == "TC-CONF")
    assert conf.reasons == ["symbol_removed"]
    assert analysis.unresolved_symbols == []
    # 历史缺陷只链在 Scope 内用例
    assert [d.external_id for d in analysis.historical_defects] == ["TKT-1"]
    assert analysis.historical_defects[0].matched_case_ids == ["TC-RUN"]
    # 推荐集只有可直接跑用例
    assert analysis.recommended_case_urns == [CASE_RUN]


async def test_bare_anchor_change_set_resolves_to_indexed_urns(tmp_path):
    """真实 GitCodeRepository 产出裸锚点 symbol_id（无路径前缀）。"""
    store = bootstrap_store(tmp_path / "data")
    _seed_index(store)
    _seed_cases_and_relations(store)
    container = _Container(store, _FakeScm(_change_set(bare=True)))

    analysis = await analyze_impact(
        container, base_commit=BASE, head_commit=HEAD)

    assert {s.urn for s in analysis.affected_symbols} == {MOD_URN, REM_URN}
    assert analysis.unresolved_symbols == []
    assert analysis.classification_counts["runnable"] == 1


async def test_unindexed_symbol_goes_to_unresolved(tmp_path):
    store = bootstrap_store(tmp_path / "data")
    _seed_index(store)
    _seed_cases_and_relations(store)
    cs = ChangeSet(
        repo=RepoRef(project="demo", repo_path="/tmp/demo-repo"),
        base_commit=BASE, head_commit=HEAD, changed_files=[GIT_PATH],
        symbol_changes=[
            SymbolChange(symbol_id=f"{GIT_PATH}#Ghost.gone()",
                         change_type=ChangeType.REMOVED),
        ],
    )
    container = _Container(store, _FakeScm(cs))

    analysis = await analyze_impact(
        container, base_commit=BASE, head_commit=HEAD)

    assert len(analysis.unresolved_symbols) == 1
    assert analysis.unresolved_symbols[0].symbol_id.endswith("Ghost.gone()")
    assert analysis.affected_symbols == []


async def test_requirement_entry_includes_validating_cases(tmp_path):
    store = bootstrap_store(tmp_path / "data")
    _seed_index(store)
    # TC-RUN 额外挂一条需求验证关系，经需求入口独立纳入
    _seed_cases_and_relations(
        store, extra_relations=[_validates(CASE_RUN, REQ_URN)])
    container = _Container(store, _FakeScm(_change_set()))

    analysis = await analyze_impact(
        container, base_commit=BASE, head_commit=HEAD,
        requirement_urns=[REQ_URN])

    row = next(c for c in analysis.cases if c.external_id == "TC-RUN")
    assert "requirement_entry" in row.reasons
    assert row.requirement_urns == [REQ_URN]


async def test_missing_tickets_adapter_returns_empty_defects(tmp_path):
    store = bootstrap_store(tmp_path / "data")
    _seed_index(store)
    _seed_cases_and_relations(store)
    container = _Container(store, _FakeScm(_change_set()))  # 无 tickets

    analysis = await analyze_impact(
        container, base_commit=BASE, head_commit=HEAD)

    assert analysis.historical_defects == []
    assert analysis.cases  # 主路径不受票源缺省影响


async def test_analyze_without_scm_raises(tmp_path):
    store = bootstrap_store(tmp_path / "data")
    container = _Container(store, _FakeScm(_change_set()))
    container.adapters.pop("scm")
    with pytest.raises(RuntimeError, match="scm/repo"):
        await analyze_impact(container, base_commit=BASE, head_commit=HEAD)


# ── MR 摘要：确定性脊柱 + 白名单补点 ─────────────────────────────────────────


async def test_mr_summary_deterministic_then_model_whitelist(tmp_path):
    store = bootstrap_store(tmp_path / "data")
    _seed_index(store)
    container = _Container(store, _FakeScm(_change_set()))

    deterministic = await build_mr_summary(
        container, base_commit=BASE, head_commit=HEAD)
    assert deterministic.generation_source == "deterministic"
    assert deterministic.summary.generated_from == "diff"
    assert deterministic.summary.symbols_modified == 1
    assert deterministic.summary.symbols_removed == 1
    # 确定性条目自带 diff 证据
    assert all(b.evidence for b in deterministic.summary.bullets)

    good_note = {"text": "风险：调度主入口变更，关注响铃时序",
                 "symbol_urn": MOD_URN}
    evil_note = {"text": "越界备注", "symbol_urn": "urn:testpilot:fs-docs:symbol:x"}
    supplemented = await build_mr_summary(
        container, base_commit=BASE, head_commit=HEAD,
        model_rows=[good_note, evil_note], model_version="wl-1",
    )
    assert supplemented.generation_source == "model"
    assert len(supplemented.notes) == 1
    assert supplemented.rejected[0].reason == "unknown_symbol"
    # 模型行改不了确定性脊柱
    assert supplemented.summary.bullets == deterministic.summary.bullets


# ── 精度回算（AC2，门限 0.8） ─────────────────────────────────────────────────


def _seed_run(store, run_id: str, results: list[CaseResult]) -> None:
    request = TestExecutionRequest(
        request_id=f"req-{run_id}", idempotency_key=f"idem-{run_id}",
        project="demo", selector=CaseSelector(), commit_sha=HEAD,
    )
    store.insert_test_run_ledger(
        request=request, run_id=run_id, status=RunStatus.QUEUED.value)
    run = TestRun(
        run_id=run_id, idempotency_key=f"idem-{run_id}", project="demo",
        commit_sha=HEAD, status=RunStatus.PASSED,
        environment=EnvironmentInfo(provider="junit-local", env_id="jdk21"),
        case_results=results,
    )
    store.update_test_run(
        run_id, status=RunStatus.PASSED.value,
        raw_json=run.model_dump_json())


async def test_precision_from_run_back_computes_gate_and_reduction(tmp_path):
    store = bootstrap_store(tmp_path / "data")
    _seed_index(store)
    _seed_cases_and_relations(store)
    tickets = _FakeTickets([_issue("TKT-1", ["TC-RUN"])])
    container = _Container(store, _FakeScm(_change_set()), tickets=tickets)
    analysis = await analyze_impact(
        container, base_commit=BASE, head_commit=HEAD)

    _seed_run(store, "run-abc", [
        CaseResult(external_case_id="TC-RUN", status=CaseStatus.PASSED),
        CaseResult(external_case_id="TC-CONF", status=CaseStatus.FAILED),
    ])
    report = await precision_from_run(
        container, analysis=analysis, run_id="run-abc",
        full_suite_size=10,
    )
    # 推荐集仅 TC-RUN，它被工单缺陷真值命中 → precision 1.0 过门
    assert report.evaluated == 1 and report.related_hits == 1
    assert report.precision == 1.0 and report.gate_passed is True
    assert report.reduction_ratio == 0.8  # 执行 2 / 全套 10


async def test_precision_unknown_run_raises(tmp_path):
    store = bootstrap_store(tmp_path / "data")
    _seed_index(store)
    _seed_cases_and_relations(store)
    container = _Container(store, _FakeScm(_change_set()))
    analysis = await analyze_impact(
        container, base_commit=BASE, head_commit=HEAD)
    with pytest.raises(NotFoundError, match="run-missing"):
        await precision_from_run(
            container, analysis=analysis, run_id="run-missing")
