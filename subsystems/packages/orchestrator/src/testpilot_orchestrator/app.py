"""FastAPI 应用装配（S0 /health；S1.5 起挂载 /api/qa/* 与静态前端）。

生命周期：lifespan 内 build_qa_service（AsyncSqliteSaver 只能在 running loop 里建），
关停顺序固定为 QaService.aclose()（收口 checkpoint 连接）→ store.close()。
前端为 Next.js 静态导出（web/out），存在才挂载到根路径；缺失只记日志，API 不受影响，
CI/无 node 环境照常启动。
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .config_loader import load_profile
from .factory import Container, build_container
from .qa_api import api_router, register_exception_handlers
from .qa_graph.service import build_qa_service

logger = logging.getLogger("testpilot.orchestrator.app")

# subsystems/web/out（app.py 位于 subsystems/packages/orchestrator/src/testpilot_orchestrator/）
_DEFAULT_WEB_DIST = Path(__file__).resolve().parents[4] / "web" / "out"


def create_app(
    profile_path: str | None = None,
    *,
    container: Container | None = None,
    web_dist: str | Path | None = None,
) -> FastAPI:
    """创建应用。

    container 注入接缝供测试使用（tmp data_dir + 预灌数据，不起端口、不读环境 profile）；
    web_dist 同理，默认指向 subsystems/web/out。
    """
    profile_path = profile_path or os.environ.get(
        "TESTPILOT_PROFILE",
        str(Path(__file__).resolve().parents[4] / "config" / "profiles" / "offline-demo.yaml"),
    )
    web_dist = Path(web_dist) if web_dist is not None else _DEFAULT_WEB_DIST

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if container is not None:
            active_container = container
        else:
            config = load_profile(profile_path)
            active_container = build_container(
                config, Path(profile_path).parent.parent.parent
            )
        app.state.container = active_container
        # sqlite saver 依赖 running loop，只能在 lifespan 内异步启动
        app.state.qa = await build_qa_service(active_container)
        try:
            yield
        finally:
            # 先收口图的 checkpoint 连接，再关账本库，顺序不可反
            await app.state.qa.aclose()
            store = active_container.get("store")
            if store is not None:
                store.close()

    app = FastAPI(title="TestPilot Orchestrator", version="0.1.0", lifespan=lifespan)
    register_exception_handlers(app)
    app.include_router(api_router)

    @app.get("/health")
    async def health() -> dict:
        active_container: Container = app.state.container
        adapters = {
            name: type(impl).__name__ if impl is not None else None
            for name, impl in active_container.adapters.items()
        }
        return {
            "status": "ok",
            "profile": active_container.config.profile,
            "project": active_container.config.project.name,
            "external_write_enabled": active_container.config.external_write_enabled,
            "adapters": adapters,
        }

    # 静态前端最后挂载：路由按注册顺序匹配，/health 与 /api/* 优先于根路径兜底
    if web_dist.is_dir():
        app.mount("/", StaticFiles(directory=str(web_dist), html=True), name="web")
    else:
        # 不阻断启动：无前端构建产物时仍是完整 API 服务（verify_s0 A8 依赖此行为）
        logger.warning(
            "web/out 不存在（%s），跳过静态前端挂载，仅提供 /api 与 /health；"
            "构建前端见 scripts/build_web.sh",
            web_dist,
        )

    return app


app = create_app()
