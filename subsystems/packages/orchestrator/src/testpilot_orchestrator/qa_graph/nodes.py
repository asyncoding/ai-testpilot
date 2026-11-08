"""问答图节点：LangGraph 无关的 async f(state) -> dict 纯签名。

框架只在 graph.py 做薄装配；本文件不 import langgraph，备选手写状态机
（一张路由表循环）可直接消费同一批节点函数。适配器经 QaDeps 闭包注入，
节点单测只需构造 fake deps，不启 FastAPI、不连任何外部服务。

状态写入纪律：每个 state 字段只有一个节点写（见 state.py 表）；早终止节点
写 status/unknown_reason 后由条件边路由到 END，节点自身不感知图结构。
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlparse

from pydantic import ValidationError

from testpilot_core.answering import (
    MAX_ANSWER_ATTEMPTS,
    PARAMS_VERSION,
    QaParams,
    annotate_version_states,
    assess_sufficiency,
    build_urn_whitelist,
    check_acl,
    extract_history_testimony,
    fallback_classify_intent,
    should_regenerate,
    to_evidence_dict,
    validate_citations,
    assemble_final,
    assemble_incident,
)
from testpilot_core.chunking import (
    MARKDOWN_CHUNKER_VERSION,
    SWIFT_CHUNKER_VERSION,
    chunk_markdown,
    chunk_swift,
    select_chunk_by_anchor,
)
from testpilot_core.config_schema import Limits
from testpilot_core.enums import QaIntent, QaStatus
from testpilot_core.errors import AdapterUnavailableError, PathMissingAtCommitError
from testpilot_core.models import (
    AnswerDraft,
    AssembledAnswer,
    HistoryTestimony,
    RepoRef,
)
from testpilot_core.retrieval import HybridResult, parse_symbol_reference
from testpilot_adapters.tracers import NoopTracer

from .prompts import (
    INTENT_SCHEMA,
    PROMPT_VERSION,
    build_answer_messages,
    build_intent_messages,
)

# 节点名常量：graph.py 与条件边 path_map 共用，杜绝字符串漂移
N_INTENT = "intent"
N_ACL = "acl"
N_RETRIEVAL = "retrieval"
N_SUFFICIENCY = "sufficiency"
N_VERIFY = "verify"
N_HEAD_REFRESH = "head_refresh"
N_GENERATION = "generation"
N_CITATION_CHECK = "citation_check"
N_ASSEMBLY = "assembly"

_VALID_INTENTS = {i.value for i in QaIntent}

# v7 意图来源留痕三值（qa_runs.intent_source）：
# - regex_bypass：题面是精确 `Type.member(label:)` 形态，确定性判 code，整次 LLM 调用省掉；
#   与 retrieval 节点的符号路由共用同一解析器，免模型与置顶尝试的题面口径严格一致。
# - model：模型分类成功；fallback：模型不可用/非法枚举，走关键词保守兜底（同时落降级标记）。
INTENT_SOURCE_BYPASS = "regex_bypass"
INTENT_SOURCE_MODEL = "model"
INTENT_SOURCE_FALLBACK = "fallback"
_VERIFY_KEYS = (
    "urn", "kind", "exists", "verified", "body_changed", "degraded",
    "reason", "head_commit", "indexed_commit", "latency_ms", "branch",
)


@dataclass(kw_only=True)
class QaDeps:
    """节点依赖包；同步 factory 可构造（不含任何 async saver）。全部关键字传参。"""

    model: Any  # ModelGatewayPort
    search: Any  # KnowledgeSearchPort（hybrid 或 fts，形态在节点内归一化）
    scm: Any = None  # CodeRepositoryPort | None
    store: Any = None  # Store | None（assembly 反查 uri/hash；离线 FTS 路径需要）
    repo: RepoRef | None = None
    tracer: Any = None
    params: QaParams | None = None
    limits: Limits | None = None

    def __post_init__(self) -> None:
        if self.params is None:
            self.params = QaParams()
        if self.limits is None:
            self.limits = Limits()
        if self.tracer is None:
            self.tracer = NoopTracer()


# ── 通用 helper ─────────────────────────────────────────────────────────────

def _flags(state: dict, *new_flags: str) -> list[str]:
    """累积降级标注（去重保序）。"""
    seen = dict.fromkeys(state.get("degraded_flags") or [])
    for f in new_flags:
        seen[f] = None
    return list(seen)


def _add_tokens(state: dict, env: dict) -> dict[str, int]:
    cur = state.get("tokens_total") or {"in": 0, "out": 0}
    return {
        "in": cur["in"] + int(env.get("tokens_in") or 0),
        "out": cur["out"] + int(env.get("tokens_out") or 0),
    }


def _model_key(state: dict, kind: str) -> str:
    """录制夹具 key 约定：带 case_key 用 qa_<kind>_<case>，否则通用 qa_<kind>。"""
    case = state.get("case_key") or ""
    return f"qa_{kind}_{case}" if case else f"qa_{kind}"


def _git_path(ev: dict, repo: RepoRef | None) -> str | None:
    """证据块 → git 根相对路径。优先 file:// URI 反推（与 LiveVerifier 同口径），
    退回 URN 里的 doc external id；拒绝路径穿越（git 适配器内还有第二道校验）。"""
    uri = ev.get("source_uri") or ""
    if uri and repo:
        parsed = urlparse(uri)
        if parsed.scheme == "file" and parsed.path:
            try:
                disk = Path(unquote(parsed.path)).resolve(strict=False)
                root = Path(repo.repo_path).resolve(strict=False)
                return disk.relative_to(root).as_posix()
            except (ValueError, OSError):
                pass
    candidate = ev.get("path") or ""
    if candidate and ".." not in candidate.split("/") and "\x00" not in candidate \
            and not candidate.startswith("-"):
        return candidate
    return None


# ── 节点工厂 ─────────────────────────────────────────────────────────────────

def make_nodes(deps: QaDeps) -> dict[str, Callable]:
    tracer = deps.tracer

    # 1) 意图分类 ─────────────────────────────────────────────────────────────
    async def intent(state: dict) -> dict:
        question = state["question"]
        with tracer.span("qa.intent", run_id=state.get("run_id", "")):
            # P4 高精度免模型：精确符号形态的题面必然是代码/符号类问题，直接判 code，
            # 省一次意图 LLM 调用（P0 实测三次本地 LLM 调用占 >99% 墙钟）。这是确定性
            # 路由而非降级：不写 degraded_flags，零 token 累积，来源单独留痕。
            if parse_symbol_reference(question) is not None:
                return {
                    "intent": QaIntent.CODE.value,
                    "intent_degraded": False,
                    "intent_source": INTENT_SOURCE_BYPASS,
                }
            try:
                env = await deps.model.structured(
                    build_intent_messages(question),
                    INTENT_SCHEMA,
                    key=_model_key(state, "intent"),
                )
                data = env.get("data")
                intent_value = (data or {}).get("intent")
                if env.get("degraded") or not data or intent_value not in _VALID_INTENTS:
                    raise AdapterUnavailableError("意图分类返回降级或非法枚举")
                out: dict[str, Any] = {
                    "intent": intent_value,
                    "intent_degraded": False,
                    "intent_source": INTENT_SOURCE_MODEL,
                    "model_version": env.get("model", ""),
                    "tokens_total": _add_tokens(state, env),
                }
            except (AdapterUnavailableError, ValidationError) as exc:
                # 保守兜底：拿不准归 code（误判非 code 会漏 live-verify，代价不对称）
                out = {
                    "intent": fallback_classify_intent(question).value,
                    "intent_degraded": True,
                    "intent_source": INTENT_SOURCE_FALLBACK,
                    "degraded_flags": _flags(state, f"intent_degraded:{type(exc).__name__}"),
                }
        return out

    # 2) ACL 前置判定 ─────────────────────────────────────────────────────────
    async def acl(state: dict) -> dict:
        verdict = check_acl(
            QaIntent(state["intent"]), state.get("acl_scope", "default"),
            params=deps.params,
        )
        if verdict.outcome == "out_of_scope":
            return {
                "status": QaStatus.UNKNOWN.value,
                "unknown_reason": "out_of_scope",
                "denied_reason": verdict.reason,
            }
        if verdict.outcome == "deny":
            return {"status": QaStatus.DENIED.value, "denied_reason": verdict.reason}
        return {}

    # 3) 检索 + 两形态归一化 ───────────────────────────────────────────────────
    async def retrieval(state: dict) -> dict:
        with tracer.span("qa.retrieval", run_id=state.get("run_id", "")):
            kwargs: dict[str, Any] = {
                "project": state.get("project", ""),
                "branch": state.get("branch", "main"),
                "acl_scope": state.get("acl_scope", "default"),
                # S1.6 标定接缝：默认 8 与原硬编码一致，改值经 profile qa 段注入并 bump 版本
                "limit": deps.params.retrieval_limit,
            }
            flags = list(state.get("degraded_flags") or [])
            # 能力探测：HybridSearchAdapter.search 有 kinds 形参，FtsSearchAdapter 没有
            search_params = inspect.signature(deps.search.search).parameters
            supports_kinds = "kinds" in search_params
            if state["intent"] == QaIntent.CODE.value:
                if supports_kinds:
                    kwargs["kinds"] = ("code_symbol", "text")
                else:
                    flags.append("kinds_filter_unsupported")
                # P3：题面精确符号形态（Type.member(label:)）→ 确定性置顶路由；
                # 解析不出/多符号/非 code 意图都不传，检索维持原混合路径
                symbol_ref = parse_symbol_reference(state["question"])
                if symbol_ref is not None:
                    if "symbol_name" in search_params:
                        kwargs["symbol_name"] = symbol_ref.normalized_name
                    else:
                        flags.append("symbol_pin_unsupported")
            result = await deps.search.search(state["question"], **kwargs)

            if isinstance(result, HybridResult):
                scores = {s.urn: s.total for s in result.scores}
                chunks = result.chunks
                trace = {
                    "fts_available": result.trace.fts_available,
                    "vector_available": result.trace.vector_available,
                    "degraded": result.trace.degraded,
                    "fts_urns": list(result.trace.fts_urns),
                    "vector_urns": list(result.trace.vector_urns),
                    "symbol_pinned": result.trace.symbol_pinned,
                    "symbol_pin_urn": result.trace.symbol_pin_urn,
                    "symbol_pin_name": result.trace.symbol_pin_name,
                }
                degraded = result.degraded
                event_id = result.event_id
            else:
                # FtsSearchAdapter 返回裸 list[KnowledgeChunk]：无归一化分数、无留痕
                chunks = list(result)
                scores = {}
                trace = {}
                degraded = False
                event_id = None

            evidence = [
                to_evidence_dict(c, score=scores.get(c.urn)) for c in chunks
            ]
            history = [
                h.model_dump() for h in extract_history_testimony(evidence)
            ]
            return {
                "evidence": evidence,
                "retrieval_event_id": event_id,
                "retrieval_degraded": degraded,
                "retrieval_trace": trace,
                "history_testimony": history,
                "degraded_flags": _flags(state, *flags),
            }

    # 4) 充分性早终止 ─────────────────────────────────────────────────────────
    async def sufficiency(state: dict) -> dict:
        verdict = assess_sufficiency(
            QaIntent(state["intent"]), state.get("evidence") or [],
            params=deps.params,
        )
        if not verdict.sufficient:
            return {
                "status": QaStatus.UNKNOWN.value,
                "unknown_reason": verdict.reason or "insufficient_evidence",
            }
        return {}

    # 5) 代码证据强制 live-verify（调用数=代码证据块数，100% 留痕由适配器保证）──
    async def verify(state: dict) -> dict:
        with tracer.span("qa.verify", run_id=state.get("run_id", "")):
            results: list[dict] = []
            head = ""
            for ev in state["evidence"]:
                if ev.get("kind") != "code_symbol":
                    continue
                raw = await deps.search.live_verify(ev["urn"], "")
                row = {k: raw.get(k) for k in _VERIFY_KEYS}
                results.append(row)
                head = head or str(raw.get("head_commit") or "")
            return {"verify_results": results, "data_version": head}

    # 6) HEAD 证据刷新 ────────────────────────────────────────────────────────
    async def head_refresh(state: dict) -> dict:
        with tracer.span("qa.head_refresh", run_id=state.get("run_id", "")):
            by_urn = {v["urn"]: v for v in state.get("verify_results") or []}
            stale_urns = [
                ev["urn"] for ev in state["evidence"]
                if by_urn.get(ev["urn"], {}).get("body_changed") is True
            ]
            if not stale_urns:
                return {"head_refreshed_urns": []}
            if deps.scm is None or deps.repo is None:
                return {
                    "head_refreshed_urns": [],
                    "degraded_flags": _flags(state, "head_refresh_unavailable"),
                }

            evidence = [dict(ev) for ev in state["evidence"]]
            refreshed: list[str] = []
            failures: list[str] = []
            head = state.get("data_version") or ""
            for ev in evidence:
                if ev["urn"] not in stale_urns:
                    continue
                git_path = _git_path(ev, deps.repo)
                if git_path is None:
                    failures.append(ev["urn"])
                    continue
                try:
                    text = await deps.scm.read_file_at_commit(
                        deps.repo, git_path, head
                    )
                    suffix = Path(git_path).suffix.lower()
                    if suffix == ".swift":
                        rechunked = chunk_swift(text)
                    elif suffix in {".md", ".markdown"}:
                        rechunked = chunk_markdown(text)
                    else:
                        failures.append(ev["urn"])
                        continue
                    current = select_chunk_by_anchor(rechunked, ev.get("anchor") or "")
                    if current is None:
                        failures.append(ev["urn"])
                        continue
                    ev["text"] = current.text
                    ev["content_hash"] = hashlib.sha256(
                        current.text.encode("utf-8")
                    ).hexdigest()
                    ev["metadata"] = {
                        **(ev.get("metadata") or {}),
                        "chunker_version": (
                            SWIFT_CHUNKER_VERSION if suffix == ".swift"
                            else MARKDOWN_CHUNKER_VERSION
                        ),
                        "head_refreshed": True,
                    }
                    refreshed.append(ev["urn"])
                except (PathMissingAtCommitError, AdapterUnavailableError):
                    # 保留索引证据但不进 refreshed：三态标注据此落 unknown
                    failures.append(ev["urn"])

            flags = _flags(state)
            if failures:
                flags = list(dict.fromkeys(
                    [*flags, *[f"head_refresh_failed:{u}" for u in failures]]
                ))
            return {
                "evidence": evidence,
                "head_refreshed_urns": refreshed,
                "degraded_flags": flags,
            }

    # 7) 约束生成 ─────────────────────────────────────────────────────────────
    async def generation(state: dict) -> dict:
        with tracer.span("qa.generation", run_id=state.get("run_id", "")):
            tokens = state.get("tokens_total") or {"in": 0, "out": 0}
            if tokens["in"] >= deps.limits.max_tokens_per_run:
                return {
                    "status": QaStatus.UNKNOWN.value,
                    "unknown_reason": "budget_exceeded",
                }
            attempt = int(state.get("attempts") or 0) + 1
            key = _model_key(
                state, "answer_retry" if attempt > 1 else "answer"
            )
            evidence = state.get("evidence") or []
            # P4：仅当 P3 符号置顶真实命中（retrieval_trace 带 pin URN）时瘦身 prompt；
            # FTS 裸 list/未命中/重名路径 trace 无此键，pinned_urn=None，消息维持全文
            pinned_urn = (state.get("retrieval_trace") or {}).get("symbol_pin_urn") or None
            messages = build_answer_messages(
                state["question"], evidence, build_urn_whitelist(evidence),
                regen_errors=state.get("citation_errors") or None,
                pinned_urn=pinned_urn,
            )
            try:
                env = await deps.model.structured(
                    messages, AnswerDraft.model_json_schema(), key=key,
                    # 零引用草稿在适配器重试循环内当场拦，不白跑一轮图迭代
                    required_nonempty=("citations",),
                )
            except AdapterUnavailableError as exc:
                return {
                    "status": QaStatus.UNKNOWN.value,
                    "unknown_reason": "model_unavailable",
                    "degraded_flags": _flags(state, f"model_unavailable:{type(exc).__name__}"),
                }
            if env.get("degraded") or not env.get("data"):
                return {
                    "status": QaStatus.UNKNOWN.value,
                    "unknown_reason": "model_unavailable",
                    "degraded_flags": _flags(state, "model_degraded_envelope"),
                    "tokens_total": _add_tokens(state, env),
                    "model_version": env.get("model", ""),
                }
            try:
                draft = AnswerDraft.model_validate(env["data"])
            except ValidationError as exc:
                return {
                    "status": QaStatus.UNKNOWN.value,
                    "unknown_reason": "model_unavailable",
                    "degraded_flags": _flags(
                        state, f"draft_schema_invalid:{exc.error_count()}"
                    ),
                }
            return {
                "draft": draft.model_dump(),
                "attempts": attempt,
                "tokens_total": _add_tokens(state, env),
                "model_version": env.get("model", ""),
                "prompt_version": PROMPT_VERSION,
                "params_version": PARAMS_VERSION,
            }

    # 8) 引用校验（纯判定节点；预算耗尽在这里落终态，路由函数只读本不写）──────
    async def citation_check(state: dict) -> dict:
        draft = AnswerDraft.model_validate(state["draft"])
        verdict = validate_citations(draft, state.get("evidence") or [])
        out: dict[str, Any] = {"citation_errors": list(verdict.errors)}
        if not verdict.ok and not should_regenerate(
            int(state.get("attempts") or 1), verdict
        ):
            out["status"] = QaStatus.UNKNOWN.value
            out["unknown_reason"] = "citation_failed_exhausted"
        return out

    # 9) 装配（三态标注 + 事故双段 + URI 反查）────────────────────────────────
    async def assembly(state: dict) -> dict:
        with tracer.span("qa.assembly", run_id=state.get("run_id", "")):
            evidence = state.get("evidence") or []
            draft = AnswerDraft.model_validate(state["draft"])

            doc_hashes: dict[str, str] = {}
            if deps.store is not None:
                needed = {
                    ev["doc_urn"] for ev in evidence
                    if not ev.get("doc_content_hash") or not ev.get("source_uri")
                }
                if needed:
                    docs = await asyncio.gather(*[
                        asyncio.to_thread(deps.store.get_document, urn)
                        for urn in needed
                    ])
                    for doc in docs:
                        if doc is None:
                            continue
                        doc_hashes[doc.urn] = doc.content_hash
                        for ev in evidence:
                            if ev["doc_urn"] == doc.urn and not ev.get("source_uri"):
                                ev["source_uri"] = doc.uri
            for ev in evidence:
                if ev.get("doc_content_hash") and ev["doc_urn"] not in doc_hashes:
                    doc_hashes[ev["doc_urn"]] = ev["doc_content_hash"]

            citations = annotate_version_states(
                draft.citations, evidence,
                state.get("verify_results") or [],
                head_refreshed_urns=state.get("head_refreshed_urns") or [],
                doc_hashes=doc_hashes,
            )
            if state["intent"] == QaIntent.INCIDENT.value \
                    and state.get("history_testimony"):
                history = [
                    HistoryTestimony.model_validate(h)
                    for h in state["history_testimony"]
                ]
                answer: AssembledAnswer = assemble_incident(
                    history, draft, citations, evidence=evidence,
                )
            else:
                answer = assemble_final(draft, citations)

            return {
                "final_answer": answer.model_dump(),
                "status": QaStatus.ANSWERED.value,
            }

    return {
        N_INTENT: intent,
        N_ACL: acl,
        N_RETRIEVAL: retrieval,
        N_SUFFICIENCY: sufficiency,
        N_VERIFY: verify,
        N_HEAD_REFRESH: head_refresh,
        N_GENERATION: generation,
        N_CITATION_CHECK: citation_check,
        N_ASSEMBLY: assembly,
    }


# ── 条件边路由（纯函数，读 state 返回下一节点名/END 标记）──────────────────────

END_MARK = "__end__"


def route_after_acl(state: dict) -> str:
    return END_MARK if state.get("status") else N_RETRIEVAL


def route_after_sufficiency(state: dict) -> str:
    if state.get("status"):
        return END_MARK
    return N_VERIFY if state.get("intent") == QaIntent.CODE.value else N_GENERATION


def route_after_verify(state: dict) -> str:
    changed = any(
        v.get("body_changed") is True for v in state.get("verify_results") or []
    )
    return N_HEAD_REFRESH if changed else N_GENERATION


def route_after_generation(state: dict) -> str:
    return END_MARK if state.get("status") else N_CITATION_CHECK


def route_after_citation(state: dict) -> str:
    if state.get("status"):  # 预算耗尽终态由 citation_check 节点写入
        return END_MARK
    if state.get("citation_errors"):
        return N_GENERATION
    return N_ASSEMBLY
