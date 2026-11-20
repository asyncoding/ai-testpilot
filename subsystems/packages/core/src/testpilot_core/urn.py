"""统一资源标识（docs/01 §4）。

外部系统 ID 不作为核心主键，统一包装为：
    urn:testpilot:<source_system>:<resource_type>:<external_id>@<version>
"""

from __future__ import annotations


def make_urn(source_system: str, resource_type: str, external_id: str, version: str | None = None) -> str:
    base = f"urn:testpilot:{source_system}:{resource_type}:{external_id}"
    return f"{base}@{version}" if version else base


def parse_urn(urn: str) -> tuple[str, str, str, str | None]:
    """返回 (source_system, resource_type, external_id, version)。"""
    prefix = "urn:testpilot:"
    if not urn.startswith(prefix):
        raise ValueError(f"非法 URN（缺少 {prefix} 前缀）: {urn}")
    body = urn[len(prefix) :]
    head, _, version = body.partition("@")
    parts = head.split(":", 2)
    if len(parts) != 3 or not all(parts):
        raise ValueError(f"非法 URN（应为 system:type:id）: {urn}")
    return parts[0], parts[1], parts[2], version or None
