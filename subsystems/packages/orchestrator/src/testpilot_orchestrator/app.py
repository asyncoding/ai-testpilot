"""FastAPI 应用骨架（S0：/health 与适配器装配自检；工作流端点 S1 起进入）。"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from .config_loader import load_profile
from .factory import Container, build_container


def create_app(profile_path: str | None = None) -> FastAPI:
    profile_path = profile_path or os.environ.get(
        "TESTPILOT_PROFILE", str(Path(__file__).resolve().parents[4] / "config" / "profiles" / "offline-demo.yaml")
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        config = load_profile(profile_path)
        container: Container = build_container(config, Path(profile_path).parent.parent.parent)
        app.state.container = container
        yield
        store = container.get("store")
        if store is not None:
            store.close()

    app = FastAPI(title="TestPilot Orchestrator", version="0.1.0", lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict:
        container: Container = app.state.container
        adapters = {
            name: type(impl).__name__ if impl is not None else None
            for name, impl in container.adapters.items()
        }
        return {
            "status": "ok",
            "profile": container.config.profile,
            "project": container.config.project.name,
            "external_write_enabled": container.config.external_write_enabled,
            "adapters": adapters,
        }

    return app


app = create_app()
