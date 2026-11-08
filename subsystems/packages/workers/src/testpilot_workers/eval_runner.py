"""S1.6 离线评测 runner（FR-EV-002）：golden set → 问答服务 → core 裁判 → 落库与报告。

分工边界：
- 裁判逻辑（judge_case / aggregate_metrics / 泄漏检测）在 core，零 IO 纯函数，
  已有独立红→绿测试；本模块只做装配与适配，不重写任何判定规则。
- 双形态由调用方选择：recorded（offline-demo + 录制夹具，确定性，进 verify_s0 A9）
  与 ollama（hermetic 副本库 + 真模型，E4 基线/标定）。题集与裁判代码完全相同。
- 每个评测数字带四元组（data/model/prompt/params，docs/06 §6.1），随 eval_runs 落库；
  报告落 out/eval/（gitignore），题集本身入 git，改题走 git 评审（FR-EV-001 AC2）。
"""

from __future__ import annotations

import json
import uuid
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from testpilot_core.evaluation import (
    GENERATION_SIDE_LABELS,
    RECALL_SIDE_LABELS,
    CaseResult,
    CaseVerdict,
    aggregate_metrics,
    detect_holdout_leakage,
    judge_case,
)
from testpilot_core.models import AskRequest, EvalCase, EvalRun, EvalVersion
from testpilot_core.semantic_judge import aggregate_semantic
from testpilot_workers.semantic_judge import (
    DEFAULT_JUDGE_MODEL,
    JUDGE_PROMPT_VERSION,
    run_semantic_judgement,
)

# S1 门禁数值（docs/08 §13）：报告里只做对照展示，runner 不据此篡改任何指标
S1_GATES = (
    ("引用准确率", "citation_accuracy", 0.95, "且 citation_forgery_count=0"),
    ("Recall@K", "recall_at_k", 0.85, ""),
    ("拒答正确率", "refusal_accuracy", 0.90, ""),
    ("忠实度", None, 0.90, "E5 语义裁判接入前为「无基线」，不得造数"),
)


@dataclass(frozen=True)
class CaseRecord:
    """单题的完整判定留痕：裁判输入（投影后的 run）+ 裁判输出 + qa run 关联 id。"""

    case_id: str
    run_id: str
    verdict: CaseVerdict
    result: CaseResult
    versions: dict[str, str]


@dataclass(frozen=True)
class EvaluationReport:
    eval_run_id: str
    set_id: str
    mode: str  # recorded / ollama
    version: EvalVersion
    metrics: dict[str, float]
    records: tuple[CaseRecord, ...]
    leakage: tuple[str, ...]
    report_json: Path
    report_md: Path
    # E5：语义裁判块（空 dict = 本轮未跑语义裁判，忠实度保持「无基线」不造数）
    semantic: dict[str, Any] = field(default_factory=dict)


@dataclass
class _GoldenFile:
    set_id: str
    cases: list[EvalCase]


def load_golden(path: str | Path) -> _GoldenFile:
    """加载冻结题集 JSON → pydantic EvalCase 列表（结构不变量由 test_eval_golden_set 守）。"""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    cases = [EvalCase.model_validate(item) for item in payload["cases"]]
    return _GoldenFile(set_id=payload["set_id"], cases=cases)


def case_result_from_run(row: dict[str, Any]) -> CaseResult:
    """qa_runs 行 → core 裁判输入投影（纯函数；旧行缺列时走安全默认，不补造数据）。

    白名单口径与 QaService.get_run_source 完全一致：证据块 URN ∪ 证据所属文档
    URN ∪ 模型实际引用 URN——citation_forgery 判定必须用 run 自己的白名单。
    """
    evidence = row.get("evidence") or []
    citations = row.get("citations") or []
    evidence_urns = [e.get("urn", "") for e in evidence if e.get("urn")]
    citation_urns = [c.get("urn", "") for c in citations if c.get("urn")]
    whitelist = set(evidence_urns)
    whitelist.update(e.get("doc_urn", "") for e in evidence if e.get("doc_urn"))
    whitelist.update(citation_urns)
    whitelist.discard("")
    return CaseResult(
        status=str(row.get("status") or ""),
        intent=str(row.get("intent") or ""),
        evidence_urns=evidence_urns,
        citation_urns=citation_urns,
        whitelist_urns=sorted(whitelist),
        unknown_reason=str(row.get("unknown_reason") or ""),
        intent_source=str(row.get("intent_source") or ""),
        degraded=list(row.get("degraded") or []),
        attempts=int(row.get("attempts") or 0),
        latency_ms=int(row.get("latency_ms") or 0),
        tokens_in=int(row.get("tokens_in") or 0),
        tokens_out=int(row.get("tokens_out") or 0),
    )


