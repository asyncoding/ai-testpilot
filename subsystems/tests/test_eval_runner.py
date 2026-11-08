"""S1.6 评测 runner 测试（FR-EV-002）：投影/加载/报告/副本/落库纯函数与单进程装配。

裁判正确性本身在 test_eval_judge.py 红→绿守着；本文件守 runner 的适配与装配：
qa_runs 行 → CaseResult 投影（白名单口径必须与 get_run_source 一致）、题集加载、
Markdown 报告的无基线声明、hermetic 在线备份、eval_cases/eval_runs 落库、
以及 offline-demo 真实容器跑 smoke 题集的确定性端到端。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from testpilot_adapters.store import bootstrap_store
from testpilot_core.enums import AuthorityLevel
from testpilot_core.evaluation import CaseResult, CaseVerdict, detect_holdout_leakage
from testpilot_core.models import EvalCase, SourceDocument
from testpilot_workers.eval_runner import (
    CaseRecord,
    EvaluationReport,
    case_result_from_run,
    compare_reports,
    hermetic_copy_data_dir,
    load_golden,
    render_comparison,
    render_markdown,
    run_evaluation,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "eval"
CHUNK_URN = "urn:testpilot:fs-docs:chunk:docs/kill-backup.md#背景"
DOC_URN = "urn:testpilot:fs-docs:document:docs/kill-backup.md"


def _answered_row(**over) -> dict:
    row = {
        "run_id": "run-1",
        "status": "answered",
        "intent": "incident",
        "unknown_reason": "",
        "intent_source": "model",
        "degraded": [],
        "attempts": 1,
        "latency_ms": 8000,
        "tokens_in": 1900,
        "tokens_out": 420,
        "evidence": [
            {"urn": CHUNK_URN, "doc_urn": DOC_URN},
            {"urn": "urn:testpilot:fs-docs:chunk:docs/sidebar-fallback.md#x",
             "doc_urn": "urn:testpilot:fs-docs:document:docs/sidebar-fallback.md"},
        ],
        "citations": [{"urn": DOC_URN}],
        "model_version": "recorded-local",
        "prompt_version": "qa-prompt-3",
        "params_version": "qa-params-1",
        "data_version": "",
    }
    row.update(over)
    return row


def test_projection_maps_fields_and_builds_whitelist_union():
    result = case_result_from_run(_answered_row())
    assert result.status == "answered"
    assert result.intent == "incident"
    assert result.attempts == 1
    assert result.latency_ms == 8000
    assert result.citation_urns == [DOC_URN]
    # 白名单 = 证据块 URN ∪ 文档 URN ∪ 引用 URN（与 QaService.get_run_source 同口径）
    assert CHUNK_URN in result.whitelist_urns
    assert DOC_URN in result.whitelist_urns
    assert "urn:testpilot:fs-docs:document:docs/sidebar-fallback.md" in result.whitelist_urns
    assert "" not in result.whitelist_urns


def test_projection_legacy_row_safe_defaults_no_fabrication():
    # v5 老行没有 intent_source/citation_errors/degraded 列：默认空值，绝不补造
    result = case_result_from_run(
        {"status": "unknown", "intent": "code", "evidence": None, "citations": None}
    )
    assert result.status == "unknown"
    assert result.evidence_urns == [] and result.citation_urns == []
    assert result.whitelist_urns == [] and result.degraded == []
    assert result.intent_source == "" and result.attempts == 0


def test_doc_level_citation_survives_judge_after_projection():
    # 文档级引用 URN 不在证据块列表里，但经 doc_urn 进入白名单，不能误判伪造
    case = EvalCase(
        case_id="G01", kind="qa", question="q",
        ground_truth={"expected_intent": "incident", "expected_status": "answered"},
        required_evidence=["docs/kill-backup.md"],
    )
    from testpilot_core.evaluation import judge_case
    verdict = judge_case(case, case_result_from_run(_answered_row()))
    assert verdict.passed is True
    assert "citation_forgery" not in verdict.labels


def test_load_golden_s1_and_recorded_smoke():
    golden = load_golden(FIXTURES / "golden_s1.json")
    assert golden.set_id == "golden-s1-v1"
    assert len(golden.cases) == 30
    smoke = load_golden(FIXTURES / "golden_recorded_smoke.json")
    assert smoke.set_id == "golden-recorded-smoke-v1"
    assert len(smoke.cases) == 4
    keys = [c.ground_truth["recorded_case_key"] for c in smoke.cases]
    assert keys == ["kill_backup", "stale_code", "unanswerable", "chitchat"]


def _report(set_id: str, *, passed: bool = True) -> EvaluationReport:
    verdict = CaseVerdict(
        case_id="G01", kind="qa", passed=passed,
        labels=[] if passed else ["recall_miss"], detail={},
    )
    result = CaseResult(
        status="answered", intent="code",
        evidence_urns=[CHUNK_URN], citation_urns=[CHUNK_URN],
        whitelist_urns=[CHUNK_URN, DOC_URN], latency_ms=7000,
        tokens_in=10, tokens_out=5, attempts=1,
    )
    from testpilot_core.models import EvalVersion
    return EvaluationReport(
        eval_run_id="eval-test", set_id=set_id, mode="recorded",
        version=EvalVersion(data_version="", model_version="recorded-local",
                            prompt_version="qa-prompt-3", params_version="qa-params-1"),
        metrics={
            "cases_total": 1.0, "golden_passed": 1.0 if passed else 0.0,
            "golden_pass_rate": 1.0 if passed else 0.0, "recall_at_k": 1.0,
            "refusal_accuracy": 0.0, "intent_accuracy": 1.0,
            "citation_accuracy": 1.0, "citation_forgery_count": 0.0,
            "answered_count": 1.0, "latency_median_ms": 7000.0,
            "latency_p95_ms": 7000.0, "tokens_in_total": 10.0,
            "tokens_out_total": 5.0, "attempts_total": 1.0,
        },
        records=(CaseRecord(
            case_id="G01", run_id="run-1", verdict=verdict, result=result,
            versions={"data": "", "model": "recorded-local",
                      "prompt": "qa-prompt-3", "params": "qa-params-1"},
        ),),
        leakage=(),
        report_json=Path("/tmp/x.json"), report_md=Path("/tmp/x.md"),
    )


def test_render_markdown_golden_s1_has_gates_and_explicit_no_baseline():
    md = render_markdown(_report("golden-s1-v1"), gates_applicable=True)
    assert "S1 门禁对照" in md
    assert "无基线" in md and "忠实度" in md  # FR-EV-003 AC2：不得造语义分
    assert "错误标签二分" in md
    assert "召回侧" in md and "生成侧" in md
    assert "holdout 泄漏检查" in md
    assert "citation_forgery" in md  # 零计数也要把标签列出来


def test_render_markdown_smoke_set_skips_gate_judgement():
    md = render_markdown(_report("golden-recorded-smoke-v1"), gates_applicable=False)
    assert "不做 S1 门禁判定" in md
    assert "golden-recorded-smoke-v1" in md


def test_hermetic_copy_preserves_rows_and_leaves_source_untouched(tmp_path):
    src = tmp_path / "src"
    store = bootstrap_store(src)
    store.upsert_document(SourceDocument(
        urn=DOC_URN, source_system="fs-docs", uri="file:///tmp/kb.md",
        title="kb", version="v1", content_hash="h1",
        authority_level=AuthorityLevel.L1_AUTHORITATIVE,
    ))
    store.conn.execute(
        "INSERT INTO eval_runs (eval_run_id,data_version,model_version,prompt_version,"
        "params_version,degraded_path,metrics,report_uri,created_at) "
        "VALUES ('eval-x','','m','p','pr',0,'{}',NULL,'2026-11-08T00:00:00+00:00')")
    store.conn.commit()
    store.close()

    dst = hermetic_copy_data_dir(src, tmp_path / "dst")
    copied = bootstrap_store(dst)
    assert copied.all_document_urns() == [DOC_URN]
    row = copied.conn.execute(
        "SELECT eval_run_id FROM eval_runs WHERE eval_run_id='eval-x'").fetchone()
    assert row is not None
    copied.close()
    # 源库仍可独立打开且数据完整（在线备份不得改写/截断源）
    reopened = bootstrap_store(src)
    assert reopened.all_document_urns() == [DOC_URN]
    reopened.close()


def test_store_eval_case_upsert_is_idempotent_and_locks_version(tmp_path):
    store = bootstrap_store(tmp_path)
    case = EvalCase(case_id="G01", kind="qa", question="q",
                    ground_truth={"expected_intent": "code"},
                    required_evidence=["docs/kill-backup.md"])
    store.upsert_eval_case(case, locked_version="golden-s1-v1")
    store.upsert_eval_case(case, locked_version="golden-s1-v1")  # 重跑幂等
    rows = store.conn.execute("SELECT locked_version, holdout FROM eval_cases").fetchall()
    assert len(rows) == 1 and rows[0]["locked_version"] == "golden-s1-v1"
    assert rows[0]["holdout"] == 0
    store.close()


def test_leakage_check_against_real_store(tmp_path):
    store = bootstrap_store(tmp_path)
    store.upsert_document(SourceDocument(
        urn="urn:testpilot:fs-docs:document:app/docs/V1需求文档.md",
        source_system="fs-docs", uri="file:///tmp/v1.md", title="v1",
        version="v1", content_hash="h", authority_level=AuthorityLevel.L1_AUTHORITATIVE,
    ))
    markers = ["docs/技术方案文档.md", "docs/功能设计文档.md"]
    assert detect_holdout_leakage(markers, store.all_document_urns()) == []
    store.upsert_document(SourceDocument(
        urn="urn:testpilot:fs-docs:document:app/docs/技术方案文档.md",
        source_system="fs-docs", uri="file:///tmp/tech.md", title="tech",
        version="v1", content_hash="h2", authority_level=AuthorityLevel.L2_EXPERIENCE,
    ))
    assert detect_holdout_leakage(markers, store.all_document_urns()) == [
        "docs/技术方案文档.md"]
    store.close()


async def test_recorded_container_runs_smoke_golden_end_to_end(tmp_path):
    """offline-demo 真实装配：入库 → 4 题评测 → 全绿 → 账本/报告完整（A9 的 pytest 镜像）。"""
    from testpilot_orchestrator.config_loader import load_profile
    from testpilot_orchestrator.factory import build_container
    from testpilot_workers.ingestion import run_ingestion

    subsystems = Path(__file__).resolve().parents[1]
    config = load_profile(subsystems / "config" / "profiles" / "offline-demo.yaml")
    config.paths["data_dir"] = str(tmp_path / "data")
    container = build_container(config, subsystems)
    store = container.get("store")
    try:
        ingest = await run_ingestion(container, trigger="pytest-eval", full=True)
        assert ingest.failed == 0

        golden = load_golden(FIXTURES / "golden_recorded_smoke.json")
        before = store.conn.execute("SELECT COUNT(*) AS n FROM eval_runs").fetchone()["n"]
        report = await run_evaluation(
            container, golden.cases, set_id=golden.set_id, mode="recorded",
            report_dir=tmp_path / "reports",
        )
        assert int(report.metrics["cases_total"]) == 4
        assert int(report.metrics["golden_passed"]) == 4
        assert report.metrics["refusal_accuracy"] == 1.0
        assert report.metrics["citation_forgery_count"] == 0
        assert report.leakage == ()
        reds = [(r.case_id, r.verdict.labels)
                for r in report.records if not r.verdict.passed]
        assert reds == []

        after = store.conn.execute("SELECT COUNT(*) AS n FROM eval_runs").fetchone()["n"]
        assert after - before == 1
        case_rows = store.conn.execute("SELECT COUNT(*) AS n FROM eval_cases").fetchone()["n"]
        assert case_rows == 4
        locked = store.conn.execute(
            "SELECT DISTINCT locked_version FROM eval_cases").fetchall()
        assert [r["locked_version"] for r in locked] == ["golden-recorded-smoke-v1"]

        assert report.report_json.exists() and report.report_md.exists()
        payload = json.loads(report.report_json.read_text(encoding="utf-8"))
        assert payload["set_id"] == "golden-recorded-smoke-v1"
        assert {c["case_id"] for c in payload["cases"]} == {"R01", "R02", "R03", "R04"}
        md = report.report_md.read_text(encoding="utf-8")
        assert "不做 S1 门禁判定" in md
    finally:
        store.close()


class _FakeJudgeGateway:
    def __init__(self):
        self.seen_models: list[str] = []

    async def structured(self, messages, schema, **options):  # noqa: ANN001
        self.seen_models.append(options.get("model", ""))
        return {
            "data": {
                "claims": [{
                    "claim": "兜底机制存在且被引用", "supported": True,
                    "evidence_urn": "urn:testpilot:fs-docs:chunk:docs/kill-backup.md#背景",
                    "reason": "证据原文支撑",
                }],
                "relevance": {"passed": True, "reason": "切题"},
            },
            "degraded": False, "attempts": 1, "model": options.get("model"),
            "tokens_in": 50, "tokens_out": 20, "latency_ms": 100, "text": "{}",
        }


async def test_run_evaluation_attaches_semantic_judge_block(tmp_path):
    from testpilot_orchestrator.config_loader import load_profile
    from testpilot_orchestrator.factory import build_container
    from testpilot_workers.ingestion import run_ingestion

    subsystems = Path(__file__).resolve().parents[1]
    config = load_profile(subsystems / "config" / "profiles" / "offline-demo.yaml")
    config.paths["data_dir"] = str(tmp_path / "data")
    container = build_container(config, subsystems)
    store = container.get("store")
    try:
        await run_ingestion(container, trigger="pytest-eval-judge", full=True)
        golden = load_golden(FIXTURES / "golden_recorded_smoke.json")
        judge = _FakeJudgeGateway()
        report = await run_evaluation(
            container, golden.cases, set_id=golden.set_id, mode="recorded",
            report_dir=tmp_path / "reports", judge_gateway=judge,
            judge_model="deepseek-r1:7b",
        )
        # 只有 R01 是已回答正例题；3 个负例 unknown 不进语义裁判分母
        assert set(report.semantic["cases"]) == {"R01"}
        assert report.semantic["judge_model"] == "deepseek-r1:7b"
        assert report.semantic["judge_prompt_version"] == "semantic-judge-1"
        assert report.semantic["aggregate"]["faithfulness"] == 1.0
        assert report.semantic["cases"]["R01"]["adjudication"] is None  # 人工槽不预填
        assert judge.seen_models == ["deepseek-r1:7b"]  # 与生成模型不同体
        payload = json.loads(report.report_json.read_text(encoding="utf-8"))
        assert payload["semantic"]["cases"]["R01"]["faithfulness_passed"] is True
        md = report.report_md.read_text(encoding="utf-8")
        assert "语义裁判" in md and "deepseek-r1:7b" in md
        assert "无基线" in md  # kappa 在人工抽检前仍声明无基线
    finally:
        store.close()


async def test_run_evaluation_without_judge_keeps_no_baseline_declaration(tmp_path):
    from testpilot_orchestrator.config_loader import load_profile
    from testpilot_orchestrator.factory import build_container
    from testpilot_workers.ingestion import run_ingestion

    subsystems = Path(__file__).resolve().parents[1]
    config = load_profile(subsystems / "config" / "profiles" / "offline-demo.yaml")
    config.paths["data_dir"] = str(tmp_path / "data")
    container = build_container(config, subsystems)
    store = container.get("store")
    try:
        await run_ingestion(container, trigger="pytest-eval-nojudge", full=True)
        golden = load_golden(FIXTURES / "golden_recorded_smoke.json")
        report = await run_evaluation(
            container, golden.cases, set_id=golden.set_id, mode="recorded",
            report_dir=tmp_path / "reports",
        )
        assert report.semantic == {}
        md = report.report_md.read_text(encoding="utf-8")
        assert "本报告不含该数字" in md and "语义裁判（E5" not in md
    finally:
        store.close()


# ── E4：单题异常隔离（G22 基线实证：一题抛穿不得毁掉整批 30 题证据）────────────

class _ExplodingService:
    def __init__(self, exc: Exception):
        self._exc = exc

    async def ask(self, request, *, case_key: str = ""):  # noqa: ANN001
        raise self._exc


class _StubService:
    def __init__(self, row: dict):
        self._row = row
        self.asked: list[tuple] = []

    async def ask(self, request, *, case_key: str = ""):  # noqa: ANN001
        from types import SimpleNamespace

        self.asked.append((request.question, case_key))
        return SimpleNamespace(run_id="run-stub-1")

    async def get_run(self, run_id: str) -> dict:
        return self._row


async def test_run_one_case_captures_exception_without_aborting_batch():
    from testpilot_workers.eval_runner import run_one_case

    case = EvalCase(
        case_id="G99", kind="qa", question="q",
        ground_truth={"expected_intent": "code", "expected_status": "answered"},
        required_evidence=["docs/x.md"],
    )
    rec = await run_one_case(
        _ExplodingService(ValueError("boom")), case, "",
        project="demo", branch="main")

    assert rec.verdict.passed is False
    assert "execution_error" in rec.verdict.labels
    assert "ValueError: boom" in rec.verdict.detail["execution_error"]
    assert rec.run_id == ""  # 没有 run_id 就如实留空，绝不编造
    assert rec.result.status == "error"


async def test_run_one_case_happy_path_projects_row_and_versions():
    from testpilot_workers.eval_runner import run_one_case

    case = EvalCase(
        case_id="G01", kind="qa", question="q",
        ground_truth={"expected_intent": "incident", "expected_status": "answered"},
        required_evidence=["docs/kill-backup.md"],
    )
    svc = _StubService(_answered_row())
    rec = await run_one_case(svc, case, "k1", project="demo", branch="main")

    assert rec.run_id == "run-stub-1"
    assert rec.verdict.passed
    assert svc.asked == [("q", "k1")]
    assert rec.versions == {"data": "", "model": "recorded-local",
                            "prompt": "qa-prompt-3", "params": "qa-params-1"}


def test_render_markdown_flags_execution_errors():
    import dataclasses

    report = dataclasses.replace(
        _report("golden-s1-v1"),
        metrics={**_report("golden-s1-v1").metrics, "label_execution_error": 1.0},
    )
    md = render_markdown(report, gates_applicable=True)
    assert "执行异常" in md and "execution_error" in md


# ── E4：成对报告对比（单变量标定纪律）──────────────────────────────────────

def _report_payload(run_id: str, *, version: dict, cases: list[dict],
                    metrics: dict | None = None,
                    leakage: list | None = None) -> dict:
    base_metrics = {
        "cases_total": 2.0, "golden_passed": 1.0, "golden_pass_rate": 0.5,
        "recall_at_k": 0.5, "refusal_accuracy": 1.0, "intent_accuracy": 1.0,
        "citation_accuracy": 1.0, "citation_forgery_count": 0.0,
        "answered_count": 1.0, "latency_median_ms": 7000.0,
        "latency_p95_ms": 9000.0, "tokens_in_total": 2000.0,
        "tokens_out_total": 300.0, "attempts_total": 2.0,
        "label_recall_miss": 1.0,
    }
    if metrics:
        base_metrics.update(metrics)
    return {
        "eval_run_id": run_id, "set_id": "golden-s1-v1", "mode": "ollama",
        "version": version, "metrics": base_metrics,
        "leakage": leakage or [], "cases": cases,
    }


def _version(**over) -> dict:
    base = {"data_version": "d1", "model_version": "qwen2.5:7b",
            "prompt_version": "qa-prompt-3", "params_version": "qa-params-1",
            "degraded_path": False}
    base.update(over)
    return base


def test_compare_shows_metric_deltas_and_both_flip_directions():
    before = _report_payload("eval-before", version=_version(), cases=[
        {"case_id": "G01", "passed": False, "labels": ["recall_miss"]},
        {"case_id": "G02", "passed": True, "labels": []},
    ])
    after = _report_payload(
        "eval-after", version=_version(params_version="qa-params-2"),
        metrics={"golden_pass_rate": 1.0, "golden_passed": 2.0,
                 "recall_at_k": 1.0, "label_recall_miss": 0.0,
                 "label_no_citations": 1.0, "latency_median_ms": 6000.0},
        cases=[
            {"case_id": "G01", "passed": True, "labels": []},
            {"case_id": "G02", "passed": False, "labels": ["no_citations"]},
        ])
    diff = compare_reports(before, after)
    assert diff["slots_changed"] == ["params_version"]
    assert diff["discipline_warning"] == ""
    flips = {f["case_id"]: f["direction"] for f in diff["case_flips"]}
    assert flips == {"G01": "improved", "G02": "regressed"}  # 回归不许被藏
    rate_delta = next(d for d in diff["metric_deltas"]
                      if d["metric"] == "golden_pass_rate")
    assert rate_delta["delta"] == 0.5
    assert diff["label_deltas"] == {"no_citations": 1, "recall_miss": -1}
    md = render_comparison(diff)
    assert "regressed" in md and "improved" in md


def test_compare_flags_multi_variable_calibration():
    before = _report_payload("eval-before", version=_version(), cases=[])
    after = _report_payload(
        "eval-after",
        version=_version(params_version="qa-params-2", prompt_version="qa-prompt-4"),
        cases=[])
    diff = compare_reports(before, after)
    assert set(diff["slots_changed"]) == {"params_version", "prompt_version"}
    assert "单变量" in diff["discipline_warning"]
    assert "单变量守卫" in render_comparison(diff)


def test_compare_rejects_different_golden_sets():
    before = _report_payload("eval-before", version=_version(), cases=[])
    after = _report_payload("eval-after", version=_version(), cases=[])
    after["set_id"] = "golden-s1-v2"
    with pytest.raises(ValueError):
        compare_reports(before, after)
