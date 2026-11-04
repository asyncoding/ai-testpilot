"""ChromaVectorStore 契约（S1.2）：httpx MockTransport，不起真实容器。

覆盖：heartbeat、collection get-or-create（已存在/404 两条路径）、
upsert 载荷与分批/metadata 清洗、query 返回顺序与映射、delete、
503 退避重试、400 快速失败。真容器联调见 test_chroma_integration（标记跳过）。
"""

import json

import httpx
import pytest

from testpilot_adapters.chroma_vector import ChromaVectorStore, l2_normalize, _sanitize_metadata
from testpilot_core.errors import AdapterUnavailableError
from testpilot_core.models import VectorItem

COLL = "/api/v2/tenants/default_tenant/databases/default_database/collections"


class Router:
    """按 (method, path 前缀) 路由的假 Chroma；记录全部请求。"""

    def __init__(self, routes):
        self.routes = routes
        self.requests = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        for method, prefix, fn in self.routes:
            if request.method == method and request.url.path.startswith(prefix):
                return fn(request)
        return httpx.Response(404, json={"error": f"unrouted {request.method} {request.url.path}"})

    def bodies(self):
        return [json.loads(r.content) for r in self.requests if r.content]


def make_store(router, **kw):
    transport = httpx.MockTransport(router)
    # 单测默认关闭归一化，便于对载荷做精确断言；归一化另有专测
    kw.setdefault("normalize_embeddings", False)
    return ChromaVectorStore(transport=transport, **kw), router


def collection_routes(collection_exists: bool, coll_id="coll-1"):
    state = {"upserts": 0, "deleted": None}

    def get_coll(request):
        if collection_exists:
            return httpx.Response(200, json={"id": coll_id, "name": "chunks"})
        return httpx.Response(404, json={"error": "not found"})

    def create_coll(request):
        return httpx.Response(200, json={"id": coll_id, "name": "chunks"})

    def upsert(request):
        state["upserts"] += 1
        return httpx.Response(200, json={})

    def query(request):
        return httpx.Response(200, json={
            "ids": [["u2", "u1"]],
            "distances": [[0.11, 0.42]],
            "metadatas": [[{"kind": "text"}, {"kind": "code_symbol"}]],
        })

    def delete(request):
        state["deleted"] = json.loads(request.content)
        return httpx.Response(200, json={})

    # 顺序：具体子路径在前（startswith 前缀匹配会"吃掉"更短的 create 前缀）
    routes = [
        ("GET", f"{COLL}/chunks", get_coll),
        ("POST", f"{COLL}/{coll_id}/upsert", upsert),
        ("POST", f"{COLL}/{coll_id}/query", query),
        ("POST", f"{COLL}/{coll_id}/delete", delete),
        ("POST", COLL, create_coll),
    ]
    return routes, state


# ── healthcheck ───────────────────────────────────────────────────────────────

async def test_healthcheck_true_and_false():
    async def ok(request):
        return httpx.Response(200, json={})

    store, _ = make_store(Router([("GET", "/api/v2/heartbeat", ok)]))
    assert await store.healthcheck() is True

    dead = ChromaVectorStore(
        transport=httpx.MockTransport(lambda r: (_ for _ in ()).throw(httpx.ConnectError("x")))
    )
    assert await dead.healthcheck() is False


# ── collection get-or-create ─────────────────────────────────────────────────

async def test_upsert_uses_existing_collection():
    routes, state = collection_routes(collection_exists=True)
    store, router = make_store(Router(routes))
    await store.upsert([VectorItem(id="u1", embedding=[0.1, 0.2], text="正文",
                                   metadata={"kind": "text", "content_hash": "h1"})])
    paths = [r.url.path for r in router.requests]
    assert paths == [f"{COLL}/chunks", f"{COLL}/coll-1/upsert"]  # 没有 POST create
    body = router.bodies()[-1]
    assert body["ids"] == ["u1"] and body["embeddings"] == [[0.1, 0.2]]
    assert body["documents"] == ["正文"]
    assert body["metadatas"] == [{"kind": "text", "content_hash": "h1"}]


async def test_upsert_creates_collection_when_404():
    routes, state = collection_routes(collection_exists=False)
    store, router = make_store(Router(routes))
    await store.upsert([VectorItem(id="u1", embedding=[0.1])])
    create = [r for r in router.requests if r.method == "POST" and r.url.path == COLL]
    assert len(create) == 1
    # bodies() 跳过无 body 的 GET：第 0 个即 create 载荷
    assert router.bodies()[0] == {"name": "chunks", "configuration_json": None, "metadata": None}


async def test_400_invalid_collection_treated_as_missing():
    # 实测 0.5.20 对不存在的 collection 返回 400 InvalidCollection 而非 404
    def handler(request):
        if request.method == "GET":
            return httpx.Response(400, json={"error": "InvalidCollection",
                                            "message": "does not exist"})
        if request.url.path == COLL:
            return httpx.Response(200, json={"id": "new-coll", "name": "chunks"})
        return httpx.Response(200, json={})

    router = Router([("GET", f"{COLL}/chunks", handler),
                     ("POST", COLL, handler),
                     ("POST", f"{COLL}/new-coll/upsert", handler)])
    store, rtr = make_store(router)
    await store.upsert([VectorItem(id="u1", embedding=[0.1])])
    assert store._collection_id == "new-coll"  # noqa: SLF001


