"""人审回写 Markdown 用例库适配器（TestCaseRepositoryPort，S2.5 FR-TD-004）。

与只读语料仓**物理隔离**：写根配置化（profile 默认 data_dir/case-writeback），
任何情况下不指向 fs-cases 的只读根。写入纪律对齐 json_tickets：

- R2 守卫：每次写入经 core.policy.assert_writable 校验 Approval（approved 且
  分级一致）与运行期 kill switch，未批/停用一律 PolicyDenied，不产生文件；
- 幂等：manifest 是适配器本地内容寻址账本，idempotency_key 命中即返回同
  external_ref，outbox 重放绝不产生重复文件；
- 原子：tmp+os.replace 落盘，先文件后 manifest（崩溃残留可被同内容重放修复，
  反过来 manifest 先写则会指向缺失文件）；
- 可回读：list_cases 用 S2.1 同一解析器扫描写根，回写产物可无损重新摄入。
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from testpilot_core.approval import (
    CASE_REPO_TARGET_SYSTEM,
    outbox_idempotency_key,
    render_case_markdown,
    writeback_rel_path,
)
from testpilot_core.enums import ActionLevel
from testpilot_core.errors import AdapterUnavailableError
from testpilot_core.models import Approval, CaseSelector, OutboxRecord, TestCase
from testpilot_core.policy import assert_writable
from testpilot_core.testcase import parse_case_markdown
from testpilot_core.urn import parse_urn

MANIFEST_NAME = "_manifest.json"
_MANIFEST_VERSION = 1


class MarkdownCaseRepository:
    """写根目录内布局：``<doc 去 .md>/<case_id>.md`` + 根级 ``_manifest.json``。"""

    def __init__(
        self,
        write_root: str | Path,
        *,
        approval_provider: Callable[[str], Approval | None] | None = None,
        kill_switch: Callable[[], bool] | None = None,
        source_system: str = CASE_REPO_TARGET_SYSTEM,
    ) -> None:
        self.write_root = Path(write_root)
        self.write_root.mkdir(parents=True, exist_ok=True)
        self.approval_provider = approval_provider
        self._kill_switch = kill_switch
        self.source_system = source_system
        self._manifest_path = self.write_root / MANIFEST_NAME

    def _approval(self, approval_id: str) -> Approval | None:
        if self.approval_provider is None:
            return None
        return self.approval_provider(approval_id)

    def _kill(self) -> bool:
        return self._kill_switch() if self._kill_switch else False

    # ── manifest 账本 ───────────────────────────────────────────────────────
    def _load_manifest(self) -> dict:
        if not self._manifest_path.exists():
            return {"version": _MANIFEST_VERSION, "entries": {}}
        return json.loads(self._manifest_path.read_text(encoding="utf-8"))

    @staticmethod
    def _atomic_write(path: Path, text: str) -> None:
        """同目录 tmp 文件 + os.replace：崩溃不留半文件，读者只见全文或旧文。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    def _resolve_inside_root(self, rel_path: str) -> Path:
        """路径穿越守卫：URN 反解值也不盲信，越界即 AdapterUnavailable 响失败。"""
        root = self.write_root.resolve()
        candidate = (root / rel_path).resolve()
        if candidate != root and root not in candidate.parents:
            raise AdapterUnavailableError(f"回写路径越出写根，拒绝写入: {rel_path}")
        return candidate

    # ── outbox 驱动的主入口（worker 派发器调用）──────────────────────────────
    async def write_approved_record(self, record: OutboxRecord) -> str:
        """把一条已审批 outbox 记录落成 Markdown；同幂等键重放返回同 ref。"""
        if record.target_system != self.source_system:
            raise AdapterUnavailableError(
                f"目标系统 {record.target_system} 与本适配器 {self.source_system} 不匹配"
            )
        approval = self._approval(record.approval_id)
        assert_writable(
            ActionLevel.R2_REVIEWED_WRITE, approval, global_kill_switch=self._kill()
        )
        body = record.request_body
        return self._write(
            record.idempotency_key,
            record.approval_id,
            doc_external_id=body["doc_external_id"],
            case_external_id=body["case_external_id"],
            markdown=body["markdown"],
        )

    # ── TestCaseRepositoryPort  canonical 方法 ──────────────────────────────
    async def write_approved_case(self, case: TestCase, approval_id: str) -> str:
        """端口契约方法：渲染与幂等键均复用 core 纯函数，与 outbox 路径同写入门。

        评审人/时间取自 Approval（outbox 路径则在审批时已烘进 markdown，
        保证「批准的字节即落盘的字节」）。
        """
        approval = self._approval(approval_id)
        assert_writable(
            ActionLevel.R2_REVIEWED_WRITE, approval, global_kill_switch=self._kill()
        )
        markdown = render_case_markdown(
            case,
            case.oracles,
            reviewed_by=approval.reviewer if approval and approval.reviewer else "unknown",
            decided_at=approval.decided_at if approval else None,
        )
        key = outbox_idempotency_key(case.urn, markdown)
        # URN external_id 形如 cases/x.md#TC-1：左段文档、右段用例
        external_body = parse_urn(case.urn)[2]
        doc_external_id = external_body.split("#", 1)[0]
        return self._write(
            key, approval_id,
            doc_external_id=doc_external_id,
            case_external_id=case.external_id,
            markdown=markdown,
        )

    def _write(
        self, idempotency_key: str, approval_id: str, *,
        doc_external_id: str, case_external_id: str, markdown: str,
    ) -> str:
        rel_path = writeback_rel_path(doc_external_id, case_external_id)
        external_ref = f"{self.source_system}:{rel_path}"
        manifest = self._load_manifest()
        existing = manifest["entries"].get(idempotency_key)
        if existing is not None:
            # 重放幂等：同键即同一次写，直接回同 ref，不碰文件系统内容
            return existing["external_ref"]

        target = self._resolve_inside_root(rel_path)
        self._atomic_write(target, markdown)
        manifest["entries"][idempotency_key] = {
            "external_ref": external_ref,
            "path": rel_path,
            "case_external_id": case_external_id,
            "doc_external_id": doc_external_id,
            "approval_id": approval_id,
            "written_at": datetime.now(timezone.utc).isoformat(),
        }
        self._atomic_write(
            self._manifest_path,
            json.dumps(manifest, ensure_ascii=False, indent=2),
        )
        return external_ref

    # ── 写根回读（探针 round-trip / 重新摄入）────────────────────────────────
    async def list_cases(self, selector: CaseSelector) -> list[TestCase]:
        """扫描写根全部 Markdown（manifest 非 .md 自然排除），按选择器过滤。"""
        cases: list[TestCase] = []
        for path in sorted(self.write_root.glob("**/*.md")):
            rel = path.relative_to(self.write_root).as_posix()
            parsed = parse_case_markdown(
                path.read_text(encoding="utf-8"), doc_external_id=rel)
            cases.extend(parsed.cases)
        if selector.test_ids:
            wanted = set(selector.test_ids)
            cases = [c for c in cases if c.external_id in wanted]
        if selector.tags:
            needed = set(selector.tags)
            cases = [c for c in cases if needed.issubset(set(c.tags))]
        return cases