def hermetic_copy_data_dir(src_data_dir: str | Path, dst_dir: str | Path) -> Path:
    """真模型评测前把演示库在线备份到隔离目录（P4 同款纪律：不碰活库/活进程）。

    sqlite online backup API 对 WAL 库一致且只读源库（不 truncate、不 checkpoint
    源文件），比字节拷贝 + 手拷 WAL 更稳。Chroma 走 HTTP 共享只读，不在 data_dir，
    无需复制；checkpoints/qa.sqlite 不拷，目标容器首次 ask 自建。
    """
    import sqlite3

    src_db = Path(src_data_dir) / "store" / "testpilot.sqlite"
    if not src_db.exists():
        raise FileNotFoundError(f"源库不存在: {src_db}")
    dst = Path(dst_dir)
    (dst / "store").mkdir(parents=True, exist_ok=True)
    dst_db = dst / "store" / "testpilot.sqlite"
    with sqlite3.connect(src_db) as source, sqlite3.connect(dst_db) as target:
        source.backup(target)
    return dst


def _error_record(case: EvalCase, exc: Exception) -> CaseRecord:
    """单题抛穿时的兜底留痕：记 execution_error，其余字段零值，绝不补造 run/证据。

    基线 G22 实证：装配守卫曾让一题异常炸掉整批 30 题评测。兜底后异常本身成为
    一条可见的失败证据（golden 不计通过），其余题目继续跑完。judge_case 仍对零值
    投影给出 intent_mismatch/unexpected_unknown 等标签，execution_error 是叠加的
     distinguished 标签，报告里单独成段，不混进 8 类裁判标签二分。
    """
    message = f"{type(exc).__name__}: {str(exc)[:300]}"
    result = CaseResult(
        status="error", intent="", evidence_urns=[], citation_urns=[],
        whitelist_urns=[], degraded=[message],
    )
    base = judge_case(case, result)
    verdict = CaseVerdict(
        case_id=case.case_id, kind=case.kind, passed=False,
        labels=[*base.labels, "execution_error"],
        detail={**base.detail, "execution_error": message},
    )
    return CaseRecord(
        case_id=case.case_id, run_id="", verdict=verdict, result=result,
        versions={"data": "", "model": "", "prompt": "", "params": ""},
    )


async def run_one_case(service, case: EvalCase, case_key: str, *,
                       project: str, branch: str) -> CaseRecord:
    """单题执行 + 投影 + 裁判；任何异常收口为 execution_error 记录而非抛穿。"""
    try:
        resp = await service.ask(
            AskRequest(question=case.question, project=project, branch=branch),
            case_key=case_key,
        )
        row = await service.get_run(resp.run_id)
        if row is None:  # 账本先持久化才返回 run_id，命中即缺陷而非静默放过
            raise RuntimeError(f"qa run 落库后查无此行: {resp.run_id}")
        result = case_result_from_run(row)
        verdict = judge_case(case, result)
        return CaseRecord(
            case_id=case.case_id, run_id=resp.run_id, verdict=verdict,
            result=result,
            versions={
                # data_version 来自 live-verify 的 HEAD 短 sha；未走验证的 run 为空串
                "data": str(row.get("data_version") or ""),
                "model": str(row.get("model_version") or ""),
                "prompt": str(row.get("prompt_version") or ""),
                "params": str(row.get("params_version") or ""),
            },
        )
    except Exception as exc:  # noqa: BLE001 — 评测视角：异常即失败证据，必须继续下一题
        return _error_record(case, exc)


