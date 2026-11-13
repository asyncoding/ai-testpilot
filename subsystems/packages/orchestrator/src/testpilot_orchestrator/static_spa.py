"""SPA 静态托管：Next.js 扁平静态导出的无扩展名 URL 回退。

``output: "export"`` 默认产出 ``approvals.html`` 这类扁平文件，而不是
``approvals/index.html`` 目录索引；Starlette ``StaticFiles(html=True)``
只认目录索引，干净 URL ``/approvals`` 会落到 404.html（实测 2026-11-13，
/approvals、/knowledge、/runs 全部 404，仅 / 与 *.html 可达）。

本类在父类解析前先尝试同路径 ``.html``；命中即直接返回，未命中完全交还
父类（目录索引、404.html、Allow 头等行为一字不变）。路径安全不另起炉灶：
候选路径仍走父类 ``lookup_path`` 的 commonpath 防穿越校验，目录外一律 miss。
"""

from __future__ import annotations

import stat

import anyio
from starlette.staticfiles import StaticFiles


class SpaStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope):
        # 已带 .html（或其他扩展名）的请求不回退，保持父类原语义
        if not path.endswith(".html"):
            candidate = f"{path.rstrip('/')}.html"
            if candidate != ".html":  # 根路径 "" 不抢，父类负责 index.html
                full_path, stat_result = await anyio.to_thread.run_sync(
                    self.lookup_path, candidate
                )
                if stat_result is not None and stat.S_ISREG(stat_result.st_mode):
                    return self.file_response(full_path, stat_result, scope)
        return await super().get_response(path, scope)
