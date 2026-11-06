"""问答图 state：全 JSON 原语 TypedDict（无 reducer，last-write-wins）。

每个字段只有唯一写入节点（见各节点 docstring 标注），规避 LangGraph 同
superstep 多写入方的 InvalidUpdateError。pydantic 模型/datetime 一律不进
state——1.2.20 虽能宽容往返但带 unregistered type 警告且未来版本将 block，
证据块统一经 answering.to_evidence_dict 白名单转换。
"""

from __future__ import annotations

from typing import Any, TypedDict


class QaState(TypedDict, total=False):
    # ── service 入参（START 前写入）──
    run_id: str
    case_key: str  # 离线录制夹具选择键；生产 profile 为空，节点退回通用 key
    question: str
    project: str
    branch: str
    acl_scope: str

    # ── intent 节点写入 ──
    intent: str  # QaIntent 的 value
    intent_degraded: bool
    model_version: str

    # ── acl 节点写入 ──
    denied_reason: str

    # ── retrieval 节点写入 ──
    evidence: list[dict[str, Any]]
    retrieval_event_id: int | None
    retrieval_degraded: bool
    retrieval_trace: dict[str, Any]
    history_testimony: list[dict[str, Any]]

    # ── verify 节点写入（仅 intent=code 走该节点）──
    verify_results: list[dict[str, Any]]
    data_version: str  # HEAD sha

    # ── head_refresh 节点写入 ──
    head_refreshed_urns: list[str]

    # ── generation / citation_check 节点写入 ──
    draft: dict[str, Any] | None
    attempts: int
    citation_errors: list[str]
    tokens_total: dict[str, int]

    # ── 多节点累积（每个终止/降级节点只整体替换一次，无并发写）──
    status: str  # QaStatus 的 value；空串=尚未终止
    unknown_reason: str
    degraded_flags: list[str]

    # ── assembly 节点写入 ──
    final_answer: dict[str, Any] | None

    # ── service 写入图配置而非 state；prompt/params 版本由 generation 写 ──
    prompt_version: str
    params_version: str
