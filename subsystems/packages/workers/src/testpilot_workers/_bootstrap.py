"""三个 worker 共用的启动装配（S0 只验证配置/容器可在独立进程装起）。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from testpilot_orchestrator.config_loader import load_profile
from testpilot_orchestrator.factory import build_container


def bootstrap(worker_name: str):
    profile_path = os.environ.get(
        "TESTPILOT_PROFILE",
        str(Path(__file__).resolve().parents[4] / "config" / "profiles" / "offline-demo.yaml"),
    )
    config = load_profile(profile_path)
    container = build_container(config, Path(profile_path).parent.parent.parent)
    print(f"[{worker_name}] profile={config.profile} adapters={sorted(container.adapters)}", file=sys.stderr)
    return container