async def _build_semantic_block(
    cases: list[EvalCase],
    records: list[CaseRecord],
    rows_by_run: dict[str, dict[str, Any]],
    judge_gateway: Any,
    judge_model: str,
) -> dict[str, Any]:
    """E5：对已回答的正例题跑隔离语义裁判；负例/unknown/error 不在语义裁判分母。

    人工裁定槽位 adjudication 恒为 None：专家抽检前 kappa 无基线，绝不预填。
    """
    items: list[tuple[str, str, list[dict[str, Any]], str]] = []
    case_ids: list[str] = []
    positive = {c.case_id: c for c in cases if c.kind != "negative"}
    for rec in records:
        if rec.case_id not in positive or rec.result.status != "answered" or not rec.run_id:
            continue
        row = rows_by_run.get(rec.run_id)
        if row is None:
            continue
        answer_md = str((row.get("answer_payload") or {}).get("markdown") or "")
        items.append((
            rec.case_id, positive[rec.case_id].question,
            list(row.get("evidence") or []), answer_md,
        ))
        case_ids.append(rec.case_id)

    verdicts = await run_semantic_judgement(
        judge_gateway, items, judge_model=judge_model) if items else {}
    per_case = {
        cid: {
            "faithfulness_passed": v.faithfulness_passed,
            "relevance_passed": v.relevance_passed,
            "faithfulness_ratio": round(v.faithfulness_ratio, 4),
            "parse_error": v.parse_error,
            "unsupported_claims": [
                {"claim": c.claim, "evidence_urn": c.evidence_urn, "reason": c.reason}
                for c in v.unsupported_claims
            ],
            "relevance_reason": v.relevance_reason,
            # 专家人工裁定入口（docs/06 §6.3）：抽检前恒为 null，kappa 无基线不造数
            "adjudication": None,
        }
        for cid, v in verdicts.items()
    }
    return {
        "judge_model": judge_model,
        "judge_prompt_version": JUDGE_PROMPT_VERSION,
        "aggregate": aggregate_semantic(list(verdicts.values())),
        "cases": per_case,
    }


def _dominant(rows: list[dict[str, Any]], key: str) -> str:
    """四元组版本位：取该批 run 中出现频次最高的非空值；全空如实留空串，不填默认值。"""
    counter = Counter(str(r.get(key) or "") for r in rows)
    counter.pop("", None)
    return counter.most_common(1)[0][0] if counter else ""


def _gate_line(name: str, metrics: dict[str, float], key: str | None,
               threshold: float, extra: str) -> str:
    if key is None:
        return f"| {name} | ≥{threshold:.2f} | 无基线（{extra}） | — |"
    actual = metrics.get(key)
    if actual is None:
        return f"| {name} | ≥{threshold:.2f} | 无分母未输出 | — |"
    mark = "✅" if actual >= threshold else "❌"
    suffix = f"（{extra}）" if extra else ""
    return f"| {name}{suffix} | ≥{threshold:.2f} | {actual:.4f} | {mark} |"


