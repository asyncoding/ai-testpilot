"""账本/端口组合的归因证据提供者（S3.5c，FR-EX-007 六 seam 真实接线）。

:class:`~testpilot_workers.attribution_service.NullEvidenceProvider` 代表"零外部
证据"的 fail-closed 底线；本模块是它的量产实现：把执行账本（test_runs 历史）、
os_log 端口、git 变更集端口与可注入的断言/变更记录来源，归一为 core 归因引擎
吃的六类结构化证据。所有端口缺失或不可达时**降级为空证据而非硬归因**——引擎
随后裁决 needs_human（AC4），与 Null 底线行为一致。

六 seam 的证据来源与口径：

- ``reruns``：test_runs 账本真读。当前失败轮自身作为一条观察（触发轮也是同 commit
  的一次执行事实），叠加同 project 历史 run 中同 external_case_id 的终态观察；
  queued/running（raw_json 为空）与 skipped 观察不计入。
- ``assertion_check``：注入式异步来源 ``assertion_source(case, run)``。源码级
  常量比对在 POC 期没有确定性实现，留接缝由合成夹具/未来真实 oracle 喂料，
  不注入即为 None（不硬判用例过期）。
- ``log_signals``：注入的 os_log 端口（OsLogQueryAdapter 同构即可）按配置的
  可复现谓词逐条查询，时间窗取 run.started_at/finished_at（缺任一则跳过：
  不可复现的窗口不构成证据）。零命中不产生信号（避免噪声账本），结果按
  (run, 谓词) 缓存，多用例/双 seam 共用不重复起进程。
- ``diff_signal``：git 端口的 ChangeSet（base 由 profile 配置 diff_base，
  head=run.commit_sha）映射为 DiffSignal；堆栈符号与变更符号做保守后缀匹配。
- ``environment_signals``：由同批 os_log 条目的信号族分类派生（授权竞态/能力
  缺口/生命周期后台），未覆盖层随 run.uncovered_layers 透传，明示哪层只能生产验。
- ``change_records``：默认把 git ChangeSet 本身作为产品变更记录（一条，符号取
  并集）；可注入 ``change_record_source`` 改走工单系统等来源。

边界：本层只做组包与端口调用，判定规则全在 core.attribution（零 IO）；端口抛
AdapterUnavailable/PathMissing 属"证据不可达"，捕获后降级；契约违例类异常不吞。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from testpilot_core.attribution import (
    AssertionCheck,
    ChangeRecord,
    DiffSignal,
    EnvironmentSignal,
    LogSignal,
    RerunRecord,
)
from testpilot_core.enums import CaseStatus
from testpilot_core.errors import AdapterUnavailableError, PathMissingAtCommitError
from testpilot_core.models import CaseResult, RepoRef, TestRun

logger = logging.getLogger("testpilot.workers.evidence")

#: 进入重跑观察的用例终态：skipped 不是执行事实，不参与 flaky/复现判定。
_TERMINAL_OBSERVED = (CaseStatus.PASSED, CaseStatus.FAILED, CaseStatus.ERROR)

#: os_log 条目 → 环境信号族关键词表（匹配 subsystem/category/message 拼接小写体）。
#: 调表即调证据口径，改动需在 docs/09 术语表与本注释同步留痕。
_LOG_FAMILY_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("authorization_race", ("authorization", "permission", "tcc", "deny")),
    ("capability_gap", ("entitlement", "unsupported", "capability",
                        "unrecognized selector")),
    ("lifecycle_background", ("background", "suspend", "lifecycle", "launch")),
)

#: 断言来源签名：(case, run) -> AssertionCheck | None（异步、可注入纯比对）。
AssertionSource = Callable[[CaseResult, TestRun], Awaitable[AssertionCheck | None]]
#: 变更记录来源签名：拿到已取到的 ChangeSet（core 模型）自行决定如何落 ChangeRecord。
ChangeRecordSource = Callable[[CaseResult, TestRun, Any], Awaitable[list[ChangeRecord]]]


def classify_log_families(entry: dict[str, Any]) -> list[str]:
    """纯函数：把一条 os_log 条目按关键词归入信号族（无命中返回空表）。"""
    text = " ".join(
        str(entry.get(key, "")) for key in ("subsystem", "category", "message")
    ).lower()
    return [
        family
        for family, keywords in _LOG_FAMILY_RULES
        if any(keyword in text for keyword in keywords)
    ]


def _stack_match(
    stack_top: str, changed_symbols: Sequence[str]
) -> tuple[str, bool]:
    """保守匹配失败堆栈符号是否落在本次变更符号集内。

    stack_top 形态既可能是限定符号（``ReminderScheduler.backupFallback()``）也可能
    是文件行号（``AlarmMissionTests.swift:88``）；只做全等与双向后缀匹配，拿不准
    一律返回未命中——真缺陷规则不允许排除法凑证据。
    """
    if not stack_top:
        return "", False
    for symbol in changed_symbols:
        if not symbol:
            continue
        if stack_top == symbol or stack_top.endswith(symbol) or symbol.endswith(stack_top):
            return symbol, True
    return stack_top, False


class StoreBackedEvidenceProvider:
    """六 seam 真实接线；所有端口可选，缺谁降级谁，整机永不硬归因。"""

    def __init__(
        self,
        store,
        *,
        repo: RepoRef | None = None,
        logs: Any | None = None,
        scm: Any | None = None,
        log_predicates: Sequence[str] = (),
        diff_base: str | None = None,
        assertion_source: AssertionSource | None = None,
        change_record_source: ChangeRecordSource | None = None,
    ) -> None:
        self._store = store
        self._repo = repo
        self._logs = logs
        self._scm = scm
        self._predicates = list(log_predicates)
        self._diff_base = diff_base
        self._assertion_source = assertion_source
        self._change_record_source = change_record_source
        # run_id -> [(predicate, query 返回体)]；双 seam 共用、多用例不重复查询
        self._log_cache: dict[str, list[tuple[str, dict[str, Any]]]] = {}
        # "base..head" -> ChangeSet；只缓存成功结果，不可达不粘住后续重试
        self._change_set_cache: dict[str, Any] = {}

    # ── seam 1：重跑历史（账本真读）──────────────────────────────────────────

    async def reruns(
        self, case: CaseResult, run: TestRun
    ) -> list[RerunRecord]:
        records: list[RerunRecord] = []
        for row in self._store.list_test_runs(project=run.project):
            if row["run_id"] == run.run_id:
                continue  # 当前轮在末尾单独补，避免归因时刻 raw_json 未落的时序问题
            historic = self._store.row_to_test_run(row)
            if historic is None:
                continue  # queued/running 幻影行：尚无结构化结论，不构成观察
            observed = next(
                (
                    c
                    for c in historic.case_results
                    if c.external_case_id == case.external_case_id
                ),
                None,
            )
            if observed is None or observed.status not in _TERMINAL_OBSERVED:
                continue
            records.append(
                RerunRecord(
                    run_id=historic.run_id,
                    commit_sha=historic.commit_sha,
                    status=observed.status,
                )
            )
        # 当前失败轮自身是同 commit 的一次执行事实（flaky 需 passed+failed 双态，
        # 复现缺陷计失败次数）；账本行此刻 raw_json 尚未回写，故直接用入参补。
        records.append(
            RerunRecord(
                run_id=run.run_id, commit_sha=run.commit_sha, status=case.status
            )
        )
        return records

    # ── seam 2：断言对照（注入来源）──────────────────────────────────────────

    async def assertion_check(
        self, case: CaseResult, run: TestRun
    ) -> AssertionCheck | None:
        if self._assertion_source is None:
            return None
        return await self._assertion_source(case, run)

    # ── seam 3：os_log 信号（端口 + 可复现谓词 + 缓存）────────────────────────

    async def _run_log_queries(
        self, run: TestRun
    ) -> list[tuple[str, dict[str, Any]]]:
        cached = self._log_cache.get(run.run_id)
        if cached is not None:
            return cached
        results: list[tuple[str, dict[str, Any]]] = []
        if self._logs is None or not self._predicates:
            self._log_cache[run.run_id] = results
            return results
        if run.started_at is None or run.finished_at is None:
            # 时间窗不全则查询不可复现（FR-EX-006 AC1），宁缺证不硬查
            logger.warning(
                "run %s 缺 started/finished 时间窗，跳过 os_log 归因证据", run.run_id
            )
            self._log_cache[run.run_id] = results
            return results
        start_iso = run.started_at.isoformat()
        end_iso = run.finished_at.isoformat()
        for predicate in self._predicates:
            try:
                result = await self._logs.query(
                    run_id=run.run_id,
                    predicate=predicate,
                    start_time=start_iso,
                    end_time=end_iso,
                )
            except (AdapterUnavailableError, PathMissingAtCommitError) as exc:
                logger.warning(
                    "os_log 端口不可达，降级为空日志证据 run=%s predicate=%r: %s",
                    run.run_id, predicate, exc,
                )
                continue
            results.append((predicate, result))
        self._log_cache[run.run_id] = results
        return results

    async def log_signals(
        self, case: CaseResult, run: TestRun
    ) -> list[LogSignal]:
        signals: list[LogSignal] = []
        for idx, (predicate, result) in enumerate(
            await self._run_log_queries(run), start=1
        ):
            entries = result.get("entries", [])
            if not entries:
                continue  # 零命中是噪声不是证据，不进账本
            families = sorted({
                family
                for entry in entries
                for family in classify_log_families(entry)
            })
            signals.append(
                LogSignal(
                    query_ref=f"oslog:{run.run_id}:{idx}",
                    predicate=predicate,
                    matched_count=len(entries),
                    families=families,
                )
            )
        return signals

    # ── seam 4：diff 信号（git ChangeSet 端口）────────────────────────────────

    async def _change_set(self, run: TestRun) -> Any | None:
        if self._scm is None or not self._diff_base or self._repo is None:
            return None
        key = f"{self._diff_base}..{run.commit_sha}"
        cached = self._change_set_cache.get(key)
        if cached is not None:
            return cached
        try:
            change_set = await self._scm.get_change_set(
                self._repo, self._diff_base, run.commit_sha
            )
        except (AdapterUnavailableError, PathMissingAtCommitError) as exc:
            # 证据不可达 ≠ 无变更：不缓存 None，端口恢复后下个 run 自动重试
            logger.warning(
                "git 变更集不可达，diff/变更记录证据降级为空 run=%s: %s",
                run.run_id, exc,
            )
            return None
        self._change_set_cache[key] = change_set
        return change_set

    async def diff_signal(
        self, case: CaseResult, run: TestRun
    ) -> DiffSignal | None:
        change_set = await self._change_set(run)
        if change_set is None:
            return None
        changed_symbols = sorted({sc.symbol_id for sc in change_set.symbol_changes})
        stack_symbol, changed = _stack_match(case.stack_top, changed_symbols)
        return DiffSignal(
            base=change_set.base_commit,
            head=change_set.head_commit,
            changed_symbols=changed_symbols,
            stack_symbol=stack_symbol,
            stack_symbol_changed=changed,
        )

    # ── seam 5：环境信号（日志族分类派生，未覆盖层透传）────────────────────────

    async def environment_signals(
        self, case: CaseResult, run: TestRun
    ) -> list[EnvironmentSignal]:
        queries = await self._run_log_queries(run)
        detail_by_family: dict[str, str] = {}
        for _predicate, result in queries:
            for entry in result.get("entries", []):
                for family in classify_log_families(entry):
                    detail_by_family.setdefault(
                        family, str(entry.get("message", ""))[:200]
                    )
        return [
            EnvironmentSignal(
                family=family,
                detail=detail,
                # 模拟器日志派生的环境证据，天然盖不住真机/签名等声明层
                uncovered_layers=list(run.uncovered_layers),
            )
            for family, detail in sorted(detail_by_family.items())
        ]

    # ── seam 6：产品变更记录（ChangeSet 默认派生，可注入工单来源）──────────────

    async def change_records(
        self, case: CaseResult, run: TestRun
    ) -> list[ChangeRecord]:
        change_set = await self._change_set(run)
        if change_set is None:
            return []
        if self._change_record_source is not None:
            return await self._change_record_source(case, run, change_set)
        symbols = sorted({sc.symbol_id for sc in change_set.symbol_changes})
        return [
            ChangeRecord(
                ref=f"git:{change_set.head_commit[:8]}",
                summary=(
                    f"变更集 {change_set.base_commit[:8]}..{change_set.head_commit[:8]}"
                    f"：{len(change_set.changed_files)} 文件 / {len(symbols)} 符号变更"
                ),
                symbols=symbols,
            )
        ]
