"""人工闸门服务（docs/15 §7，H2）。

薄服务层：状态语义全部在 core.hybrid_plan 纯函数里，本层只做
FileGateStore 取接、读时过期清扫与落盘。零 DDL、无后台线程——
过期不是定时任务推的，而是任何读请求（Web 轮询/GET 详情）到达时
按 deadline 现算并持久化，headless/单进程环境行为确定、可钉测。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from testpilot_core.errors import NotFoundError
from testpilot_core.hybrid_plan import (
    GateRequest,
    HumanVerdict,
    PlanStep,
    build_gate_request,
    cancel_gate,
    expire_gate_if_due,
    record_gate_response,
    renew_gate as renew_gate_pure,
)


def _gate_store(container: Any):
    store = container.get("gate_store")
    if store is None:
        # 装配期无条件构造；到这里说明容器被手工拼坏，fail-loud 不静默
        raise NotFoundError("gate_store 未装配（检查 factory.build_container）")
    return store


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(timezone.utc)


def open_gate(
    container: Any,
    *,
    session_id: str,
    step: PlanStep,
    evidence_urns: list[str],
    deadline_seconds: int,
    now: datetime | None = None,
) -> GateRequest:
    """挂出闸门；同会话同步骤的 gate_id 确定性，重复建单返回既有记录。

    崩溃重建语义：编排器重试时拿到的是同一条 pending 单，不会给人弹两张卡。
    """
    store = _gate_store(container)
    moment = _now(now)
    gate = build_gate_request(
        session_id=session_id, step=step, evidence_urns=list(evidence_urns),
        now=moment, deadline_seconds=deadline_seconds)
    existing = store.get(gate.gate_id)
    if existing is not None:
        return existing
    store.put(gate)
    return gate


def _sweep(store, gate: GateRequest, moment: datetime) -> GateRequest:
    expired = expire_gate_if_due(gate, moment)
    if expired is not gate and expired.status != gate.status:
        store.put(expired)
    return expired


def list_gates(
    container: Any, *, status: str | None = None, now: datetime | None = None
) -> list[GateRequest]:
    """闸门队列；读时清扫过期单，可按 status 过滤（Web 轮询 pending）。"""
    store = _gate_store(container)
    moment = _now(now)
    swept = [_sweep(store, g, moment) for g in store.list_all()]
    if status is None:
        return swept
    return [g for g in swept if g.status.value == status]


def get_gate(container: Any, gate_id: str, *,
             now: datetime | None = None) -> GateRequest:
    store = _gate_store(container)
    gate = store.get(gate_id)
    if gate is None:
        raise NotFoundError(f"人工闸门不存在: {gate_id}")
    return _sweep(store, gate, _now(now))


def respond_gate(
    container: Any, gate_id: str, verdict: HumanVerdict, *,
    now: datetime | None = None,
) -> GateRequest:
    """记录人工裁决；core 负责表单/证据/状态闭集校验，非法一律 422。"""
    store = _gate_store(container)
    gate = get_gate(container, gate_id, now=now)
    updated = record_gate_response(gate, verdict, _now(now))
    store.put(updated)
    return updated


def renew_gate(
    container: Any, gate_id: str, extra_seconds: int, *,
    now: datetime | None = None,
) -> GateRequest:
    """续期/重开：pending 延 deadline，expired 重开回 pending。"""
    store = _gate_store(container)
    gate = get_gate(container, gate_id, now=now)
    updated = renew_gate_pure(gate, _now(now), extra_seconds)
    store.put(updated)
    return updated


def abandon_gate(
    container: Any, gate_id: str, *, now: datetime | None = None
) -> GateRequest:
    """放弃闸门 → canceled；已响应的裁决留痕不可抹，core 会拒绝。"""
    store = _gate_store(container)
    gate = get_gate(container, gate_id, now=now)
    updated = cancel_gate(gate, _now(now))
    store.put(updated)
    return updated
