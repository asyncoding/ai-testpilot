"""Chroma 真容器联调（默认跳过；起容器后自动生效）。

启动方式（镜像走 docker.1ms.run，见 handoff S1.2 环境事实）：
    docker pull docker.1ms.run/chromadb/chroma:0.5.20
    docker tag docker.1ms.run/chromadb/chroma:0.5.20 chromadb/chroma:0.5.20
    docker compose -f deploy/docker-compose.yml up -d chroma

验证四接口真连通：heartbeat / collection create / upsert+query / delete。
用唯一 collection 名并在 teardown 删表，不污染常驻库。
"""

import os
import uuid
import time
import urllib.request

import pytest

from testpilot_adapters.chroma_vector import ChromaVectorStore
from testpilot_core.models import VectorItem

CHROMA_URL = os.environ.get("TESTPILOT_CHROMA_URL", "http://localhost:8000")

# 显式绕过系统代理：macOS 下 urllib 会读系统代理配置，localhost 请求被
# 代理拦截返回 502（httpx 不读系统代理，适配器本身不受影响，仅探测受影响）
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
try:
    with _opener.open(f"{CHROMA_URL}/api/v2/heartbeat", timeout=2) as resp:
        _CHROMA_UP = resp.status == 200
except OSError:
    _CHROMA_UP = False

pytestmark = pytest.mark.skipif(not _CHROMA_UP, reason="Chroma 容器未启动（见模块 docstring）")


@pytest.fixture
async def store():
    name = f"it-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    gw = ChromaVectorStore(base_url=CHROMA_URL, collection=name, timeout=10)
    yield gw
    # teardown：删 collection。实测 0.5.20 DELETE 收的是**名字**而非 UUID
    if gw._collection_id:  # noqa: SLF001
        client = await gw._get_client()  # noqa: SLF001
        await client.delete(f"{gw._coll_base}/{gw.collection_name}")  # noqa: SLF001
    await gw.aclose()


async def test_heartbeat(store):
    assert await store.healthcheck() is True


async def test_upsert_query_delete_roundtrip(store):
    await store.upsert([
        VectorItem(id="a", embedding=[1.0, 0.0, 0.0], text="物理键日志",
                   metadata={"kind": "text", "quality": 0.8, "branch": "main"}),
        VectorItem(id="b", embedding=[0.0, 1.0, 0.0], text="侧边键冷启动",
                   metadata={"kind": "text", "quality": 0.7, "branch": "main"}),
        VectorItem(id="c", embedding=[0.9, 0.1, 0.0], text="锁屏键",
                   metadata={"kind": "incident", "quality": 0.6, "branch": "main"}),
    ])

    hits = await store.query([1.0, 0.0, 0.0], top_k=3)
    assert [h.id for h in hits] == ["a", "c", "b"]
    assert hits[0].metadata_["kind"] == "text"

    # metadata 过滤
    incidents = await store.query([1.0, 0.0, 0.0], 3, where={"kind": "incident"})
    assert [h.id for h in incidents] == ["c"]

    await store.delete(["a"])
    after = await store.query([1.0, 0.0, 0.0], 3)
    assert "a" not in [h.id for h in after]


async def test_upsert_is_idempotent_by_urn(store):
    item = VectorItem(id="x", embedding=[1.0, 0.0], metadata={"v": 1})
    await store.upsert([item])
    await store.upsert([item.model_copy(update={"metadata_": {"v": 2}})])
    hits = await store.query([1.0, 0.0], 1)
    assert len(hits) == 1 and hits[0].id == "x" and hits[0].metadata_["v"] == 2
