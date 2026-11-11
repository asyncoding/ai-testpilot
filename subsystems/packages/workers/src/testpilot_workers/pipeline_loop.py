"""PipelineFinished CI 触发闭环（docs/08 FR-EX-008 / docs/04 §9，S4.4）。

事件 → 拉取 CI 结果报告（``file://`` payload_uri，全程只读）→ CI run 幂等入账
→ 失败用例选择重跑（execution_service 的配额/幂等/队列全复用，不另起执行通道）
→ 队列排空到终态（归因在 ``_process_job`` 内自动落 ``TestRun.attributions``）
→ R3 待审工单草稿（Approval+Outbox，target=json-tickets，pending 不外部写，
即 FR-WR-003 的 dry-run 默认姿态）。

纪律：

- 只信报告证据：payload 与 TestRun 报告矛盾（commit/结果状态）响亮失败，由
  event_dispatch 转 handler_error 死信，绝不带着矛盾数据写账本；
- 重放幂等：全部账本键内容寻址自 event_id，dispatch 层 event_id PK 与服务层
  幂等键双保险，重复投递零增生；
- handler 不直接调 xcodebuild：只经 submit_execution 入队，执行仍由
  runner-worker 既有通道消费（orchestrator/ingest 进程不碰平台工具链）；
- 缺 runner/fixtures_dir/报告文件一律抛错，不静默跳过（fail-closed）。

零 schema 变更：CI run 复用 test_runs、草稿复用 approvals/outbox（均 v15 既有表）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from testpilot_core.approval import TICKET_TARGET_SYSTEM
from testpilot_core.enums import (
    ActionLevel,
    ApprovalDecision,
    CaseStatus,
    OutboxStatus,
    RunStatus,
)
from testpilot_core.errors import AdapterUnavailableError
from testpilot_core.models import (
    Approval,
    CaseSelector,
    EnvironmentRequirements,
    EventEnvelope,
    OutboxRecord,
    TestExecutionRequest,
    TestRun,
)
from testpilot_core.urn import make_urn

from . import execution_service

_FILE_SCHEME = "file://"
# payload.result 闭集：失败才触发闭环；成功类结果 no-op；其余响亮失败进死信
_RESULT_FAILED = "failed"
_RESULT_SUCCEEDED = {"succeeded", "passed", "success"}
_TERMINAL_STATUSES = {
    RunStatus.PASSED, RunStatus.FAILED, RunStatus.ERROR,
    RunStatus.CANCELED, RunStatus.TIMEOUT,
}
# 排空保护：recorded 适配器一轮即终态；真实环境由独立 worker 消费，此处只兜底
_MAX_DRAIN_ITERATIONS = 10
_CI_TRIGGERED_BY = "ci"


@dataclass
class PipelineLoopReport:
    event_id: str
    outcome: str  # no_failure / closed / replayed
    ci_run_id: str = ""
    rerun_run_id: str = ""
    failure_case_ids: list[str] = field(default_factory=list)
    rerun_status: str = ""
    draft_idempotency_key: str = ""
    attribution_total: int = 0
    attribution_by_category: dict[str, int] = field(default_factory=dict)
    needs_human: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "outcome": self.outcome,
            "ci_run_id": self.ci_run_id,
            "rerun_run_id": self.rerun_run_id,
            "failure_case_ids": self.failure_case_ids,
            "rerun_status": self.rerun_status,
            "draft_idempotency_key": self.draft_idempotency_key,
            "attribution_total": self.attribution_total,
            "attribution_by_category": self.attribution_by_category,
            "needs_human": self.needs_human,
        }


def _resolve_report_path(container, payload_uri: str) -> Path:
    """``file://`` URI → 本机路径；相对路径锚定容器 fixtures_dir。

    演示仓 URI 形态 ``file://fixtures/runs/<name>.json``：首段 fixtures/ 剥掉
    后拼配置的 fixtures_dir（tests/fixtures）。http(s) 等远端制品留给后续里程碑，
    当前响亮失败而非偷偷降级。
    """
    if not payload_uri or not payload_uri.startswith(_FILE_SCHEME):
        raise ValueError(
            f"PipelineFinished.payload_uri 仅支持 {_FILE_SCHEME}<path>：{payload_uri!r}"
        )
    raw = payload_uri[len(_FILE_SCHEME):]
    path = Path(raw)
    if path.is_absolute():
        return path
    fixtures_dir = container.get("fixtures_dir")
    if fixtures_dir is None:
        raise AdapterUnavailableError(
            "容器未装配 fixtures_dir，无法解析相对 payload_uri"
        )
    parts = path.parts
    if parts and parts[0] == "fixtures":
        return Path(fixtures_dir).joinpath(*parts[1:])
    return Path(fixtures_dir) / path


def _load_ci_run(container, event: EventEnvelope) -> TestRun:
    """读取并校验 CI 结果报告；任何证据矛盾都抛错（由派发层转死信）。"""
    path = _resolve_report_path(container, event.payload_uri)  # type: ignore[arg-type]
    if not path.exists():
        raise AdapterUnavailableError(f"CI 结果报告不存在: {path}")
    run = TestRun.model_validate(json.loads(path.read_text(encoding="utf-8")))

    payload_commit = event.payload.get("commit_sha", "")
    if payload_commit and payload_commit != run.commit_sha:
        raise ValueError(
            f"事件 commit_sha={payload_commit} 与报告 commit_sha={run.commit_sha} 矛盾"
        )
    if run.status not in (RunStatus.FAILED, RunStatus.ERROR):
        raise ValueError(
            f"事件声称失败但报告状态为 {run.status.value}，证据矛盾不写账本"
        )
    return run


def _ci_idempotency_key(event: EventEnvelope) -> str:
    return f"ci-run:{event.event_id}"


def _rerun_idempotency_key(event: EventEnvelope) -> str:
    return f"pipeline-rerun:{event.event_id}"


def _draft_idempotency_key(event: EventEnvelope) -> str:
    return f"ci-ticket-draft:{event.event_id}"


def _fixture_hint(payload_uri: str) -> str:
    """录制适配器选报告用：取 URI 文件名去后缀（xcode 真实适配器忽略此值）。"""
    return Path(payload_uri[len(_FILE_SCHEME):]).stem


def _import_ci_run(store, event: EventEnvelope, run: TestRun) -> bool:
    """CI 外部 run 入账本（零 schema，复用 test_runs）。返回 True=新导入。"""
    key = _ci_idempotency_key(event)
    if store.get_test_run_by_idempotency(key) is not None:
        return False
    request = TestExecutionRequest(
        request_id=f"ci:{event.event_id}",
        idempotency_key=key,
        project=run.project,
        selector=CaseSelector(
            test_ids=[c.external_case_id for c in run.case_results],
            fixture_hint=_fixture_hint(event.payload_uri),  # type: ignore[arg-type]
        ),
        commit_sha=run.commit_sha,
        build_id=run.build_id,
        environment=EnvironmentRequirements(
            capabilities=list(run.environment.capabilities),
            labels={
                "provider": run.environment.provider,
                "env_id": run.environment.env_id,
            },
        ),
        triggered_by=_CI_TRIGGERED_BY,
    )
    # 外部 CI 已跑完：先挂 queued 幻影行再立即回写终态与完整 TestRun，
    # 与本仓执行账本同形，执行详情页可直接展示（FR-EX-008 全程可追踪）。
    store.insert_test_run_ledger(
        request=request, run_id=run.run_id, status=RunStatus.QUEUED.value
    )
    store.update_test_run(
        run.run_id,
        status=run.status.value,
        raw_json=run.model_dump_json(),
        started_at=run.started_at.isoformat() if run.started_at else None,
        finished_at=run.finished_at.isoformat() if run.finished_at else None,
    )
    return True


async def _drain_until_terminal(container, run_id: str) -> str:
    """驱动队列直到目标 run 进终态；recorded 环境一轮完成，真实部署由 worker 接管。"""
    store = container.get("store")
    for _ in range(_MAX_DRAIN_ITERATIONS):
        row = store.get_test_run_row(run_id)
        if row is None:
            raise AdapterUnavailableError(f"重跑 run 账本缺失: {run_id}")
        status = RunStatus(row["status"])
        if status in _TERMINAL_STATUSES:
            return status.value
        job_result = await execution_service.run_once(container)
        if job_result is None:
            raise AdapterUnavailableError(
                f"队列已空但 run {run_id} 仍未终态（status={row['status']}）"
            )
    raise AdapterUnavailableError(
        f"run {run_id} 排空超过 {_MAX_DRAIN_ITERATIONS} 轮仍未终态"
    )


def _evidence_uris(run: TestRun) -> list[str]:
    """汇总失败用例附件/制品/原始报告引用，去重保序（FR-WR-003 AC2 证据链接）。"""
    uris: list[str] = []
    for case in run.case_results:
        if case.status in (CaseStatus.FAILED, CaseStatus.ERROR):
            uris.extend(case.artifact_urns)
    uris.extend(a.uri for a in run.artifacts)
    if run.raw_report_ref:
        uris.append(run.raw_report_ref)
    return list(dict.fromkeys(u for u in uris if u))


def _write_ticket_draft(
    store, event: EventEnvelope, ci_run: TestRun, rerun: TestRun
) -> str:
    """落 R3 待审 Approval + pending Outbox 草稿；不触发任何外部写。"""
    key = _draft_idempotency_key(event)
    if store.get_outbox_record(key) is not None:
        return key  # 重放：ON CONFLICT 之外的显式短路，账本零改动
    approval_id = make_urn("ci-loop", "approval", event.event_id)
    by_category: dict[str, int] = {}
    for verdict in rerun.attributions:
        by_category[verdict.category.value] = by_category.get(
            verdict.category.value, 0
        ) + 1
    approval = Approval(
        approval_id=approval_id,
        action_level=ActionLevel.R3_APPROVED_EXTERNAL,
        target_system=TICKET_TARGET_SYSTEM,
        request_summary=(
            f"CI 流水线 {event.subject_ref} 失败：{ci_run.run_id} 选择重跑 "
            f"{rerun.run_id}，待审批建工单"
        ),
        request_diff={
            "event_id": event.event_id,
            "source_run_id": ci_run.run_id,
            "rerun_run_id": rerun.run_id,
            "failed_case_ids": [
                c.external_case_id
                for c in rerun.case_results
                if c.status in (CaseStatus.FAILED, CaseStatus.ERROR)
            ],
        },
        decision=ApprovalDecision.PENDING,
        based_on_versions={"event_version": event.version},
    )
    store.save_approval(approval)
    record = OutboxRecord(
        idempotency_key=key,
        action_level=ActionLevel.R3_APPROVED_EXTERNAL,
        target_system=TICKET_TARGET_SYSTEM,
        request_body={
            "action": "create_ticket",
            "project": event.project,
            "event_id": event.event_id,
            "source_run_id": ci_run.run_id,
            "rerun_run_id": rerun.run_id,
            "commit_sha": rerun.commit_sha,
            "failed_case_ids": approval.request_diff["failed_case_ids"],
            "attribution_summary": {
                "total": len(rerun.attributions),
                "by_category": by_category,
                "needs_human": sum(
                    1 for v in rerun.attributions if v.needs_human_reason
                ),
            },
            "evidence_uris": _evidence_uris(rerun),
            "uncovered_layers": list(rerun.uncovered_layers),
        },
        status=OutboxStatus.PENDING,
        approval_id=approval_id,
        rollback_note=(
            "回滚步骤：1) 在审批页驳回该 approval（pending 草稿无外部副作用）；"
            f"2) 删除 outbox 幂等键 {key} 与 approval {approval_id}；"
            "3) test_runs 账本保留不改（执行事实只追加不抹除）。"
        ),
    )
    store.add_outbox(record)
    return key


async def handle_pipeline_finished(
    container, event: EventEnvelope
) -> PipelineLoopReport:
    """PipelineFinished handler（经 event_dispatch.register_handler 挂接）。

    payload 契约：``{"build_id", "commit_sha", "result"}`` + 信封 payload_uri
    指向 TestRun JSON 报告。成功结果 no-op 零写入；失败走完整闭环。
    """
    result = event.payload.get("result", "")
    if result in _RESULT_SUCCEEDED:
        return PipelineLoopReport(event_id=event.event_id, outcome="no_failure")
    if result != _RESULT_FAILED:
        raise ValueError(
            f"PipelineFinished payload.result 非法: {result!r}"
            f"（闭集：failed / {' / '.join(sorted(_RESULT_SUCCEEDED))}）"
        )

    store = container.get("store")
    tracer = container.get("tracer")
    if container.get("runner") is None:
        raise AdapterUnavailableError("容器未装配 runner，无法发起选择重跑")

    with tracer.span(
        "pipeline.finished",
        run_id=event.event_id,
        metadata={"build_id": event.payload.get("build_id", "")},
    ):
        ci_run = _load_ci_run(container, event)
        report = PipelineLoopReport(
            event_id=event.event_id,
            outcome="closed",
            ci_run_id=ci_run.run_id,
            failure_case_ids=[
                c.external_case_id
                for c in ci_run.case_results
                if c.status in (CaseStatus.FAILED, CaseStatus.ERROR)
            ],
        )
        if not report.failure_case_ids:
            raise ValueError("失败事件的报告中没有 failed/error 用例，证据矛盾")

        _import_ci_run(store, event, ci_run)

        rerun_key = _rerun_idempotency_key(event)
        existing = store.get_test_run_by_idempotency(rerun_key)
        request = TestExecutionRequest(
            request_id=f"rerun:{event.event_id}",
            idempotency_key=rerun_key,
            project=event.project,
            selector=CaseSelector(
                test_ids=report.failure_case_ids,
                fixture_hint=_fixture_hint(event.payload_uri),  # type: ignore[arg-type]
            ),
            commit_sha=ci_run.commit_sha,
            build_id=ci_run.build_id,
            environment=EnvironmentRequirements(
                capabilities=list(ci_run.environment.capabilities)
            ),
            triggered_by=_CI_TRIGGERED_BY,
            # 带预算 key 即占每日重跑配额（core.execution.is_rerun 口径）
            daily_budget_key=(
                f"ci-rerun:{event.project}:{ci_run.commit_sha[:12]}"
            ),
        )
        submit = await execution_service.submit_execution(container, request)
        if submit.get("replayed"):
            report.outcome = "replayed"
        rerun_run_id = submit["run_id"]
        report.rerun_run_id = rerun_run_id
        # 内容寻址兜底：submit 与既有行必然同 run_id，不一致是账本接线 bug
        if existing is not None and existing["run_id"] != rerun_run_id:
            raise AdapterUnavailableError(
                f"重跑幂等键 {rerun_key} 对应 run_id 漂移："
                f"{existing['run_id']} != {rerun_run_id}"
            )

        report.rerun_status = await _drain_until_terminal(container, rerun_run_id)
        rerun_row = store.get_test_run_row(rerun_run_id)
        rerun = store.row_to_test_run(rerun_row)
        if rerun is None:
            raise AdapterUnavailableError(
                f"重跑 run {rerun_run_id} 已终态但 raw_json 缺失"
            )

        report.draft_idempotency_key = _write_ticket_draft(
            store, event, ci_run, rerun
        )
        report.attribution_total = len(rerun.attributions)
        report.attribution_by_category = {
            category: sum(
                1 for v in rerun.attributions if v.category.value == category
            )
            for category in {v.category.value for v in rerun.attributions}
        }
        report.needs_human = sum(
            1 for v in rerun.attributions if v.needs_human_reason
        )
    return report
