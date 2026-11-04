"""Chroma 向量库适配器（VectorStorePort，S1.2）。

httpx 直连 Chroma 0.5.x 服务端 REST v2，**不引 chromadb SDK**（避免 numpy/重依赖）：
- GET  /api/v2/heartbeat
- GET  /api/v2/tenants/{t}/databases/{db}/collections/{name}        （不存在返回 404）
- POST /api/v2/tenants/{t}/databases/{db}/collections               （get-or-create）
- POST .../collections/{id}/upsert | query | delete

文档 id 直接用 chunk URN（稳定幂等）；metadata 只允许标量（Chroma 约束），
写入前做白名单清洗。collection id 首次操作时懒加载并缓存。
传输故障/5xx 退避重试后抛 AdapterUnavailableError，由混合检索层降级为 FTS-only。

距离空间（实测踩坑，2026-11-04）：chromadb/chroma:0.5.20 thin-server 建表时
强制 L2——configuration_json（dict/字符串/带 _type）、configuration、
metadata.hnsw:space、v1 端点五种写法均被静默忽略（返回 space=l2）。
故默认在写入/查询两侧对向量做 L2 归一化：单位向量上 L2 距离序与 cosine 等价
（||a-b||² = 2−2cos），归一化必须双侧同时开启/关闭，否则距离序错乱。
"""

from __future__ import annotations

import asyncio
import math
from typing import Any, Awaitable, Callable

import httpx

from testpilot_adapters.http_util import trust_env_for
from testpilot_core.errors import AdapterUnavailableError
from testpilot_core.models import VectorHit, VectorItem

_RETRY_STATUS = {429, 500, 502, 503, 504}
# Chroma 单请求 embedding 条数有上限（默认约 5k），分批写稳妥且可观测
_UPSERT_BATCH = 500


def l2_normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vector))
    if norm == 0:
        return list(vector)
    return [x / norm for x in vector]


class ChromaVectorStore:
    def __init__(
        self,
        *,
        base_url: str = "http://localhost:8000",
        collection: str = "chunks",
        tenant: str = "default_tenant",
        database: str = "default_database",
        api_key: str | None = None,
        timeout: float = 30.0,
        max_retries: int = 2,
        backoff_seconds: float = 0.5,
        normalize_embeddings: bool = True,
        transport: httpx.AsyncBaseTransport | None = None,
        sleeper: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.collection_name = collection
        self._coll_base = (
            f"/api/v2/tenants/{tenant}/databases/{database}/collections"
        )
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._headers = headers
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_seconds = backoff_seconds
        self.normalize_embeddings = normalize_embeddings
        self._transport = transport
        self._sleeper = sleeper or asyncio.sleep
        self._client: httpx.AsyncClient | None = None
        self._collection_id: str | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout,
                transport=self._transport,
                headers=self._headers,
                # loopback 端点绕过 Clash 类系统代理（否则 localhost 被代理 502）
                trust_env=trust_env_for(self.base_url),
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def healthcheck(self) -> bool:
        try:
            client = await self._get_client()
            resp = await client.get("/api/v2/heartbeat")
            return resp.status_code == 200
        except httpx.HTTPError:
            return False

    async def _request(
        self, method: str, path: str, *, json_body: dict[str, Any] | None = None
    ) -> httpx.Response:
        client = await self._get_client()
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = await client.request(method, path, json=json_body)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = exc
                resp = None  # type: ignore[assignment]
            if resp is not None:
                if resp.status_code < 400:
                    return resp
                last_exc = AdapterUnavailableError(
                    f"Chroma {method} {path} HTTP {resp.status_code}: {resp.text[:200]}"
                )
                if resp.status_code not in _RETRY_STATUS:
                    raise last_exc
            if attempt < self.max_retries:
                await self._sleeper(self.backoff_seconds * (2**attempt))
        raise AdapterUnavailableError(f"Chroma 调用失败（{method} {path}）：{last_exc}")

    async def _ensure_collection(self) -> str:
        if self._collection_id:
            return self._collection_id
        path = f"{self._coll_base}/{self.collection_name}"
        client = await self._get_client()
        resp = await client.get(path)
        if resp.status_code == 200:
            self._collection_id = resp.json()["id"]
            return self._collection_id
        # 实测 chromadb/chroma:0.5.20：不存在时返回 400 InvalidCollection
        # （而非 REST 语义上的 404），两种都按"需建表"处理
        missing = resp.status_code == 404 or (
            resp.status_code == 400
            and resp.json().get("error") == "InvalidCollection"
        )
        if not missing:
            raise AdapterUnavailableError(
                f"Chroma 查询 collection 失败 HTTP {resp.status_code}: {resp.text[:200]}"
            )
        created = await self._request(
            "POST",
            self._coll_base,
            json_body={
                "name": self.collection_name,
                "configuration_json": None,
                "metadata": None,
            },
        )
        self._collection_id = created.json()["id"]
        return self._collection_id

    async def upsert(self, items: list[VectorItem]) -> None:
        if not items:
            return
        coll_id = await self._ensure_collection()
        for start in range(0, len(items), _UPSERT_BATCH):
            batch = items[start : start + _UPSERT_BATCH]
            await self._request(
                "POST",
                f"{self._coll_base}/{coll_id}/upsert",
                json_body={
                    "ids": [it.id for it in batch],
                    "embeddings": [
                        l2_normalize(it.embedding) if self.normalize_embeddings else it.embedding
                        for it in batch
                    ],
                    "documents": [it.text for it in batch] or None,
                    "metadatas": [_sanitize_metadata(it.metadata_) for it in batch],
                },
            )

    async def query(
        self,
        vector: list[float],
        top_k: int,
        where: dict[str, Any] | None = None,
    ) -> list[VectorHit]:
        coll_id = await self._ensure_collection()
        query_vector = l2_normalize(vector) if self.normalize_embeddings else vector
        resp = await self._request(
            "POST",
            f"{self._coll_base}/{coll_id}/query",
            json_body={
                "where": where,
                "where_document": None,
                "query_embeddings": [query_vector],
                "n_results": top_k,
                "include": ["metadatas", "distances"],
            },
        )
        data = resp.json()
        ids = data.get("ids", [[]])[0]
        distances = data.get("distances", [[]])[0]
        metadatas = data.get("metadatas", [[]])[0]
        return [
            VectorHit(id=hid, distance=float(dist), metadata=meta or {})
            for hid, dist, meta in zip(ids, distances, metadatas, strict=True)
        ]

    async def delete(self, ids: list[str]) -> None:
        if not ids:
            return
        coll_id = await self._ensure_collection()
        await self._request(
            "POST",
            f"{self._coll_base}/{coll_id}/delete",
            json_body={"ids": ids},
        )


def _sanitize_metadata(meta: dict[str, Any]) -> dict[str, Any]:
    """Chroma metadata 值只接受 str/int/float/bool；剔除 None 与非标量。

    content_hash/kind/branch/acl_scope/quality 等计划内字段全是标量；
    混进其他类型时静默丢弃（不抛异常阻断入库），保留键集合可追溯。
    """
    out: dict[str, Any] = {}
    for key, value in meta.items():
        if value is None:
            continue
        if isinstance(value, bool) or isinstance(value, (str, int, float)):
            out[key] = value
    return out
