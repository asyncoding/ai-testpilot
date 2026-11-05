"""live-verify 纯判定（docs/08 FR-QA-002）：只做事实归并，不做任何 IO。

适配器侧负责：在目标 commit 重读文件 → 同一 chunk_swift/chunk_markdown 重切 →
按 anchor 取块 → sha256(RawChunk.text)；本模块只回答"索引时的证据在目标版本
是否仍成立"。

版本位纪律：索引切块与当前切块器版本不一致（含旧索引没有版本位）时，hash 差异
无法归因于代码变更，body_changed 给 None 且 verified=False——**绝不误报变更**。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class VerifyVerdict:
    verified: bool
    exists: bool
    body_changed: bool | None
    reason: str  # unchanged / body_changed / symbol_missing / chunker_version_drift


def verify_evidence(
    *,
    exists: bool,
    indexed_hash: str | None,
    current_hash: str | None,
    indexed_chunker_version: str | None,
    current_chunker_version: str,
) -> VerifyVerdict:
    """把"符号存在性 + 双端 hash + 双端切块器版本"归并成校验结论。

    - 符号在目标版本不存在：verified=False，body_changed=None（无 body 可比）；
    - 切块器版本缺失/不一致：verified=False，body_changed=None（差异不可归因）；
    - 同版本同 hash：verified=True，body_changed=False；
    - 同版本异 hash：verified=False，body_changed=True（stale-index，当场抓现行）。
    """
    if not exists:
        return VerifyVerdict(
            verified=False, exists=False, body_changed=None, reason="symbol_missing"
        )
    if (
        indexed_chunker_version is None
        or indexed_chunker_version != current_chunker_version
    ):
        return VerifyVerdict(
            verified=False, exists=True, body_changed=None,
            reason="chunker_version_drift",
        )
    if indexed_hash is not None and indexed_hash == current_hash:
        return VerifyVerdict(
            verified=True, exists=True, body_changed=False, reason="unchanged"
        )
    return VerifyVerdict(
        verified=False, exists=True, body_changed=True, reason="body_changed"
    )
