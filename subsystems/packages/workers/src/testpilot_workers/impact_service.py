"""影响面分析 + MR 摘要 worker 服务（docs/08 FR-FRS-003，S4.3）。

编排层（取数 + 落账），推导规则全部在 :mod:`testpilot_core.impact` 纯函数：

- :func:`analyze_impact`：scm ChangeSet → 库内 URN 解析（复用 freshness 同口径）
  → 用例关系/用例/工单装配 → core 三分类影响链；
- :func:`build_mr_summary`：只从 diff 反推摘要，模型备注走变更符号 URN 白名单，
  拒入行随结果返回（fail-closed 幻觉台账，纪律对齐 S2.3）；
- :func:`precision_from_run`：以真实 TestRun.case_results 回算推荐精度
  （docs/06 回归选择有效率，门限 0.8），相关真值来自工单 related_case_ids。

零 schema 变更：本服务只读既有表/适配器，不新增任何持久化结构。
"""

from __future__ import annotations

from testpilot_core.enums import CaseStatus
from testpilot_core.errors import NotFoundError
from testpilot_core.freshness import pair_symbol_renames
from testpilot_core.impact import (
    CaseExecutionOutcome,
    ChangeSummaryResult,
    ImpactAnalysis,
    SelectionPrecision,
    build_impact_analysis,
    regression_selection_precision,
    summarize_change_set,
    supplement_summary,
)

from .freshness import resolve_change_symbols

# 工单夹具量级在百条以内；显式 limit 防默认 10 条截断历史缺陷链接
_TICKET_LIMIT = 500


async def _load_issues(container) -> list:
    """工单适配器缺省（stub profile）时返回空列表，不阻塞影响链主路径。"""
    tickets = container.get("tickets")
    if tickets is None:
        return []
    repo = container.get("repo")
    project = repo.project if repo is not None else "default"
    return await tickets.search("", project=project, limit=_TICKET_LIMIT)


async def analyze_impact(
    container,
    *,
    base_commit: str,
    head_commit: str,
    requirement_urns: list[str] | None = None,
) -> ImpactAnalysis:
    """影响链：变更符号 → 关联用例三分类 → 历史缺陷链接。"""
    scm = container.get("scm")
    repo = container.get("repo")
    store = container.get("store")
    tracer = container.get("tracer")
    if scm is None or repo is None:
        raise RuntimeError("容器未装配 scm/repo，无法做影响面分析")

    # 入口先把任意 rev（短 sha/分支名/HEAD）归一成全 sha：下游 ChangeSet、
    # ImpactAnalysis、impact-rerun 鲜度守卫与幂等身份只见 40 位 sha，
    # 用户输入长短不产生两套结果（2026-11-22 短 sha 误判 map 过期缺陷修复）。
    base_commit = await scm.resolve_commit(base_commit)
    head_commit = await scm.resolve_commit(head_commit)

    with tracer.span(
        "impact.analyze",
        run_id=f"impact-{base_commit[:8]}-{head_commit[:8]}",
        metadata={"base": base_commit[:12], "head": head_commit[:12]},
    ):
        change_set = await scm.get_change_set(repo, base_commit, head_commit)
        # rename 目标以库内 renamed_to 关系为准（resolve_change_symbols 内部补），
        # 配对逻辑只服务 MR 摘要，影响链分类不需要再跑一次
        changed_symbols, unresolved = resolve_change_symbols(store, change_set)
        # 影响面是机器回归选择消费者（FR-ING-004 允许低置信素材的自动化用途）：
        # 显式取低置信关系，检索挂链(0.50)/calls 传播(0.45/0.40)才能进入影响链；
        # 知识浏览 API 的 list_case_relations() 默认藏低置信口径不变。
        return build_impact_analysis(
            base_commit=base_commit,
            head_commit=head_commit,
            changed_symbols=changed_symbols,
            unresolved_symbols=unresolved,
            relations=store.list_case_relations(include_low=True),
            cases=store.list_test_cases(),
            issues=await _load_issues(container),
            requirement_urns=requirement_urns or [],
        )


async def build_mr_summary(
    container,
    *,
    base_commit: str,
    head_commit: str,
    model_rows: list[dict] | None = None,
    model_version: str = "",
) -> ChangeSummaryResult:
    """MR 摘要：diff 确定性脊柱 + 模型白名单备注（无模型行时纯确定性）。"""
    scm = container.get("scm")
    repo = container.get("repo")
    store = container.get("store")
    if scm is None or repo is None:
        raise RuntimeError("容器未装配 scm/repo，无法生成 MR 摘要")

    # 与 analyze_impact 同口径归一，保证两个端点的 ChangeSet 身份逐字一致
    base_commit = await scm.resolve_commit(base_commit)
    head_commit = await scm.resolve_commit(head_commit)
    change_set = await scm.get_change_set(repo, base_commit, head_commit)
    renames = pair_symbol_renames(
        change_set.symbol_changes, change_set.file_renames
    )
    summary = summarize_change_set(change_set, renames)
    if not model_rows:
        return ChangeSummaryResult(summary=summary)
    # 白名单 = 本次变更在库内解析到的真实符号 URN（解析不到的不允许挂备注）
    resolved, _unresolved = resolve_change_symbols(store, change_set)
    changed_urns = [symbol.urn for symbol in resolved]
    return supplement_summary(
        summary, changed_urns, model_rows, model_version=model_version,
    )


async def precision_from_run(
    container,
    *,
    analysis: ImpactAnalysis,
    run_id: str,
    full_suite_size: int = 0,
    gate_threshold: float = 0.8,
) -> SelectionPrecision:
    """以一次真实执行 run 回算推荐精度（FR-FRS-003 AC2）。

    相关真值（docs/06「缺陷命中」）：工单 ``related_case_ids`` 覆盖的用例。
    run 不存在响亮失败；CaseResult 的 external_case_id 在用例库映射不到 URN
    时跳过（不臆造 URN）。无票源时真值为空，precision 仍按执行口径可算，
    只是 related_hits 必然为 0——证据边界如实反映在数字里。
    """
    store = container.get("store")
    row = store.get_test_run_row(run_id)
    if row is None:
        raise NotFoundError(f"执行 run 不存在: {run_id}")
    run = store.row_to_test_run(row)

    urn_by_external = {
        case.external_id: case.urn for case in store.list_test_cases()
    }
    executed: list[CaseExecutionOutcome] = []
    for result in run.case_results:
        case_urn = urn_by_external.get(result.external_case_id)
        if case_urn:
            executed.append(CaseExecutionOutcome(
                case_urn=case_urn,
                status=CaseStatus(result.status),
            ))

    truth_urns: set[str] = set()
    for issue in await _load_issues(container):
        for external_id in issue.related_case_ids:
            urn = urn_by_external.get(external_id)
            if urn:
                truth_urns.add(urn)

    return regression_selection_precision(
        recommended=analysis.recommended_case_urns,
        executed=executed,
        related_truth=sorted(truth_urns),
        full_suite_size=full_suite_size,
        gate_threshold=gate_threshold,
    )
