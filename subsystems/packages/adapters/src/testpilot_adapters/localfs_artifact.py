"""本地文件系统制品适配器（ArtifactStorePort）。

生产替代：S3/MinIO/Artifactory（docs/04 §4.2）。大文件不进向量库，只存 URI+hash。
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import Any

from testpilot_core.urn import make_urn


class LocalFsArtifactAdapter:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    async def put(self, local_path: str, metadata: dict[str, Any]) -> str:
        src = Path(local_path)
        digest = hashlib.sha256(src.read_bytes()).hexdigest()
        dest = self.root / f"{digest[:12]}-{src.name}"
        if not dest.exists():
            shutil.copy2(src, dest)
        return make_urn("localfs-artifact", metadata.get("type", "other"), dest.name, digest[:12])

    async def resolve_uri(self, urn: str) -> str:
        from testpilot_core.urn import parse_urn

        _, _, external_id, _ = parse_urn(urn)
        path = self.root / external_id
        if not path.exists():
            raise FileNotFoundError(f"制品不存在: {path}")
        return path.resolve().as_uri()