def render_markdown(report: EvaluationReport, *, gates_applicable: bool) -> str:
    """渲染人读报告：四元组 → 门禁对照 → 指标 → 错误二分 → 逐题 → 泄漏 → 无基线声明。"""
    m = report.metrics
    v = report.version
    lines = [
        f"# 评测报告 {report.eval_run_id}",
        "",
        f"- 题集：`{report.set_id}`；形态：`{report.mode}`；题数：{int(m['cases_total'])}",
        f"- 四元组：data=`{v.data_version or '（空）'}` / model=`{v.model_version or '（空）'}`"
        f" / prompt=`{v.prompt_version or '（空）'}` / params=`{v.params_version or '（空）'}`",
        f"- degraded_path：`{v.degraded_path}`（任一 run 带降级标记即为 true）",
        "",
    ]
    if gates_applicable:
        lines += ["## S1 门禁对照（docs/08 §13）", "",
                  "| 指标 | 门禁 | 实测 | 判定 |", "|---|---|---|---|"]
        for name, key, threshold, extra in S1_GATES:
            if key is None and report.semantic:
                agg = report.semantic.get("aggregate", {})
                actual = agg.get("faithfulness")
                if actual is None:
                    lines.append(f"| {name} | ≥{threshold:.2f} | 无分母未输出 | — |")
                else:
                    mark = "✅" if actual >= threshold else "❌"
                    lines.append(
                        f"| {name}（{report.semantic['judge_model']} / "
                        f"{report.semantic['judge_prompt_version']}） | "
                        f"≥{threshold:.2f} | {actual:.4f} | {mark} |")
            else:
                lines.append(_gate_line(name, m, key, threshold, extra))
        forged = int(m.get("citation_forgery_count", 0))
        lines.append(f"| 引用伪造数 | =0 | {forged} | {'✅' if forged == 0 else '❌'} |")
        lines.append("")
    else:
        lines += [
            "## 门禁对照",
            "",
            "机控题集（recorded smoke）只验证评测链路装配，**不做 S1 门禁判定**；",
            "门禁数字以 `golden-s1-v1` 的 ollama hermetic 报告为准。",
            "",
        ]
    lines += [
        "## 指标汇总",
        "",
        f"- golden 通过率：{m['golden_passed']:.0f}/{m['cases_total']:.0f}"
        f" = {m['golden_pass_rate']:.4f}",
        f"- Recall@K：{m['recall_at_k']:.4f}；拒答正确率：{m['refusal_accuracy']:.4f}",
        f"- 意图准确率：{m['intent_accuracy']:.4f}；引用准确率：{m['citation_accuracy']:.4f}"
        f"（已回答 {m['answered_count']:.0f} 题，伪造 {m['citation_forgery_count']:.0f} 次）",
        f"- 延迟中位 {m['latency_median_ms']:.0f} ms / p95 {m['latency_p95_ms']:.0f} ms；"
        f"token 入 {m['tokens_in_total']:.0f} / 出 {m['tokens_out_total']:.0f}；"
        f"总尝试 {m['attempts_total']:.0f}",
        "",
        "## 错误标签二分（docs/06 §6：召回侧/生成侧分别记账，禁止只报总分）",
        "",
        "| 侧 | 标签 | 次数 |",
        "|---|---|---|",
    ]
    for side_name, labels in (("召回侧", RECALL_SIDE_LABELS), ("生成侧", GENERATION_SIDE_LABELS)):
        for label in sorted(labels):
            lines.append(f"| {side_name} | {label} | {int(m.get(f'label_{label}', 0))} |")
    execution_errors = int(m.get("label_execution_error", 0))
    if execution_errors:
        lines += [
            "",
            f"⚠️ **执行异常 {execution_errors} 题**（未走完问答链路，标签 execution_error "
            "不计入上面的 8 类裁判二分；异常类型/文案见逐题 detail 与 qa trace）",
        ]
    lines += ["", "## 逐题判定", "",
              "| case | kind | status | intent | 通过 | 标签 | 延迟 ms |",
              "|---|---|---|---|---|---|---|"]
    for rec in report.records:
        verdict = rec.verdict
        lines.append(
            f"| {rec.case_id} | {verdict.kind} | {rec.result.status or '-'} "
            f"| {rec.result.intent or '-'} | {'✅' if verdict.passed else '❌'} "
            f"| {','.join(verdict.labels) or '—'} | {rec.result.latency_ms} |"
        )
    lines += ["", "## holdout 泄漏检查（FR-EV-001）", ""]
    if report.leakage:
        lines.append(f"❌ 发现 holdout marker 出现在已索引 URN：{list(report.leakage)}")
    else:
        lines.append("✅ holdout marker 均未出现在 source_documents（无泄漏）")
    if report.semantic:
        agg = report.semantic.get("aggregate", {})
        lines += [
            "## 语义裁判（E5：隔离模型 LLM-as-judge，docs/06 §6.3）",
            "",
            f"- 裁判模型：`{report.semantic['judge_model']}`（与生成模型不同体）；"
            f"裁判 prompt 版本：`{report.semantic['judge_prompt_version']}`",
            f"- 忠实度（陈述级微平均，门 ≥.90）：{agg.get('faithfulness', 0):.4f}；"
            f"逐题全支撑率：{agg.get('faithfulness_case_pass_rate', 0):.4f}",
            f"- 答案相关性（门 ≥.85）：{agg.get('answer_relevance', 0):.4f}；"
            f"裁判信封解析失败 {agg.get('parse_error_count', 0):.0f} 题（fail-closed）",
            "- 人工裁定：每题 adjudication 槽位为 null，专家 ≥10% 抽检前 kappa 无基线。",
            "",
        ]
        bad = [(cid, c) for cid, c in report.semantic.get("cases", {}).items()
               if c.get("parse_error") or not c["faithfulness_passed"]
               or not c["relevance_passed"]]
        if bad:
            lines += ["| case | 忠实 | 相关 | 不支撑陈述/解析错误 |", "|---|---|---|---|"]
            for cid, c in bad:
                reason = c["parse_error"] or "; ".join(
                    x["claim"][:40] for x in c["unsupported_claims"]) or "—"
                lines.append(
                    f"| {cid} | {'✅' if c['faithfulness_passed'] else '❌'} "
                    f"| {'✅' if c['relevance_passed'] else '❌'} | {reason} |")
            lines.append("")
    lines += [
        "",
        "## 无基线声明（FR-EV-003 AC2）",
        "",
    ]
    if report.semantic:
        lines.append("- 语义忠实度/相关性已由隔离裁判模型产出（见上节）；"
                     "人工裁定 kappa 在专家抽检前仍为「无基线」，不得预填。")
    else:
        lines.append("- 忠实度/相关性语义指标由 E5 隔离裁判模型（deepseek-r1:7b ≠ 生成模型）"
                     "产出，本报告不含该数字，任何位置不得用结构化裁判分数代替或编造。")
    lines.append("")
    return "\n".join(lines)


