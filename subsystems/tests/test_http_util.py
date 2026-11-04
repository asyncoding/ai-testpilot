"""loopback 代理绕过约定（S1.2 实测：系统代理把 localhost 请求送进代理 → 502）。"""

from __future__ import annotations

from testpilot_adapters.http_util import trust_env_for


def test_loopback_hosts_bypass_env_proxy():
    assert trust_env_for("http://localhost:11434") is False
    assert trust_env_for("http://127.0.0.1:8000/api/v2") is False
    assert trust_env_for("http://[::1]:8000") is False


def test_remote_hosts_keep_env_proxy():
    assert trust_env_for("http://ollama.internal:11434") is True
    assert trust_env_for("https://chroma.example.com:8000") is True


def test_malformed_url_fails_safe_to_default():
    assert trust_env_for("not-a-url") is True
