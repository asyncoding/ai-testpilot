"""profile 配置加载（docs/08 FR-CFG-001）。

- YAML 为基线；机器相关路径允许用同名环境变量覆盖（TESTPILOT_REPO_PATH 等）；
- 不写死任何机器绝对路径；
- 启动时校验必填项，缺失给明确错误。
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

from testpilot_core.config_schema import ProfileConfig

# 路径支持：(section, key)；嵌套适配器支持：(section, adapter, key)
_ENV_OVERRIDES = {
    ("project", "repo_path"): "TESTPILOT_REPO_PATH",
    ("adapters", "tracer", "type"): "TESTPILOT_TRACER",
    # 本机服务端口/远端地址不写死在 profile 里，多机协作时用环境变量覆盖
    ("adapters", "model", "settings", "base_url"): "TESTPILOT_OLLAMA_URL",
    ("adapters", "vectors", "settings", "base_url"): "TESTPILOT_CHROMA_URL",
}


def load_profile(path: str | Path) -> ProfileConfig:
    path = Path(path)
    if not path.exists():
        # 配置类错误统一抛 ValueError，调用方只需一种异常类型
        raise ValueError(f"profile 配置不存在: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"profile 配置格式错误: {path}")

    for path_tuple, env_name in _ENV_OVERRIDES.items():
        if env_name not in os.environ:
            continue
        node = raw
        for key in path_tuple[:-1]:
            node = node.setdefault(key, {})
        node[path_tuple[-1]] = os.environ[env_name]

    config = ProfileConfig.model_validate(raw)
    _validate(config)
    return config


def _validate(config: ProfileConfig) -> None:
    missing = []
    if not config.project.name:
        missing.append("project.name")
    if not config.project.repo_path:
        missing.append("project.repo_path")
    if missing:
        raise ValueError(f"profile 缺少必填配置: {', '.join(missing)}")