async def test_collection_id_cached_across_calls():
    routes, state = collection_routes(collection_exists=True)
    store, router = make_store(Router(routes))
    await store.upsert([VectorItem(id="u1", embedding=[0.1])])
    await store.delete(["u1"])
    gets = [r for r in router.requests if r.method == "GET" and r.url.path == f"{COLL}/chunks"]
    assert len(gets) == 1  # 第二次操作复用缓存 id


# ── upsert 分批 / metadata 清洗 ──────────────────────────────────────────────

async def test_upsert_batches_at_500():
    routes, state = collection_routes(collection_exists=True)
    store, router = make_store(Router(routes))
    items = [VectorItem(id=f"u{i}", embedding=[0.0, 1.0]) for i in range(1001)]
    await store.upsert(items)
    upsert_bodies = [
        json.loads(r.content)
        for r in router.requests
        if r.url.path.endswith("/upsert")
    ]
    assert [len(b["ids"]) for b in upsert_bodies] == [500, 500, 1]


async def test_sanitize_metadata_keeps_scalars_drops_none_and_nested():
    clean = _sanitize_metadata({
        "kind": "text", "quality": 0.8, "branch_idx": 3, "fresh": True,
        "none_field": None, "nested": {"a": 1}, "listed": [1, 2],
    })
    assert clean == {"kind": "text", "quality": 0.8, "branch_idx": 3, "fresh": True}


async def test_empty_upsert_and_delete_skip_http():
    routes, _ = collection_routes(collection_exists=True)
    store, router = make_store(Router(routes))
    await store.upsert([])
    await store.delete([])
    assert router.requests == []


async def test_l2_normalization_on_both_write_and_query():
    # 0.5.20 服务端强制 L2 空间：双侧归一化后与 cosine 序等价（见模块 docstring）
    routes, _ = collection_routes(collection_exists=True)
    store, router = make_store(Router(routes), normalize_embeddings=True)
    await store.upsert([VectorItem(id="u1", embedding=[3.0, 4.0])])  # 范数 5
    await store.query([0.0, 2.0], 2)
    upsert_body = [json.loads(r.content) for r in router.requests
                   if r.url.path.endswith("/upsert")][0]
    assert upsert_body["embeddings"][0] == pytest.approx([0.6, 0.8])
    query_body = [json.loads(r.content) for r in router.requests
                  if r.url.path.endswith("/query")][0]
    assert query_body["query_embeddings"][0] == pytest.approx([0.0, 1.0])
    assert l2_normalize([0.0, 0.0]) == [0.0, 0.0]  # 零向量不放大


# ── query / delete ────────────────────────────────────────────────────────────

async def test_query_returns_hits_in_response_order_with_distance():
    routes, _ = collection_routes(collection_exists=True)
    store, router = make_store(Router(routes))
    hits = await store.query([0.1, 0.2], top_k=5, where={"kind": "text"})
    assert [h.id for h in hits] == ["u2", "u1"]
    assert hits[0].distance == 0.11 and hits[0].metadata_ == {"kind": "text"}
    body = router.bodies()[-1]
    assert body["query_embeddings"] == [[0.1, 0.2]]
    assert body["n_results"] == 5 and body["where"] == {"kind": "text"}


async def test_delete_payload():
    routes, state = collection_routes(collection_exists=False)
    store, _ = make_store(Router(routes))
    await store.delete(["u1", "u2"])
    assert state["deleted"] == {"ids": ["u1", "u2"]}


# ── 重试 / 失败 ───────────────────────────────────────────────────────────────

async def test_create_collection_retries_503_with_backoff():
    # GET collection 立即 404；POST create 前两次 503、第三次成功；upsert 200
    waited: list[float] = []
    creates = {"n": 0}

    async def sleeper(s):
        waited.append(s)

    def handler(request):
        if request.method == "GET":
            return httpx.Response(404, json={})
        if request.url.path.endswith("/collections"):
            creates["n"] += 1
            if creates["n"] <= 2:
                return httpx.Response(503, json={"error": "busy"})
            return httpx.Response(200, json={"id": "coll-1", "name": "chunks"})
        return httpx.Response(200, json={})  # upsert

    store = ChromaVectorStore(
        transport=httpx.MockTransport(handler),
        sleeper=sleeper,
        max_retries=2,
        backoff_seconds=0.5,
    )
    await store.upsert([VectorItem(id="u1", embedding=[0.1])])
    assert creates["n"] == 3
    assert waited == [0.5, 1.0]


async def test_400_fails_fast():
    def bad(request):
        return httpx.Response(400, json={"error": "dim mismatch"})

    store = ChromaVectorStore(transport=httpx.MockTransport(bad))
    with pytest.raises(AdapterUnavailableError, match="HTTP 400"):
        await store.query([0.1], 5)
