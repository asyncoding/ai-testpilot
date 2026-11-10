"""失败归因组包服务（docs/08 FR-EX-007，S3.4）。

把端口层证据（重跑历史、断言对照、os_log、diff、环境信号、变更记录）经可注入
的 :class:`AttributionEvidenceProvider` 归一为 core 的 :class:`FailureEvidence`，
再调纯引擎 :func:`attribute_failure` 裁决。

边界：

- core 管判定（零 IO），本层管组包（async 端口调用），adapters 管真实 IO；
- 本层**不持久化**：verdict 落库、HTTP API、详情页归 S3.5；
- 证据账本随结构化信号同步生成，保证 engine 的"信号必可回溯"校验始终满足；
- 只对 failed/error 用例归因，passed/skipped 不进归因流程。
"""

from __future__ import annotations

from typing import Protocol

from testpilot_core.attribution import (
    AssertionCheck,
    AttributionVerdict,
    ChangeRecord,
    DiffSignal,
    EnvironmentSignal,
    EvidenceItem,
    EvidenceKind,
    FailureEvidence,
    LogSignal,
    RerunRecord,
    attribute_failure,
)
from testpilot_core.enums import CaseStatus
from testpilot_core.models import CaseResult, TestRun


class AttributionEvidenceProvider(Protocol):
    """归因证据提供者接缝；S3.5 由适配器/账本组合实现，测试用假件。"""

    async def reruns(self, case: CaseResult, run: TestRun) -> list[RerunRecord]: ...

    async def assertion_check(
        self, case: CaseResult, run: TestRun
    ) -> AssertionCheck | None: ...

    async def log_signals(
        self, case: CaseResult, run: TestRun
    ) -> list[LogSignal]: ...

    async def diff_signal(
        self, case: CaseResult, run: TestRun
    ) -> DiffSignal | None: ...

    async def environment_signals(
        self, case: CaseResult, run: TestRun
    ) -> list[EnvironmentSignal]: ...

    async def change_records(
        self, case: CaseResult, run: TestRun
    ) -> list[ChangeRecord]: ...


class NullEvidenceProvider:
    """全空提供者：无任何外部证据时引擎应一律裁决 needs_human（AC4）。"""

    async def reruns(self, case: CaseResult, run: TestRun) -> list[RerunRecord]:
        return []

    async def assertion_check(
        self, case: CaseResult, run: TestRun
    ) -> AssertionCheck | None:
        return None

    async def log_signals(
        self, case: CaseResult, run: TestRun
    ) -> list[LogSignal]:
        return []

    async def diff_signal(
        self, case: CaseResult, run: TestRun
    ) -> DiffSignal | None:
        return None

    async def environment_signals(
        self, case: CaseResult, run: TestRun
    ) -> list[EnvironmentSignal]:
        return []

    async def change_records(
        self, case: CaseResult, run: TestRun
    ) -> list[ChangeRecord]:
        return []


class AttributionService:
    """组包 + 调引擎；无状态，可并发处理多个 run。"""

    def __init__(self, provider: AttributionEvidenceProvider | None = None) -> None:
        self._provider = provider or NullEvidenceProvider()

    async def attribute_case(self, run: TestRun, case: CaseResult) -> AttributionVerdict:
        if case.status not in (CaseStatus.FAILED, CaseStatus.ERROR):
            raise ValueError(
                f"用例 {case.external_case_id} 状态 {case.status} 非失败，不进归因流程"
            )

        reruns = await self._provider.reruns(case, run)
        assertion = await self._provider.assertion_check(case, run)
        log_signals = await self._provider.log_signals(case, run)
        diff = await self._provider.diff_signal(case, run)
        environment = await self._provider.environment_signals(case, run)
        change_records = await self._provider.change_records(case, run)

        ledger: list[EvidenceItem] = []
        if reruns:
            ledger.append(
                EvidenceItem(
                    ref="ev-rerun", kind=EvidenceKind.RERUN,
                    summary=f"同用例历史观察 {len(reruns)} 条",
                    data={"run_ids": [r.run_id for r in reruns]},
                )
            )
        if assertion is not None:
            ledger.append(
                EvidenceItem(
                    ref="ev-assert", kind=EvidenceKind.ASSERTION,
                    summary=(
                        f"断言期望 {assertion.expected!r} vs 代码常量 "
                        f"{assertion.actual_constant!r}，matches={assertion.matches}"
                    ),
                    data={"symbol_id": assertion.symbol_id},
                )
            )
        for i, sig in enumerate(log_signals, 1):
            ledger.append(
                EvidenceItem(
                    ref=f"ev-log-{i}", kind=EvidenceKind.LOG,
                    summary=(
                        f"os_log 查询 {sig.query_ref} 命中 {sig.matched_count} 条，"
                        f"信号族 {sig.families}"
                    ),
                    data={"predicate": sig.predicate, "families": sig.families},
                )
            )
        if diff is not None:
            ledger.append(
                EvidenceItem(
                    ref="ev-diff", kind=EvidenceKind.DIFF,
                    summary=(
                        f"diff {diff.base[:8]}..{diff.head[:8]}，"
                        f"堆栈符号命中={diff.stack_symbol_changed}"
                    ),
                    data={"changed_symbols": diff.changed_symbols},
                )
            )
        for i, sig in enumerate(environment, 1):
            ledger.append(
                EvidenceItem(
                    ref=f"ev-env-{i}", kind=EvidenceKind.ENVIRONMENT,
                    summary=f"{sig.family}: {sig.detail}",
                    data={"family": sig.family},
                )
            )
        for i, record in enumerate(change_records, 1):
            ledger.append(
                EvidenceItem(
                    ref=f"ev-change-{i}", kind=EvidenceKind.CHANGE_RECORD,
                    summary=f"{record.ref}: {record.summary}",
                    data={"symbols": record.symbols},
                )
            )

        packet = FailureEvidence(
            case_id=case.external_case_id,
            commit_sha=run.commit_sha,
            message=case.message,
            stack_top=case.stack_top,
            reruns=reruns,
            assertion=assertion,
            log_signals=log_signals,
            diff=diff,
            environment=environment,
            change_records=change_records,
            uncovered_layers=list(run.uncovered_layers),
            ledger=ledger,
        )
        return attribute_failure(packet)

    async def attribute_run(self, run: TestRun) -> list[AttributionVerdict]:
        """对 run 内全部 failed/error 用例归因，顺序与 case_results 一致。"""
        failed = [
            case
            for case in run.case_results
            if case.status in (CaseStatus.FAILED, CaseStatus.ERROR)
        ]
        return [await self.attribute_case(run, case) for case in failed]
