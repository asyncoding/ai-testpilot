"""重构感知 stale/reindex 服务（docs/08 FR-FRS-002，S4.2）。

CommitPushed 事件 → scm ChangeSet → core 纯函数配对 rename →
受影响符号切块立即标 STALE（检索侧 fresh 过滤即时生效，无需等 reindex）→
rename 链落 relations（doc_urn=NULL，不被单文档级联清掉）→
高价值文档立即 reindex，低价值 lazy 仅标记。

身份解析纪律：只引用库内真实存在的符号 URN，绝不按 git 路径臆造 URN
（fs 文档 external_id 带 root alias，与 git 相对路径没有恒等映射）。
reindex 后新符号入库，会对首轮未解析的 rename 再补一次链。

零 schema 变更：stale/valid_to 用 chunks 既有列，rename 复用 relations 表，
过程留痕走 ingestion_runs（trigger='event-reindex'）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from testpilot_core.chunking import SWIFT_CHUNKER_VERSION, chunk_markdown
from testpilot_core.enums import ChangeType
from testpilot_core.errors import AdapterUnavailableError
from testpilot_core.freshness import (
    is_high_value,
    pair_symbol_renames,
    split_symbol_id,
)
from testpilot_core.models import EventEnvelope, Relation, SourceDocument
from testpilot_core.urn import make_urn, parse_urn

from .ingestion import (
    _SOURCE_SYSTEM,
    _build_chunks,
    _build_entities_relations,
    _chunk_swift_source,
)

# profile 未配 freshness 段时的保守默认（高价值门槛：质量优或被多处引用）
DEFAULT_HIGH_VALUE_QUALITY = 0.7
DEFAULT_HIGH_VALUE_MIN_REFS = 2
_RENAME_KIND = "renamed_to"


@dataclass
class ReindexOutcome:
    doc_urn: str
    high_value: bool
    latency_ms: int
    chunks: int


@dataclass
class FreshnessReport:
    event_id: str
    base_commit: str
    head_commit: str
    removed: int = 0
    added: int = 0
    renamed_pairs: list[dict[str, Any]] = field(default_factory=list)
    stale_rows_marked: int = 0
    rename_relations_new: int = 0
    reindexed: list[dict[str, Any]] = field(default_factory=list)
    lazy_docs: list[str] = field(default_factory=list)
    # 文档在工作区已不存在（如整文件 move 走且新路径尚未入库）：STALE 已护住检索，
    # 等下轮入库扫描级联，不硬闯 read_content 造 handler_error 死信
    reindex_skipped_missing: list[str] = field(default_factory=list)
    # 工作集合：首轮挂起、补链成功即注销；to_dict 去重排序输出
    unresolved_anchors: set[str] = field(default_factory=set)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "base_commit": self.base_commit,
            "head_commit": self.head_commit,
            "removed": self.removed,
            "added": self.added,
            "renamed_pairs": self.renamed_pairs,
            "stale_rows_marked": self.stale_rows_marked,
            "rename_relations_new": self.rename_relations_new,
            "reindexed": self.reindexed,
            "lazy_docs": self.lazy_docs,
            "reindex_skipped_missing": self.reindex_skipped_missing,
            "unresolved_anchors": sorted(self.unresolved_anchors),
        }


def _freshness_config(container) -> dict[str, Any]:
    return getattr(container.config, "freshness", {}) or {}


def _urns_matching_path(store, path: str, anchor: str,
                        cache: dict[tuple[str, str], list[str]]) -> list[str]:
    """裸锚点查 URN 后再按 git 相对路径过滤 external_id（防同名锚点跨文件误配）。

    两种 symbol_id 形态都要支持：``<path>#<anchor>``（合成事件/录制夹具）走路径
    过滤；裸 ``<anchor>``（GitCodeRepository.diff_symbol_sets 的真实产物，按文件
    对账后摊平，不带路径）无法做路径过滤，退化为锚点全库匹配——LIKE 已做
    ``#``/``#L`` 去歧，同名跨文件误配由调用侧的关系/文档归属再兜一层。
    """
    key = (path, anchor)
    if key in cache:
        return cache[key]
    raw = store.find_symbol_urns_by_anchor(anchor)
    if not path:
        cache[key] = raw
        return raw
    urns = []
    for urn in raw:
        # external_id 形态 <root alias>/<rel>#<anchor>，先剥锚点再比路径
        external_path = parse_urn(urn)[2].partition("#")[0]
        # external 路径形态 <alias>/<rel>，git path 形态 <rel>
        if external_path == path or external_path.endswith("/" + path):
            urns.append(urn)
    cache[key] = urns
    return urns


def resolve_change_symbols(store, change_set) -> tuple[list, list]:
    """git ChangeSet → 库内符号 URN 解析（freshness handler 与 impact 服务共用）。

    返回 ``(ChangedSymbol, UnresolvedSymbol)`` 两个 core 模型列表；removed 符号
    若已挂 ``renamed_to`` 链（S4.2 落账）则带上目标 URN。解析不到的符号如实
    进 unresolved，绝不按 git 路径臆造 URN。
    """
    from testpilot_core.impact import ChangedSymbol, UnresolvedSymbol

    cache: dict[tuple[str, str], list[str]] = {}
    changed: list[ChangedSymbol] = []
    unresolved: list[UnresolvedSymbol] = []
    for change in change_set.symbol_changes:
        path, anchor = split_symbol_id(change.symbol_id)
        urns = _urns_matching_path(store, path, anchor, cache)
        if not urns:
            unresolved.append(UnresolvedSymbol(
                symbol_id=change.symbol_id,
                change_type=change.change_type,
            ))
            continue
        for urn in urns:
            renamed_to = ""
            if change.change_type == ChangeType.REMOVED:
                renamed_to = store.get_rename_target(urn) or ""
            changed.append(ChangedSymbol(
                urn=urn,
                change_type=change.change_type,
                renamed_to=renamed_to,
            ))
    return changed, unresolved


def _write_rename_relations(store, event: EventEnvelope, rename,
                            old_urns: list[str], new_urns: list[str]) -> int:
    """两侧 URN 齐备时落 renamed_to 链，返回新登记行数（已存在不计）。"""
    created = 0
    # 路径过滤后两侧各属一个 external_id，按序对位即同文件身份延续
    for old_urn, new_urn in zip(sorted(old_urns), sorted(new_urns)):
        relation = Relation(
            urn=make_urn(
                _SOURCE_SYSTEM, "rel",
                f"rename#{rename.old_symbol_id}->{rename.new_symbol_id}",
            ),
            source_urn=old_urn,
            target_urn=new_urn,
            kind=_RENAME_KIND,
            confidence=rename.confidence,
            evidence_urns=[event.event_id],
        )
        if store.upsert_rename_relation(relation):
            created += 1
    return created


async def reindex_document(
    container, doc: SourceDocument, text: str, head_commit: str, run_id: str
) -> int:
    """单文档 reindex：重切重评 → 整组替换（幂等）。返回新切块数。

    调用方负责先确认正文可读（缺失文档在调用前跳过）；向量适配器存在时
    先存旧切块 URN，嵌入后清理向量孤儿，与入库管线同口径。
    """
    store = container.get("store")
    swift_chunker = container.get("swift_chunker")
    vectors = container.get("vectors")
    old_ids_by_doc: dict[str, list[str]] = {}
    if vectors is not None:
        old_ids_by_doc[doc.urn] = store.list_chunk_urns(doc.urn)

    kind = doc.metadata_.get("kind_hint") or "markdown"
    if kind == "swift":
        raws, swift_version = _chunk_swift_source(
            text, swift_chunker, store, doc.urn, run_id
        )
    else:
        # 与 ingestion.run_ingestion 保持同一口径（markdown 轮也透传 swift 版本位）
        raws, swift_version = chunk_markdown(text), SWIFT_CHUNKER_VERSION
    chunks, symbol_urns = _build_chunks(doc, raws, head_commit, swift_version)
    entities, relations = _build_entities_relations(doc, raws, symbol_urns)
    store.replace_document_index(doc, chunks, entities, relations, run_id)

    model_gw = container.get("model")
    if vectors is not None and model_gw is not None:
        from .ingestion import IngestionReport, _embed_pending

        await _embed_pending(
            store, model_gw, vectors, run_id,
            IngestionReport(run_id=run_id, trigger="event-reindex"),
            container.get("tracer"), old_ids_by_doc,
        )
    return len(chunks)


async def handle_commit_pushed(container, event: EventEnvelope) -> FreshnessReport:
    """CommitPushed handler（经 event_dispatch.register_handler 挂接）。

    事件 payload 契约：``{"base_commit": <sha|空树>, "head_commit": <sha>}``；
    head 缺省取 scm 当前 HEAD。缺 base 直接抛错（派发层转 handler_error 死信）。
    """
    scm = container.get("scm")
    repo = container.get("repo")
    store = container.get("store")
    tracer = container.get("tracer")
    if scm is None or repo is None:
        raise RuntimeError("容器未装配 scm/repo，无法处理 CommitPushed")
    base = event.payload.get("base_commit", "")
    if not base:
        raise ValueError("CommitPushed payload 缺少 base_commit，无法对账 ChangeSet")
    head = event.payload.get("head_commit") or await scm.head_sha(repo)

    with tracer.span(
        "freshness.commit",
        run_id=event.event_id,
        metadata={"base": str(base)[:12], "head": str(head)[:12]},
    ):
        change_set = await scm.get_change_set(repo, base, head)
        renames = pair_symbol_renames(
            change_set.symbol_changes, change_set.file_renames
        )
        report = FreshnessReport(
            event_id=event.event_id,
            base_commit=str(base),
            head_commit=str(head),
        )
        report.removed = sum(
            1 for c in change_set.symbol_changes
            if c.change_type == ChangeType.REMOVED
        )
        report.added = sum(
            1 for c in change_set.symbol_changes
            if c.change_type == ChangeType.ADDED
        )

        # 1) rename 链：首轮只写两侧 URN 都在库内的配对；其余挂起，reindex 后补。
        #    同文件 selector rename 下新旧 URN 永不共存（reindex 前只有旧、后只有
        #    新），故首轮把旧 URN 暂存，补链阶段即便旧切块已替换也能引用。
        cache: dict[tuple[str, str], list[str]] = {}
        pending_renames = []
        stashed_old: dict[str, list[str]] = {}
        for rename in renames:
            report.renamed_pairs.append({
                "old": rename.old_symbol_id, "new": rename.new_symbol_id,
                "confidence": rename.confidence, "reason": rename.reason,
                "resolved": False,
            })
            old_path, old_anchor = split_symbol_id(rename.old_symbol_id)
            new_path, new_anchor = split_symbol_id(rename.new_symbol_id)
            old_urns = _urns_matching_path(store, old_path, old_anchor, cache)
            new_urns = _urns_matching_path(store, new_path, new_anchor, cache)
            if old_urns:
                stashed_old[rename.old_symbol_id] = old_urns
            else:
                report.unresolved_anchors.add(old_anchor)
            if not new_urns:
                report.unresolved_anchors.add(new_anchor)
            if old_urns and new_urns:
                report.rename_relations_new += _write_rename_relations(
                    store, event, rename, old_urns, new_urns)
                report.renamed_pairs[-1]["resolved"] = True
                report.unresolved_anchors.discard(old_anchor)
                report.unresolved_anchors.discard(new_anchor)
            else:
                pending_renames.append(rename)

        # 2) 所有非新增变化（removed/signature_changed/modified）的切块标 STALE
        stale_anchors = sorted({
            (split_symbol_id(c.symbol_id)[0], split_symbol_id(c.symbol_id)[1])
            for c in change_set.symbol_changes
            if c.change_type != ChangeType.ADDED
        })
        stale_urns: list[str] = []
        for path, anchor in stale_anchors:
            urns = _urns_matching_path(store, path, anchor, cache)
            if not urns:
                report.unresolved_anchors.add(anchor)
            stale_urns.extend(urns)
        report.stale_rows_marked = store.mark_stale_by_symbol_urns(
            stale_urns, valid_to=str(head)
        )

        # 3) 高价值立即 reindex / 低价值 lazy（阈值走 profile freshness 段）
        cfg = _freshness_config(container)
        quality_threshold = float(
            cfg.get("high_value_quality", DEFAULT_HIGH_VALUE_QUALITY)
        )
        reference_threshold = int(
            cfg.get("high_value_min_refs", DEFAULT_HIGH_VALUE_MIN_REFS)
        )
        docs_adapter = container.get("docs")
        urn_to_doc = store.map_symbol_urns_to_docs(stale_urns)
        urns_by_doc: dict[str, list[str]] = {}
        for urn, doc_urn in urn_to_doc.items():
            urns_by_doc.setdefault(doc_urn, []).append(urn)

        for doc_urn in sorted(urns_by_doc):
            doc_urns = urns_by_doc[doc_urn]
            max_refs = max(
                (store.count_incoming_calls(u) for u in doc_urns), default=0
            )
            high_value = is_high_value(
                quality_score=store.max_quality_for_doc(doc_urn),
                reference_count=max_refs,
                quality_threshold=quality_threshold,
                reference_threshold=reference_threshold,
            )
            if not high_value:
                report.lazy_docs.append(doc_urn)
                continue
            doc = store.get_document(doc_urn)
            try:
                text = docs_adapter.read_content(doc_urn)
            except AdapterUnavailableError:
                # 整文件 move/删除：旧切块已 STALE，检索不可见；等入库扫描级联清理
                report.reindex_skipped_missing.append(doc_urn)
                continue
            run_id = f"reidx-{uuid4().hex[:12]}"
            store.start_ingestion_run(run_id, _SOURCE_SYSTEM, "event-reindex")
            t0 = time.monotonic()
            try:
                n_chunks = await reindex_document(
                    container, doc, text, str(head), run_id
                )
            except Exception:
                store.finish_ingestion_run(
                    run_id, "failed", {}, {"event_id": event.event_id}
                )
                raise
            latency_ms = int((time.monotonic() - t0) * 1000)
            store.finish_ingestion_run(
                run_id, "succeeded",
                {"chunks_total": n_chunks},
                {"event_id": event.event_id, "latency_ms": latency_ms},
            )
            report.reindexed.append(ReindexOutcome(
                doc_urn=doc_urn, high_value=True,
                latency_ms=latency_ms, chunks=n_chunks,
            ).__dict__)

        # 4) reindex 后新符号已入库：补挂首轮未解析的 rename 链。
        #    必须清解析缓存——首轮缓存了新锚点的空结果，不清会永远判未解析。
        #    旧 URN 取首轮暂存（同文件 rename 后旧切块已被替换，库内查不到）。
        cache.clear()
        for rename in pending_renames:
            old_path, old_anchor = split_symbol_id(rename.old_symbol_id)
            new_path, new_anchor = split_symbol_id(rename.new_symbol_id)
            old_urns = (
                _urns_matching_path(store, old_path, old_anchor, cache)
                or stashed_old.get(rename.old_symbol_id, [])
            )
            new_urns = _urns_matching_path(store, new_path, new_anchor, cache)
            if old_urns and new_urns:
                report.rename_relations_new += _write_rename_relations(
                    store, event, rename, old_urns, new_urns)
                report.unresolved_anchors.discard(old_anchor)
                report.unresolved_anchors.discard(new_anchor)
                for pair in report.renamed_pairs:
                    if pair["old"] == rename.old_symbol_id:
                        pair["resolved"] = True

    return report