async def run_evaluation(
    container,
    cases: list[EvalCase],
    *,
    set_id: str,
    mode: str,
    report_dir: str | Path,
    case_key_fn: Callable[[EvalCase], str] | None = None,
    judge_gateway: Any | None = None,
    judge_model: str = DEFAULT_JUDGE_MODEL,
) -> EvaluationReport:
    """跑完整评测并落库/落报告。调用方负责 service 之外的生命周期（ingestion、关 store）。"""
    # 延迟导入：workers 依赖 orchestrator，保持模块顶层只 import core 契约
    from testpilot_orchestrator.qa_graph.service import build_qa_service

    if case_key_fn is None:
        case_key_fn = lambda c: str(c.ground_truth.get("recorded_case_key", ""))  # noqa: E731

    store = container.get("store")
    config = container.config
    report_dir = Path(report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    eval_run_id = f"eval-{uuid.uuid4().hex}"

    service = await build_qa_service(container)
    records: list[CaseRecord] = []
    raw_rows: list[dict[str, Any]] = []
    try:
        for case in cases:
            record = await run_one_case(
                service, case, case_key_fn(case),
                project=config.project.name,
                branch=config.project.default_branch,
            )
            records.append(record)
            if record.run_id:  # 异常兜底记录没有 qa_runs 行，不进版本位统计
                row = await service.get_run(record.run_id)
                if row is not None:
                    raw_rows.append(row)
    finally:
        await service.aclose()

    metrics = aggregate_metrics([(c, r.verdict, r.result)
                                 for c, r in zip(cases, records)])

    # 泄漏检查：holdout 题声明的文档 marker 不得出现在任何已索引文档 URN
    markers = [str(c.ground_truth["holdout_doc"])
               for c in cases if c.holdout and c.ground_truth.get("holdout_doc")]
    leakage = tuple(detect_holdout_leakage(markers, store.all_document_urns()))

    version = EvalVersion(
        data_version=_dominant(raw_rows, "data_version"),
        model_version=_dominant(raw_rows, "model_version"),
        prompt_version=_dominant(raw_rows, "prompt_version"),
        params_version=_dominant(raw_rows, "params_version"),
        degraded_path=any(r.result.degraded for r in records),
    )

    # E5 语义裁判（可选）：只裁已回答正例题；不传 judge_gateway 时忠实度保持「无基线」
    semantic: dict[str, Any] = {}
    if judge_gateway is not None:
        rows_by_run = {row["run_id"]: row for row in raw_rows}
        semantic = await _build_semantic_block(
            cases, records, rows_by_run, judge_gateway, judge_model)

    # 题集冻结落库（幂等）+ 评测 run 落库；先账本后报告文件，中断也能从 eval_runs 对账
    for case in cases:
        store.upsert_eval_case(case, locked_version=set_id)

    report = EvaluationReport(
        eval_run_id=eval_run_id, set_id=set_id, mode=mode, version=version,
        metrics=metrics, records=tuple(records), leakage=leakage,
        report_json=report_dir / f"{eval_run_id}.json",
        report_md=report_dir / f"{eval_run_id}.md", semantic=semantic,
    )
    json_payload = {
        "eval_run_id": eval_run_id,
        "set_id": set_id,
        "mode": mode,
        "version": {
            "data_version": version.data_version,
            "model_version": version.model_version,
            "prompt_version": version.prompt_version,
            "params_version": version.params_version,
            "degraded_path": version.degraded_path,
        },
        "metrics": metrics,
        "leakage": list(leakage),
        # E5：未跑语义裁判时为 {}，报告对应位置保持「无基线」声明，不造数
        "semantic": semantic,
        "cases": [
            {
                "case_id": rec.case_id,
                "qa_run_id": rec.run_id,
                "kind": rec.verdict.kind,
                "passed": rec.verdict.passed,
                "labels": rec.verdict.labels,
                "recall_labels": rec.verdict.recall_labels,
                "generation_labels": rec.verdict.generation_labels,
                "detail": rec.verdict.detail,
                "result": {
                    "status": rec.result.status,
                    "intent": rec.result.intent,
                    "unknown_reason": rec.result.unknown_reason,
                    "intent_source": rec.result.intent_source,
                    "degraded": rec.result.degraded,
                    "attempts": rec.result.attempts,
                    "latency_ms": rec.result.latency_ms,
                    "tokens_in": rec.result.tokens_in,
                    "tokens_out": rec.result.tokens_out,
                    "evidence_urns": rec.result.evidence_urns,
                    "citation_urns": rec.result.citation_urns,
                },
                "versions": rec.versions,
            }
            for rec in records
        ],
    }
    report.report_json.write_text(
        json.dumps(json_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    report.report_md.write_text(
        render_markdown(report, gates_applicable=set_id.startswith("golden-s1")),
        encoding="utf-8")

    store.add_eval_run(EvalRun(
        eval_run_id=eval_run_id,
        version=version,
        metrics=metrics,
        report_uri=str(report.report_md),
    ))
    return report


# ── E4：成对报告对比（单变量标定纪律，docs/06 §6.1.4：回归也必须展示）────────────

# 只对比有解释意义的数值指标；cases_total/golden_passed 等账面量由逐题翻转覆盖
_DIFF_METRIC_KEYS = (
    "golden_pass_rate", "recall_at_k", "refusal_accuracy", "intent_accuracy",
    "citation_accuracy", "citation_forgery_count", "answered_count",
    "latency_median_ms", "latency_p95_ms",
    "tokens_in_total", "tokens_out_total", "attempts_total",
)
_VERSION_SLOTS = ("data_version", "model_version", "prompt_version", "params_version")


def load_report_json(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def compare_reports(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """两份评测 JSON 的结构化 diff：指标差、逐题翻转、标签差、四元组单变量守卫。"""
    if before.get("set_id") != after.get("set_id"):
        raise ValueError(
            f"题集不一致，拒绝对比：{before.get('set_id')} vs {after.get('set_id')}")

    before_v, after_v = before.get("version", {}), after.get("version", {})
    slots_changed = [s for s in _VERSION_SLOTS if before_v.get(s) != after_v.get(s)]
    # 标定只允许动一个版本位；>1 即破坏单变量纪律（degraded_path 翻转只记账不拦）
    discipline_warning = ""
    if len(slots_changed) > 1:
        discipline_warning = (
            f"四元组有 {len(slots_changed)} 个位同时变化 {slots_changed}，"
            "违反单变量标定纪律，指标差不能归因于单一改动")

    bm, am = before.get("metrics", {}), after.get("metrics", {})
    metric_deltas = [
        {"metric": key, "before": bm.get(key, 0.0), "after": am.get(key, 0.0),
         "delta": round(am.get(key, 0.0) - bm.get(key, 0.0), 6)}
        for key in _DIFF_METRIC_KEYS if key in bm or key in am
    ]

    before_cases = {c["case_id"]: c for c in before.get("cases", [])}
    after_cases = {c["case_id"]: c for c in after.get("cases", [])}
    flips: list[dict[str, Any]] = []
    for case_id in sorted(set(before_cases) | set(after_cases)):
        b, a = before_cases.get(case_id), after_cases.get(case_id)
        if b is None or a is None or b.get("passed") == a.get("passed"):
            continue
        flips.append({
            "case_id": case_id,
            "direction": "regressed" if b.get("passed") and not a.get("passed")
            else "improved",
            "before_labels": b.get("labels", []),
            "after_labels": a.get("labels", []),
        })

    label_deltas: dict[str, int] = {}
    for label in sorted(RECALL_SIDE_LABELS | GENERATION_SIDE_LABELS):
        delta = int(am.get(f"label_{label}", 0)) - int(bm.get(f"label_{label}", 0))
        if delta:
            label_deltas[label] = delta

    return {
        "set_id": before.get("set_id"),
        "before_eval_run_id": before.get("eval_run_id"),
        "after_eval_run_id": after.get("eval_run_id"),
        "slots_changed": slots_changed,
        "version_before": before_v,
        "version_after": after_v,
        "degraded_path_before": bool(before_v.get("degraded_path")),
        "degraded_path_after": bool(after_v.get("degraded_path")),
        "discipline_warning": discipline_warning,
        "metric_deltas": metric_deltas,
        "case_flips": flips,
        "label_deltas": label_deltas,
        "leakage_before": before.get("leakage", []),
        "leakage_after": after.get("leakage", []),
    }


def render_comparison(diff: dict[str, Any]) -> str:
    lines = [
        f"# 成对评测对比（{diff['set_id']}）",
        "",
        f"- before：`{diff['before_eval_run_id']}` → after：`{diff['after_eval_run_id']}`",
        f"- 四元组变化位：{', '.join(diff['slots_changed']) or '（无）'}"
        f"；degraded_path：{diff['degraded_path_before']} → {diff['degraded_path_after']}",
    ]
    if diff["discipline_warning"]:
        lines += [f"- ⚠️ **单变量守卫**：{diff['discipline_warning']}"]
    lines += ["", "## 指标差（after − before）", "",
              "| 指标 | before | after | Δ |", "|---|---|---|---|"]
    for d in diff["metric_deltas"]:
        lines.append(
            f"| {d['metric']} | {d['before']:.4f} | {d['after']:.4f} "
            f"| {d['delta']:+.4f} |")
    lines += ["", "## 逐题翻转（回归与改进同等展示，docs/06 §6.1.4）", ""]
    if diff["case_flips"]:
        lines += ["| case | 方向 | before 标签 | after 标签 |", "|---|---|---|---|"]
        for f in diff["case_flips"]:
            lines.append(
                f"| {f['case_id']} | {f['direction']} "
                f"| {','.join(f['before_labels']) or '—'} "
                f"| {','.join(f['after_labels']) or '—'} |")
    else:
        lines.append("无逐题翻转。")
    lines += ["", "## 错误标签计数差（正=新增，负=消除）", ""]
    if diff["label_deltas"]:
        for label, delta in diff["label_deltas"].items():
            lines.append(f"- {label}: {delta:+d}")
    else:
        lines.append("无变化。")
    if diff["leakage_before"] or diff["leakage_after"]:
        lines += ["", f"## 泄漏：before={diff['leakage_before']} after={diff['leakage_after']}"]
    lines.append("")
    return "\n".join(lines)
