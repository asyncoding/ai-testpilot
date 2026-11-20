"""混合会话文件态存储（docs/01 §6，H3）。

零 DDL：每个会话一个 JSON（``hybrid-sessions/hsess-<12hex>.json``），信封里
同时装计划载荷与 SessionCheckpoint 游标。编排器每推进一步就整体原子覆写，
进程被杀后重启只凭文件即可从最后一个检查点续跑，不依赖任何内存队列。

与 FileGateStore 同纪律：tmp + os.replace 原子替换；session_id 闭集正则
拒绝路径穿越；生产替代是关系库 session 表，POC 保持 schema v15 零变更。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from testpilot_core.hybrid_plan import HybridSessionRecord

_SAFE_SESSION_ID = re.compile(r"^hsess-[0-9a-f]{12}$")


class SessionIdUnsafeError(ValueError):
    """session_id 不匹配 hsess-<12hex> 闭集：拒绝一切路径拼接，防穿越。"""


class FileHybridSessionStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root) / "hybrid-sessions"
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, session_id: str) -> Path:
        if not _SAFE_SESSION_ID.match(session_id):
            raise SessionIdUnsafeError(
                f"非法 session_id（拒绝路径拼接）: {session_id!r}")
        return self.root / f"{session_id}.json"

    def put(self, record: HybridSessionRecord) -> None:
        """原子覆写：读侧永远看到完整一版计划+游标，不会读到半截 JSON。"""
        path = self._path(record.session_id)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(record.model_dump(mode="json"),
                       ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, path)

    def get(self, session_id: str) -> HybridSessionRecord | None:
        path = self._path(session_id)
        if not path.exists():
            return None
        return HybridSessionRecord.model_validate_json(
            path.read_text(encoding="utf-8"))

    def list_all(self) -> list[HybridSessionRecord]:
        records = [
            HybridSessionRecord.model_validate_json(p.read_text(encoding="utf-8"))
            for p in self.root.glob("hsess-*.json")
        ]
        return sorted(records, key=lambda r: (
            r.checkpoint.updated_at, r.session_id))
