"""知识入库管线（docs/08 FR-ING-001 + FR-CON-002）。

扫描 → 与库内 content_hash 对账（created/updated/deleted/unchanged）→
逐文档「读正文 → 切块 → 质量评分 → 符号实体/调用关系 → 单事务登记」；
单文档失败进死信不阻断整批；变更/删除发事件（幂等）；全链路 trace。

纯切块/评分逻辑在 core（testpilot_core.chunking / quality），
存储事务在 adapters Store，本模块只做编排（六边形分层）。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from testpilot_core.chunking import RawChunk, chunk_markdown, chunk_swift
from testpilot_core.models import Entity, EventEnvelope, KnowledgeChunk, Relation
from testpilot_core.quality import score_code_symbol, score_text
from testpilot_core.urn import make_urn, parse_urn

_SOURCE_SYSTEM = "fs-docs"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class IngestionReport:
    run_id: str
    trigger: str
    status: str = "running"
    seen: int = 0
    created: int = 0
    updated: int = 0
    deleted: int = 0
    unchanged: int = 0
    failed: int = 0
    chunks_total: int = 0
    details: list[dict[str, Any]] = field(default_factory=list)
    deleted_urns: list[str] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        return {
            "seen": self.seen, "created": self.created, "updated": self.updated,
            "deleted": self.deleted, "unchanged": self.unchanged, "failed": self.failed,
            "chunks_total": self.chunks_total,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.counts(), "run_id": self.run_id, "trigger": self.trigger,
                "status": self.status, "details": self.details,
                "deleted_urns": self.deleted_urns}


def _kind_of(doc) -> str:
    return doc.metadata_.get("kind_hint") or "markdown"


def _event(event_type: str, urn: str, project: str, mtime, extra: dict) -> EventEnvelope:
    now = datetime.now(timezone.utc)
    return EventEnvelope(
        event_id=f"doc:{urn}:{event_type}:{extra.get('content_hash', '')[:12]}",
        event_type=event_type,
        source_system=_SOURCE_SYSTEM,
        occurred_at=mtime or now,
        project=project,
        subject_ref=urn,
        payload=extra,
        idempotency_key=f"doc:{urn}:{event_type}:{extra.get('content_hash', '')[:12]}",
    )


def _build_chunks(doc, raws: list[RawChunk]) -> tuple[list[KnowledgeChunk], dict[str, str]]:
    """RawChunk → KnowledgeChunk + 符号名→symbol URN 映射。"""
    _, _, external_id, _ = parse_urn(doc.urn)
    chunks: list[KnowledgeChunk] = []
    symbol_urns: dict[str, str] = {}
    for idx, raw in enumerate(raws):
        if raw.kind == "code_symbol":
            symbol_urn = make_urn(_SOURCE_SYSTEM, "symbol", f"{external_id}#{raw.anchor}")
            # 同时按去歧 anchor 与规范符号名建索引（调用关系引用的是规范名）
            symbol_urns.setdefault(raw.anchor, symbol_urn)
            symbol_urns.setdefault(raw.title, symbol_urn)
            q = score_code_symbol(
                raw.text, signature=raw.signature, has_doc_comment=raw.has_doc_comment,
                call_count=len(raw.relations), authority_level=str(doc.authority_level),
            )
        else:
            symbol_urn = None
            q = score_text(
                raw.text, heading_path=raw.heading_path,
                authority_level=str(doc.authority_level),
            )
        chunk_urn = make_urn(_SOURCE_SYSTEM, "chunk", f"{external_id}#{raw.anchor}")
        chunks.append(KnowledgeChunk(
            urn=chunk_urn,
            doc_urn=doc.urn,
            index=idx,
            kind=raw.kind,
            text=raw.text,
            symbol_urn=symbol_urn,
            quality_score=q.score,
            branch=doc.branch,
            stale="fresh",
            content_hash=_sha256(raw.text),
            metadata_={
                "title": raw.title,
                "start_line": raw.start_line,
                "end_line": raw.end_line,
                "signature": raw.signature,
                "heading_path": list(raw.heading_path),
                "quality_components": q.components,
                "quality_reasons": q.reasons,
            },
        ))
    return chunks, symbol_urns


def _build_entities_relations(
    doc, raws: list[RawChunk], symbol_urns: dict[str, str]
) -> tuple[list[Entity], list[tuple[Relation, list[str]]]]:
    _, _, external_id, _ = parse_urn(doc.urn)
    entities: list[Entity] = []
    relations: list[tuple[Relation, list[str]]] = []
    for raw in raws:
        if raw.kind != "code_symbol":
            continue
        source_urn = symbol_urns[raw.anchor]
        entities.append(Entity(
            urn=source_urn, kind="code_symbol", name=raw.title, branch=doc.branch,
            metadata_={"signature": raw.signature, "file": external_id,
                       "start_line": raw.start_line, "end_line": raw.end_line},
        ))
        seen_targets: set[str] = set()
        for call in raw.relations:
            target_urn = symbol_urns.get(call.target_symbol)
            if target_urn is None or target_urn == source_urn or target_urn in seen_targets:
                continue
            seen_targets.add(target_urn)
            relation_urn = make_urn(
                _SOURCE_SYSTEM, "rel",
                f"{external_id}#{raw.anchor}->{call.target_symbol}",
            )
            relations.append((Relation(
                urn=relation_urn,
                source_urn=source_urn,
                target_urn=target_urn,
                kind="calls",
                confidence=0.9,  # 词法结构解析所得；AST 替换后可上调
                evidence_urns=[doc.urn],
            ), [call.evidence]))
    return entities, relations


async def run_ingestion(container, *, trigger: str = "manual", full: bool = False) -> IngestionReport:
    """跑一轮入库。full=True 时忽略 hash 对账，全部重切重登记（仍幂等）。"""
    docs_adapter = container.get("docs")
    store = container.get("store")
    tracer = container.get("tracer")
    project = container.config.project.name
    run_id = f"ing-{uuid4().hex[:12]}"
    report = IngestionReport(run_id=run_id, trigger=trigger)

    with tracer.span("ingest.run", run_id=run_id, metadata={"trigger": trigger, "full": full}):
        # 扫描期坏文件（编码错误/IO 错误）同样隔离：进死信 + 状态 failed，不拖垮整批
        scan_safely = getattr(docs_adapter, "scan_safely", None)
        if scan_safely is not None:
            scanned, scan_errors = scan_safely()
        else:
            scanned, scan_errors = await docs_adapter.list_documents(), []
        report.seen = len(scanned) + len(scan_errors)
        for err in scan_errors:
            error = err["error"]
            store.add_dead_letter(
                "ingestion-scan", {"urn": err["urn"], "uri": err.get("uri", "")}, error
            )
            store.mark_document_failed(err["urn"], _SOURCE_SYSTEM, "", run_id, error)
            report.failed += 1
            report.details.append(
                {"urn": err["urn"], "outcome": "failed", "stage": "scan", "error": error}
            )

        known = {r["urn"]: r["content_hash"] for r in store.list_documents(_SOURCE_SYSTEM)}

        created, updated, unchanged, to_delete = _classify(scanned, known, full=full)
        report.unchanged = len(unchanged)
        report.deleted = len(to_delete)

        store.start_ingestion_run(run_id, _SOURCE_SYSTEM, trigger)

        # 1) 删除：发事件 → 级联删（发事件先于删，事件表独立保留删除事实）
        for urn in to_delete:
            with tracer.span("ingest.delete", run_id=run_id, metadata={"urn": urn}):
                event = _event("DocumentDeleted", urn, project, None,
                               {"content_hash": known.get(urn, "")})
                store.record_event_if_new(event)
                store.delete_document_cascade(urn)
                report.deleted_urns.append(urn)

        # 2) 新增/变更：逐文档加工，单文档失败隔离
        for bucket_name, docs in (("created", created), ("updated", updated)):
            for doc in docs:
                detail: dict[str, Any] = {
                    "urn": doc.urn, "outcome": bucket_name,
                    "title": doc.metadata_.get("relative_in_root", doc.title),
                    "kind": _kind_of(doc),
                }
                try:
                    with tracer.span("ingest.doc", run_id=run_id,
                                     metadata={"urn": doc.urn, "kind": detail["kind"]}):
                        text = docs_adapter.read_content(doc.urn)
                        raws = (chunk_swift(text) if detail["kind"] == "swift"
                                else chunk_markdown(text))
                        chunks, symbol_urns = _build_chunks(doc, raws)
                        entities, relations = _build_entities_relations(doc, raws, symbol_urns)
                        store.replace_document_index(
                            doc, chunks, entities, relations, run_id
                        )
                        event = _event("DocumentChanged", doc.urn, project,
                                       doc.mtime.isoformat() if doc.mtime else None,
                                       {"content_hash": doc.content_hash, "chunks": len(chunks)})
                        store.record_event_if_new(event)
                        detail.update(chunks=len(chunks), entities=len(entities),
                                      relations=len(relations),
                                      avg_quality=round(
                                          sum(c.quality_score for c in chunks) / len(chunks), 4)
                                      if chunks else 0.0)
                        report.chunks_total += len(chunks)
                        if bucket_name == "created":
                            report.created += 1
                        else:
                            report.updated += 1
                except Exception as exc:  # noqa: BLE001 - 单文档失败隔离是显式设计
                    error = f"{type(exc).__name__}: {exc}"
                    store.add_dead_letter(
                        "ingestion",
                        {"urn": doc.urn, "content_hash": doc.content_hash, "run_id": run_id},
                        error,
                    )
                    store.mark_document_failed(
                        doc.urn, _SOURCE_SYSTEM, doc.content_hash, run_id, error
                    )
                    detail.update(outcome="failed", error=error)
                    report.failed += 1
                report.details.append(detail)

        report.status = "succeeded" if report.failed == 0 else "partial"
        store.finish_ingestion_run(run_id, report.status, report.counts(), report.to_dict())

    return report


def _classify(scanned, known: dict[str, str], *, full: bool):
    created, updated, unchanged = [], [], []
    seen_urns: set[str] = set()
    for doc in scanned:
        seen_urns.add(doc.urn)
        if doc.urn not in known:
            created.append(doc)
        elif full or known[doc.urn] != doc.content_hash:
            updated.append(doc)
        else:
            unchanged.append(doc)
    to_delete = [urn for urn in known if urn not in seen_urns]
    return created, updated, unchanged, to_delete
