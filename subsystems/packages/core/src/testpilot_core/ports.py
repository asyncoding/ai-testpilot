"""端口接口（docs/04 §6）。

工作流只依赖这些 Protocol；外部系统差异全部在适配器层消化。
端口方法统一异步，便于未来接网络服务；离线/本地适配器内部用同步实现包一层即可。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from .models import (
    CaseSelector,
    ChangeSet,
    CommentDraft,
    EventCheckpoint,
    EventEnvelope,
    Issue,
    IssueDraft,
    KnowledgeChunk,
    RepoRef,
    SourceDocument,
    SymbolSnapshot,
    TestCase,
    TestExecutionRequest,
    TestRun,
    VectorHit,
    VectorItem,
)


@runtime_checkable
class CodeRepositoryPort(Protocol):
    async def head_sha(self, repo: RepoRef) -> str:
        """解析默认分支 HEAD 的完整 sha（ingestion 写 valid_from 用）。"""
        ...

    async def get_change_set(self, repo: RepoRef, base: str, head: str) -> ChangeSet: ...

    async def get_symbol_at_commit(
        self, repo: RepoRef, symbol_id: str, commit: str
    ) -> SymbolSnapshot: ...

    async def read_file_at_commit(self, repo: RepoRef, path: str, commit: str) -> str: ...

    async def list_changes(self, checkpoint: EventCheckpoint) -> list[EventEnvelope]: ...


@runtime_checkable
class KnowledgeSourcePort(Protocol):
    async def list_documents(self) -> list[SourceDocument]: ...

    async def list_changes(self, checkpoint: EventCheckpoint) -> list[EventEnvelope]: ...

    async def read_document(self, urn: str) -> SourceDocument: ...


@runtime_checkable
class KnowledgeSearchPort(Protocol):
    async def search(
        self,
        query: str,
        *,
        project: str,
        branch: str = "main",
        acl_scope: str = "default",
        limit: int = 8,
    ) -> list[KnowledgeChunk]: ...

    async def live_verify(self, urn: str, commit: str) -> dict[str, Any]:
        """校验证据在指定版本是否仍成立（符号存在/hash 一致）。"""
        ...


@runtime_checkable
class TestCaseRepositoryPort(Protocol):
    async def list_cases(self, selector: CaseSelector) -> list[TestCase]: ...

    async def write_approved_case(self, case: TestCase, approval_id: str) -> str:
        """批准后的用例回写；返回 external_ref。必须幂等。"""
        ...


@runtime_checkable
class TicketPort(Protocol):
    async def search(self, query: str, *, project: str, limit: int = 10) -> list[Issue]: ...

    async def create(self, draft: IssueDraft, idempotency_key: str, approval_id: str) -> str: ...

    async def add_comment(
        self, issue_ref: str, comment: CommentDraft, idempotency_key: str, approval_id: str
    ) -> str: ...

    async def transition(
        self, issue_ref: str, transition: str, idempotency_key: str, approval_id: str
    ) -> None: ...


@runtime_checkable
class TestRunnerPort(Protocol):
    async def submit(self, request: TestExecutionRequest) -> str:
        """提交执行，返回 run_id。"""
        ...

    async def cancel(self, run_id: str) -> None: ...

    async def get_run(self, run_id: str) -> TestRun: ...


@runtime_checkable
class EnvironmentPort(Protocol):
    async def acquire(self, requirements) -> str:  # EnvironmentRequirements -> lease_id
        ...

    def env_id_for_lease(self, lease_id: str) -> str:
        """租约 → 具体环境标识（模拟器即 udid）；未知/已释放租约必须响亮失败。

        worker 拿到 lease_id 后据此把 udid 透传进 request.environment.labels，
        runner 只认 labels，不自己挑设备，保证"先租后跑、无租约不裸跑"。
        """
        ...

    async def health_check(self, env_id: str) -> bool: ...

    async def reset(self, env_id: str) -> None: ...

    async def release(self, lease_id: str) -> None: ...


@runtime_checkable
class ArtifactStorePort(Protocol):
    async def put(self, local_path: str, metadata: dict[str, Any]) -> str:
        """存入制品，返回 URN。"""
        ...

    async def resolve_uri(self, urn: str) -> str: ...


@runtime_checkable
class LogQueryPort(Protocol):
    async def query(
        self,
        *,
        run_id: str,
        predicate: str,
        start_time: str,
        end_time: str,
    ) -> dict[str, Any]:
        """返回 {entries: [...], query: 可复现查询条件}。"""
        ...


@runtime_checkable
class ModelGatewayPort(Protocol):
    async def chat(self, messages: list[dict[str, str]], **options: Any) -> dict[str, Any]:
        """返回 {text, model, tokens_in, tokens_out, degraded}。"""
        ...

    async def structured(self, messages: list[dict[str, str]], schema: dict[str, Any], **options: Any) -> dict[str, Any]:
        """结构化输出；失败按重试上限处理后显式降级。"""
        ...

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


@runtime_checkable
class VectorStorePort(Protocol):
    """切块向量索引（S1.2，Chroma REST 实现）；一个适配器绑一个 collection。"""

    async def healthcheck(self) -> bool: ...

    async def upsert(self, items: list[VectorItem]) -> None:
        """按 id（chunk URN）幂等写入；含同名 collection 自动 get-or-create。"""
        ...

    async def query(
        self,
        vector: list[float],
        top_k: int,
        where: dict[str, Any] | None = None,
    ) -> list[VectorHit]:
        """按距离升序返回；where 为引擎原生 metadata 过滤。"""
        ...

    async def delete(self, ids: list[str]) -> None:
        """按 id 删除（文档级联：ingest 删除文档时同步清理向量）。"""
        ...


@runtime_checkable
class QueuePort(Protocol):
    async def enqueue(self, job_type: str, payload: dict[str, Any], idempotency_key: str) -> str: ...

    async def dequeue(self, job_types: list[str]) -> dict[str, Any] | None: ...

    async def ack(self, job_id: str) -> None: ...

    async def fail(self, job_id: str, error: str) -> None: ...


@runtime_checkable
class EventSourcePort(Protocol):
    async def replay(self, source: str) -> list[EventEnvelope]:
        """从 fixture/webhook 存储重放事件，重复 event_id 幂等。"""
        ...


@runtime_checkable
class TracePort(Protocol):
    def span(self, name: str, *, run_id: str = "", metadata: dict[str, Any] | None = None):
        """返回上下文管理器；NoopTracer 为空操作。"""
        ...
