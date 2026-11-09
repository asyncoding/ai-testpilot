"""outbox 派发 worker（docs/08 FR-WR-002/003「先账后事」，S2.5）。

扫描 pending/failed outbox，按 ``target_system`` 路由到适配器执行真正的外部写。
S2.5 只接 ``markdown-case-repo``（人审回写）一条路由；其余目标系统计
skipped_unsupported 并保留 pending，等后续里程碑接线，绝不静默丢弃。

三种结局：
- confirmed：适配器返回 external_ref，outbox 翻 confirmed，重放由适配器
  manifest + 库 UNIQUE 键双层幂等兜底；
- blocked：kill switch 打开或审批失效（PolicyDenied）——**不烧 attempts、
  不改状态**，开关放开后下轮自然拾取；
- failed：适配器抛其它异常，翻 failed 留 error，沿用原幂等键可重试。
"""

from __future__ import annotations

from typing import Any

from testpilot_core.approval import CASE_REPO_TARGET_SYSTEM
from testpilot_core.enums import OutboxStatus
from testpilot_core.errors import PolicyDeniedError

_ERROR_PREVIEW = 200


async def dispatch_pending_outbox(container: Any) -> dict[str, Any]:
    store = container.get("store")
    repo = container.get("case_repo")
    records = store.pending_outbox_records()

    report: dict[str, Any] = {
        "status": "succeeded",
        "scanned": len(records),
        "dispatched": 0,
        "confirmed": 0,
        "blocked": 0,
        "failed": 0,
        "skipped_unsupported": 0,
        "results": [],
    }

    for record in records:
        key = record.idempotency_key
        base = {"idempotency_key": key, "target_system": record.target_system}

        if record.target_system != CASE_REPO_TARGET_SYSTEM:
            report["skipped_unsupported"] += 1
            report["results"].append(base | {"outcome": "skipped_unsupported"})
            continue
        if repo is None:
            # profile 未装配回写适配器（stub）：保留 pending，不算失败
            report["skipped_unsupported"] += 1
            report["results"].append(base | {"outcome": "adapter_not_configured"})
            continue

        try:
            external_ref = await repo.write_approved_record(record)
        except PolicyDeniedError as exc:
            report["blocked"] += 1
            report["results"].append(
                base | {"outcome": "blocked", "error": str(exc)[:_ERROR_PREVIEW]})
            continue
        except Exception as exc:  # 适配器/磁盘故障：留 failed 可重试，键不变
            store.mark_outbox(key, OutboxStatus.FAILED, error=str(exc)[:500])
            report["failed"] += 1
            report["results"].append(
                base | {"outcome": "failed", "error": str(exc)[:_ERROR_PREVIEW]})
            continue

        store.mark_outbox(key, OutboxStatus.CONFIRMED, external_ref=external_ref)
        report["confirmed"] += 1
        report["dispatched"] += 1
        report["results"].append(
            base | {"outcome": "confirmed", "external_ref": external_ref})

    # 有 failed 即 partial：CLI 以非零退出提醒 cron，blocked/skipped 不算失败
    if report["failed"]:
        report["status"] = "partial"
    return report
