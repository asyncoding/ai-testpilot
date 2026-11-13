"""/api/gates/* 路由：人工闸门挂单/队列/裁决/续期/放弃（docs/15 §7，H2）。

薄 HTTP 包装，纪律同 approval_api：状态语义在 core.hybrid_plan 纯函数，
账本动作在 workers.gate_service，本层只做 HTTP 模型转换与容器取接。
Web 端轮询 GET /api/gates?status=pending 渲染弹窗卡片，不做 SSE/系统通知。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Query, Request

from testpilot_core.hybrid_plan import HumanVerdict
from testpilot_workers import gate_service

from .schemas import GateOpenHttp, GateRenewHttp, GateRespondHttp

gate_router = APIRouter(prefix="/api/gates", tags=["gates"])


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp() -> str:
    return _now().strftime("%Y-%m-%dT%H:%M:%SZ")


@gate_router.post("")
async def open_gate(body: GateOpenHttp, request: Request) -> dict[str, Any]:
    """挂出人工闸门；同会话同步骤重复挂单幂等返回既有记录（崩溃重建语义）。"""
    container = request.app.state.container
    gate = gate_service.open_gate(
        container,
        session_id=body.session_id,
        step=body.to_plan_step(),
        evidence_urns=body.evidence_urns,
        deadline_seconds=body.deadline_seconds,
        now=_now(),
    )
    return gate.model_dump(mode="json")


@gate_router.get("")
async def list_gates(
    request: Request, status: str | None = Query(default=None)
) -> dict[str, Any]:
    """闸门队列；读时按 deadline 现算过期并持久化，支持 status 过滤。"""
    container = request.app.state.container
    gates = gate_service.list_gates(container, status=status, now=_now())
    return {"gates": [g.model_dump(mode="json") for g in gates]}


@gate_router.get("/{gate_id}")
async def get_gate(gate_id: str, request: Request) -> dict[str, Any]:
    gate = gate_service.get_gate(
        request.app.state.container, gate_id, now=_now())
    return gate.model_dump(mode="json")


@gate_router.post("/{gate_id}/respond")
async def respond_gate(
    gate_id: str, body: GateRespondHttp, request: Request
) -> dict[str, Any]:
    """提交人工裁决；空证据/野字段/过期单响应一律 422（core fail-closed）。"""
    verdict = HumanVerdict(
        gate_id=gate_id,
        values=body.values,
        evidence_urns=body.evidence_urns,
        note=body.note,
        decided_by=body.decided_by,
        decided_at=_stamp(),
    )
    gate = gate_service.respond_gate(
        request.app.state.container, gate_id, verdict, now=_now())
    return gate.model_dump(mode="json")


@gate_router.post("/{gate_id}/renew")
async def renew_gate(
    gate_id: str, body: GateRenewHttp, request: Request
) -> dict[str, Any]:
    """续期：pending 延 deadline；expired 重开回 pending（会话 blocked 解除）。"""
    gate = gate_service.renew_gate(
        request.app.state.container, gate_id,
        body.extra_seconds, now=_now())
    return gate.model_dump(mode="json")


@gate_router.post("/{gate_id}/abandon")
async def abandon_gate(gate_id: str, request: Request) -> dict[str, Any]:
    """放弃闸门 → canceled；已响应闸门拒绝取消（裁决留痕不可抹）。"""
    gate = gate_service.abandon_gate(
        request.app.state.container, gate_id, now=_now())
    return gate.model_dump(mode="json")
