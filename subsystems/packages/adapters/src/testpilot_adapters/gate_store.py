"""文件态人工闸门存储（docs/15 §7，H2）。

零 DDL：每条闸门一个 JSON（``gates/<gate_id>.json``），单文件承载完整
生命周期（含响应裁决与续期痕迹）。gate_id 由 core 的 ``gate_id_for`` 哈希
确定，同会话同步骤重建幂等；写入走 tmp + os.replace 原子替换，进程被杀
只可能留下旧文件或 tmp 残片，不会出现半截 JSON。

生产替代：关系库 gate 表或工作流引擎人工任务；本仓 POC 保持 schema v15
零变更，不新建表。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from testpilot_core.hybrid_plan import GateRequest

_SAFE_GATE_ID = re.compile(r"^gate-[0-9a-f]{12}$")


class GateIdUnsafeError(ValueError):
    """gate_id 不匹配 gate-<12hex> 闭集：拒绝一切路径拼接，防穿越。"""


class FileGateStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root) / "gates"
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, gate_id: str) -> Path:
        if not _SAFE_GATE_ID.match(gate_id):
            raise GateIdUnsafeError(f"非法 gate_id（拒绝路径拼接）: {gate_id!r}")
        return self.root / f"{gate_id}.json"

    def put(self, gate: GateRequest) -> None:
        """原子覆写：同 gate_id 的状态迁移整体替换，读侧永远看到完整一版。"""
        path = self._path(gate.gate_id)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(gate.model_dump(mode="json"),
                       ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, path)

    def get(self, gate_id: str) -> GateRequest | None:
        path = self._path(gate_id)
        if not path.exists():
            return None
        return GateRequest.model_validate_json(
            path.read_text(encoding="utf-8"))

    def list_all(self) -> list[GateRequest]:
        """按创建时间、gate_id 双键稳定排序，前端队列顺序可钉测。"""
        gates = [
            GateRequest.model_validate_json(p.read_text(encoding="utf-8"))
            for p in self.root.glob("gate-*.json")
        ]
        return sorted(gates, key=lambda g: (g.created_at, g.gate_id))
