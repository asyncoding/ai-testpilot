#!/usr/bin/env python3
"""S3.1 测试执行与 runner-worker 端到端探针（docs/08 FR-EX-001/FR-EX-002，A19）。

与单测的分工：test_execution_* 用 tmp 库钉死每条分支；本探针走**生产装配路径**
（load_profile → build_container → workers.execution_service → SQLite
jobs/test_runs/rerun_budget），在临时工作目录跑完整故事，不碰 store/offline-demo、
不起端口、不访问网络、无模型调用（纯确定性）。

故事线：
  ① 空队列冷启动：run_once 返回 None、账本盘点为空；
  ② 提交先账本后队列，run_id 内容寻址，同幂等键重放不增生 job；
  ③ worker 单轮全链落账（recorded 生产适配器：终态/raw_json/身份归一/job done）；
  ④ 重跑配额活体：配额 2，第三笔重跑被拒且不留幻影（无 run 账本/无 job/预算不增）；
  ⑤ 协作式取消检查点：认领前打标，worker 不碰适配器直接 canceled 收尾；
  ⑥ 超时收口：慢适配器假件触发 wait_for 超时，run=timeout、job=failed、cancel 回调；
  ⑦ 崩溃回收-重领：过期租约 requeue，run 账本回 queued 后正常跑完；
  ⑧ 崩溃回收-死信：attempts 顶格的过期任务 dead_letter，run 账本 error；
  ⑨ JUnit 生产装配（junit-xml 走工厂）：Surefire 合成夹具 5 用例三态齐全，
     worker 长循环 stats 正确；
  ⑩ 持久化与版本：重开库账本无损，schema v15 钉版。

报告落 out/eval/execution-probe.{json,md}（out/ 已 gitignore）。任一断言不符 exit 1。

用法（在 subsystems/ 下）：
  .venv/bin/python scripts/execution_probe.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages" / "workers" / "src"))

from testpilot_adapters.store import SqliteQueue, bootstrap_store  # noqa: E402
from testpilot_core import SCHEMA_VERSION  # noqa: E402
from testpilot_core.config_schema import AdapterRef  # noqa: E402
from testpilot_core.enums import RunStatus  # noqa: E402
from testpilot_core.execution import run_id_for  # noqa: E402
from testpilot_core.models import CaseSelector, TestExecutionRequest  # noqa: E402
from testpilot_orchestrator.config_loader import load_profile  # noqa: E402
from testpilot_orchestrator.factory import build_container  # noqa: E402
from testpilot_workers import execution_service as svc  # noqa: E402
from testpilot_workers.execution_service import RerunQuotaExceededError  # noqa: E402

SUBSYSTEMS = Path(__file__).resolve().parents[1]
PROFILE = SUBSYSTEMS / "config" / "profiles" / "offline-demo.yaml"
EVAL_DIR = SUBSYSTEMS / "out" / "eval"


class _SlowRunner:
    """submit 立即拿句柄、get_run 卡住：模拟 xcodebuild 子进程挂死。"""

    def __init__(self) -> None:
        self.canceled: list[str] = []

    async def submit(self, request):
        return "slow-1"

    async def get_run(self, run_id):
        await asyncio.sleep(0.3)
        raise AssertionError("超时路径不应返回结果")

    async def cancel(self, run_id):
        self.canceled.append(run_id)


def _req(request_id: str, *, rerun_key: str | None = None,
         hint: str | None = None, project: str = "probe-demo") -> TestExecutionRequest:
    return TestExecutionRequest(
        request_id=request_id,
        idempotency_key=f"idem-{request_id}",
        project=project,
        selector=CaseSelector(test_ids=["T/t1"], fixture_hint=hint),
        commit_sha="deadbeef",
        daily_budget_key=rerun_key,
    )


def _expired_lease_iso() -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()


async def _main_async() -> tuple[list[dict[str, str]], list[str], Path]:
    work = Path(tempfile.mkdtemp(prefix="execution-probe-", dir=EVAL_DIR))
    checks: list[dict[str, str]] = []
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "passed": "✅" if ok else "❌", "detail": detail})
        if not ok:
            failures.append(f"{name}: {detail}")

    drained = 0
    config = load_profile(PROFILE)
    config.paths["data_dir"] = str(work / "data")
    config.limits.daily_rerun_quota = 2
    container = build_container(config, SUBSYSTEMS)
    store, queue = container.get("store"), container.get("queue")

    try:
        # ① 空队列冷启动
        empty_once = await svc.run_once(container)
        check("① 空队列 run_once 返回 None、账本盘点为空",
              empty_once is None and svc.list_runs(container) == [],
              f"run_once={empty_once} runs={len(svc.list_runs(container))}")

        # ② 提交：先账本后队列 + 幂等重放
        submitted = await svc.submit_execution(
            container, _req("req-1", hint="xcode_hostless"))
        expected_id = run_id_for("idem-req-1")
        jobs_after_first = store.conn.execute(
            "SELECT COUNT(*) n FROM jobs").fetchone()["n"]
        replay = await svc.submit_execution(
            container, _req("req-1", hint="xcode_hostless"))
        jobs_after_replay = store.conn.execute(
            "SELECT COUNT(*) n FROM jobs").fetchone()["n"]
        check("② run_id 内容寻址；重放 replayed 且 job 不增生",
              submitted == {"run_id": expected_id, "status": "queued",
                            "replayed": False}
              and replay["replayed"] is True and replay["run_id"] == expected_id
              and jobs_after_first == 1 and jobs_after_replay == 1,
              f"jobs {jobs_after_first}→{jobs_after_replay} run_id={expected_id}")

        # ③ worker 单轮全链落账
        summary = await svc.run_once(container)
        row = store.get_test_run_row(expected_id)
        restored = store.row_to_test_run(row)
        job_done = store.conn.execute(
            "SELECT status FROM jobs WHERE idempotency_key=?",
            ("idem-req-1",)).fetchone()["status"]
        check("③ 单轮消费：终态 failed/raw_json 落账/身份归一/job done",
              summary["outcome"] == RunStatus.FAILED.value
              and row["status"] == "failed" and bool(row["raw_json"])
              and restored.run_id == expected_id
              and restored.environment.provider == "ios-simulator"
              and job_done == "done",
              f"outcome={summary['outcome']} job={job_done} cases={len(restored.case_results)}")

        # ④ 重跑配额活体（quota=2）
        today = datetime.now(timezone.utc).date().isoformat()
        budget_key = "probe-demo:rerun"
        await svc.submit_execution(container, _req("first-run"))  # 首跑不占配额
        await svc.submit_execution(
            container, _req("rerun-1", rerun_key=budget_key))
        await svc.submit_execution(
            container, _req("rerun-2", rerun_key=budget_key))
        denied = False
        try:
            await svc.submit_execution(
                container, _req("rerun-3", rerun_key=budget_key))
        except RerunQuotaExceededError:
            denied = True
        budget_n = store.count_budget_today(budget_key, today)
        phantom = store.get_test_run_row(run_id_for("idem-rerun-3"))
        jobs_n = store.conn.execute("SELECT COUNT(*) n FROM jobs").fetchone()["n"]
        check("④ 配额 2：第三笔重跑拒入且不留幻影（无账本/无 job/预算不增）",
              denied and budget_n == 2 and phantom is None and jobs_n == 4,
              f"denied={denied} budget={budget_n} phantom={phantom is not None} jobs={jobs_n}")

        # 队列是 FIFO：排空 ④ 遗留的 3 条排队任务，保证后续故事步各自只领到自己的 job
        drained = 0
        while (await svc.run_once(container)) is not None:
            drained += 1

        # ⑤ 取消检查点（认领前）
        cancel_submit = await svc.submit_execution(container, _req("req-cancel"))
        cancel_ack = await svc.request_cancel(container, cancel_submit["run_id"])
        cancel_summary = await svc.run_once(container)
        cancel_row = store.get_test_run_row(cancel_submit["run_id"])
        cancel_flag = store.conn.execute(
            "SELECT cancel_requested FROM jobs WHERE idempotency_key=?",
            ("idem-req-cancel",)).fetchone()["cancel_requested"]
        check("⑤ 认领前取消：worker 不碰适配器，run= canceled 且 job 打标收尾",
              cancel_ack["cancel_requested"] is True
              and cancel_summary["outcome"] == RunStatus.CANCELED.value
              and cancel_row["status"] == "canceled" and cancel_flag == 1,
              f"outcome={cancel_summary['outcome']} flag={cancel_flag}")

        # ⑥ 超时收口（换慢适配器假件 + 收紧超时）
        original_runner = container.adapters["runner"]
        slow = _SlowRunner()
        container.adapters["runner"] = slow
        config.limits.request_timeout_s = 0.05
        await svc.submit_execution(container, _req("req-timeout"))
        timeout_summary = await svc.run_once(container)
        timeout_row = store.get_test_run_row(timeout_summary["run_id"])
        timeout_job = store.conn.execute(
            "SELECT status, error FROM jobs WHERE idempotency_key=?",
            ("idem-req-timeout",)).fetchone()
        config.limits.request_timeout_s = 60
        container.adapters["runner"] = original_runner
        check("⑥ 超时：run=timeout/job=failed/适配器 cancel 回调到位",
              timeout_summary["outcome"] == RunStatus.TIMEOUT.value
              and timeout_row["status"] == "timeout"
              and timeout_job["status"] == "failed" and "timeout" in timeout_job["error"]
              and slow.canceled == ["slow-1"],
              f"outcome={timeout_summary['outcome']} canceled={slow.canceled}")

        # ⑦ 崩溃回收-重领
        requeue_submit = await svc.submit_execution(
            container, _req("req-requeue", hint="xcode_hostless"))
        rq_job = await queue.dequeue(["test_execution"])
        store.update_test_run(
            requeue_submit["run_id"], status=RunStatus.RUNNING.value)
        store.conn.execute(
            "UPDATE jobs SET lease_expires_at=? WHERE id=?",
            (_expired_lease_iso(), rq_job["job_id"]))
        store.conn.commit()
        reclaim = await svc.recover_stale(container)
        requeue_row = store.get_test_run_row(requeue_submit["run_id"])
        requeue_summary = await svc.run_once(container)
        check("⑦ 过期租约 requeue：账本回 queued 后重领正常收尾",
              reclaim[0]["action"] == "requeue"
              and requeue_row["status"] == "queued"
              and requeue_summary["outcome"] == RunStatus.FAILED.value,
              f"action={reclaim[0]['action']} final={requeue_summary['outcome']}")

        # ⑧ 崩溃回收-死信
        dl_submit = await svc.submit_execution(container, _req("req-deadletter"))
        dl_job = await queue.dequeue(["test_execution"])
        store.update_test_run(
            dl_submit["run_id"], status=RunStatus.RUNNING.value)
        store.conn.execute(
            "UPDATE jobs SET attempts=3, lease_expires_at=? WHERE id=?",
            (_expired_lease_iso(), dl_job["job_id"]))
        store.conn.commit()
        dead = await svc.recover_stale(container)
        dl_row = store.get_test_run_row(dl_submit["run_id"])
        check("⑧ attempts 顶格过期任务死信：run=error 留 max_attempts 原因",
              dead[0]["action"] == "dead_letter"
              and dl_row["status"] == "error" and "max_attempts" in dl_row["error"],
              f"action={dead[0]['action']} error={dl_row['error']}")

        # ⑩ 持久化与版本（先在主容器关库前钉版本）
        version_ok = store.get_meta("schema_version") == str(SCHEMA_VERSION) == "15"
        run_count_before = len(store.list_test_runs(project="probe-demo"))
        store.close()
        reopened = bootstrap_store(config.paths["data_dir"])
        try:
            re_runs = reopened.list_test_runs(project="probe-demo")
            statuses = sorted(r["status"] for r in re_runs)
        finally:
            reopened.close()
        # 8 条：req-1/首跑/两笔重跑/req-requeue 共 5 failed，canceled/timeout/error 各 1
        check("⑩ 重开库账本无损且 schema v15 钉版",
              version_ok and len(re_runs) == run_count_before == 8
              and statuses == sorted(["canceled", "error", "failed", "failed",
                                      "failed", "failed", "failed", "timeout"]),
              f"version={SCHEMA_VERSION} count={len(re_runs)} statuses={statuses} drained={drained}")
    finally:
        try:
            store.close()
        except Exception:  # noqa: BLE001 - ⑩ 已关库时忽略
            pass

    # ⑨ JUnit 生产装配（独立容器/独立 tmp 库，junit-xml 走工厂分支）
    jconfig = load_profile(PROFILE)
    jconfig.paths["data_dir"] = str(work / "junit-data")
    jconfig.adapters.runner = AdapterRef(
        type="junit-xml", settings={"reports_dir": "runs/surefire"})
    jcontainer = build_container(jconfig, SUBSYSTEMS)
    jstore = jcontainer.get("store")
    try:
        for i in (1, 2):
            await svc.submit_execution(container=jcontainer, request=_req(
                f"junit-{i}", hint="TEST-com.example.CalcTest",
                project="probe-junit"))
        stats = await svc.run_worker(
            jcontainer, max_iterations=8, poll_interval=0)
        rows = jstore.list_test_runs(project="probe-junit")
        sample = jstore.row_to_test_run(jstore.get_test_run_row(
            run_id_for("idem-junit-1")))
        case_statuses = sorted(c.status.value for c in sample.case_results)
        artifact_types = [a.type.value for a in sample.artifacts]
        check("⑨ junit-xml 工厂装配：Surefire 5 用例三态齐全，worker 循环统计正确",
              stats["processed"] == 2 and stats["by_outcome"] == {"failed": 2}
              and len(rows) == 2 and case_statuses == ["error", "failed", "passed",
                                                        "passed", "skipped"]
              and "junit_xml" in artifact_types,
              f"stats={stats['processed']}/{stats['by_outcome']} "
              f"cases={case_statuses} artifacts={artifact_types}")
    finally:
        jstore.close()

    return checks, failures, work


def main() -> int:
    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    checks, failures, work = asyncio.run(_main_async())

    report_doc = {
        "probe": "execution_probe",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "workdir": str(work),
        "schema_version": SCHEMA_VERSION,
        "summary": {
            "passed": sum(1 for c in checks if c["passed"] == "✅"),
            "failed": len(failures),
            "total": len(checks),
        },
        "checks": checks,
    }
    (EVAL_DIR / "execution-probe.json").write_text(
        json.dumps(report_doc, ensure_ascii=False, indent=2), encoding="utf-8")
    md_lines = [
        "# S3.1 测试执行与 runner-worker 探针报告（execution_probe / FR-EX-001/002）",
        "",
        f"生成时间：{report_doc['generated_at']}",
        f"工作目录：`{work}`",
        f"Schema 版本：v{SCHEMA_VERSION}",
        "",
        "| # | 检查 | 结果 | 证据 |",
        "|---|---|---|---|",
    ]
    md_lines += [
        f"| {i} | {c['name']} | {c['passed']} | {c['detail']} |"
        for i, c in enumerate(checks, 1)
    ]
    (EVAL_DIR / "execution-probe.md").write_text(
        "\n".join(md_lines) + "\n", encoding="utf-8")

    for c in checks:
        print(f"{c['passed']} {c['name']} — {c['detail']}")
    print(f"报告：{EVAL_DIR / 'execution-probe.json'}")
    if failures:
        print("❌ execution_probe 存在失败检查", file=sys.stderr)
        return 1
    print("✅ execution_probe 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
