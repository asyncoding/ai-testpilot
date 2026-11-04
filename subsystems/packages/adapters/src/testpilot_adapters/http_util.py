"""httpx 客户端构造约定（S1.2 实测踩坑）。

办公网/个人机常开 Clash 类系统代理（macOS SystemConfiguration），
httpx 与 urllib 都会继承该配置，把发往 localhost 的请求也送进代理，
实测表现为本机 Chroma/Ollama 端口直连 curl 200、经代理却 502 Bad Gateway。

约定：端点指向 loopback 时 trust_env=False（忽略系统代理直连）；
远端端点保持 trust_env=True，仍可走 env 里配置的公司代理。
"""

from __future__ import annotations

import httpx

_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


def trust_env_for(base_url: str) -> bool:
    """httpx.AsyncClient(trust_env=...) 取值：loopback 端点返回 False。"""
    try:
        host = httpx.URL(base_url).host
    except Exception:  # noqa: BLE001 - 畸形 URL 按默认安全策略（读 env）
        return True
    return host not in _LOOPBACK_HOSTS
