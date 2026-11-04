"""适配器装配工厂（依赖注入根，docs/08 FR-CFG-002 AC3）。

换 profile = 换适配器实现，工作流代码零改动。
支持的 type：stub / fs-docs / json / json-replay / recorded / localfs /
sqlite-fts / hybrid / sqlite / noop / langfuse / ollama（model）/ chroma（vectors）；
其余在 S2–S3 引入（git / xcode …）。

降级是运行期行为（向量通道故障→FTS-only），不是装配期选项：hybrid 必须配齐
model+vectors，否则 build_container 直接 ValueError（防止静默退化为单路而不自知）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from testpilot_core.config_schema import ProfileConfig

from testpilot_adapters.fs_docs import FileSystemDocsAdapter
from testpilot_adapters.json_events import JsonReplayEventSource
from testpilot_adapters.json_tickets import JsonTicketAdapter
from testpilot_adapters.localfs_artifact import LocalFsArtifactAdapter
from testpilot_adapters.recorded_model import RecordedModelGateway
from testpilot_adapters.recorded_runner import RecordedTestRunner
from testpilot_adapters.store import SqliteQueue, bootstrap_store
from testpilot_adapters.tracers import LangfuseTracer, NoopTracer
from testpilot_adapters.fts_search import FtsSearchAdapter


@dataclass
class Container:
    config: ProfileConfig
    base_dir: Path
    data_dir: Path
    adapters: dict[str, object] = field(default_factory=dict)

    def get(self, name: str):  # noqa: ANN202
        return self.adapters.get(name)


def _p(base_dir: Path, value: str) -> Path:
    p = Path(value).expanduser()
    return p if p.is_absolute() else (base_dir / p).resolve()


def build_container(config: ProfileConfig, base_dir: str | Path) -> Container:
    base_dir = Path(base_dir)
    data_dir = _p(base_dir, config.paths.get("data_dir", "store"))
    fixtures_dir = _p(base_dir, config.paths.get("fixtures_dir", "tests/fixtures"))
    a = config.adapters

    container = Container(config=config, base_dir=base_dir, data_dir=data_dir)
    store = bootstrap_store(data_dir)
    container.adapters["store"] = store
    container.adapters["queue"] = SqliteQueue(store.conn)

    # ── 文档知识源 ──
    docs_settings = a.docs.settings
    docs_roots = docs_settings.get("roots", [])
    if a.docs.type == "fs-docs":
        from testpilot_adapters.fs_docs import AuthorityRule
        from testpilot_core.enums import AuthorityLevel

        overrides = [
            AuthorityRule(glob=r["glob"], level=AuthorityLevel(r["level"]))
            for r in docs_settings.get("authority_overrides", [])
        ]
        # repo 内代码根支持 ${REPO_PATH} 占位（路径配置化，不写死机器路径）
        repo_path = Path(config.project.repo_path).expanduser()
        if not repo_path.is_absolute():
            repo_path = (base_dir / repo_path).resolve()

        def _resolve(root: str) -> Path:
            root = root.replace("${REPO_PATH}", str(repo_path))
            return _p(base_dir, root)

        container.adapters["docs"] = FileSystemDocsAdapter(
            [_resolve(r) for r in docs_roots] or [fixtures_dir / "docs"],
            code_roots=[_resolve(r) for r in docs_settings.get("code_roots", [])],
            excludes=docs_settings.get("excludes", []),
            authority_overrides=overrides,
        )
    elif a.docs.type != "stub":
        raise ValueError(f"未知 docs 适配器类型: {a.docs.type}")

    # ── 工单 ──
    if a.tickets.type == "json":
        ticket_fixtures = _p(fixtures_dir, "tickets")
        container.adapters["tickets"] = JsonTicketAdapter(
            read_dirs=[ticket_fixtures],
            write_dir=data_dir / "ticket-store",
            approval_provider=store.get_approval,
            kill_switch=lambda: not config.external_write_enabled,
        )
    elif a.tickets.type != "stub":
        raise ValueError(f"未知 tickets 适配器类型: {a.tickets.type}")

    # ── CI 事件 ──
    if a.ci_events.type == "json-replay":
        container.adapters["ci_events"] = JsonReplayEventSource(fixtures_dir / "events")
    elif a.ci_events.type != "stub":
        raise ValueError(f"未知 ci_events 适配器类型: {a.ci_events.type}")

    # ── 执行器 ──
    if a.runner.type == "recorded":
        container.adapters["runner"] = RecordedTestRunner(fixtures_dir / "runs")
    elif a.runner.type != "stub":
        raise ValueError(f"未知 runner 适配器类型: {a.runner.type}")

    # ── 制品 ──
    if a.artifact.type == "localfs":
        container.adapters["artifact"] = LocalFsArtifactAdapter(data_dir / "artifacts")
    elif a.artifact.type != "stub":
        raise ValueError(f"未知 artifact 适配器类型: {a.artifact.type}")

    # ── trace（先装配：模型网关构造时注入，span 随调用链）──
    if a.tracer.type == "noop":
        tracer = NoopTracer()
    elif a.tracer.type == "langfuse":
        import os

        tracer = LangfuseTracer(
            host=os.environ.get("LANGFUSE_HOST", "http://localhost:3030"),
            public_key=os.environ.get("LANGFUSE_PUBLIC_KEY", "pk-lf-local"),
            secret_key=os.environ.get("LANGFUSE_SECRET_KEY", "sk-lf-local"),
        )
    else:
        raise ValueError(f"未知 tracer 类型: {a.tracer.type}")
    container.adapters["tracer"] = tracer

    # ── 模型网关（S1.2：recorded / ollama）──
    if a.model.type == "recorded":
        model_gw = RecordedModelGateway(fixtures_dir / "model" / "responses.json")
        container.adapters["model"] = model_gw
    elif a.model.type == "ollama":
        from testpilot_adapters.ollama_model import OllamaModelGateway

        ms = a.model.settings
        model_gw = OllamaModelGateway(
            base_url=ms.get("base_url", "http://localhost:11434"),
            chat_model=ms.get("chat_model", "qwen2.5:7b"),
            embed_model=ms.get("embed_model", "shaw/dmeta-embedding-zh"),
            timeout=float(ms.get("timeout", 60.0)),
            max_retries=int(ms.get("max_retries", 2)),
            backoff_seconds=float(ms.get("backoff_seconds", 0.5)),
            tracer=tracer,
        )
        container.adapters["model"] = model_gw
    elif a.model.type != "stub":
        raise ValueError(f"未知 model 适配器类型: {a.model.type}")
    else:
        model_gw = None

    # ── 向量库（S1.2：stub=不注册，入库/检索自动跳过向量通道；chroma）──
    if a.vectors.type == "chroma":
        from testpilot_adapters.chroma_vector import ChromaVectorStore

        vs = a.vectors.settings
        vector_store = ChromaVectorStore(
            base_url=vs.get("base_url", "http://localhost:8000"),
            collection=vs.get("collection", "chunks"),
            timeout=float(vs.get("timeout", 30.0)),
            max_retries=int(vs.get("max_retries", 2)),
            backoff_seconds=float(vs.get("backoff_seconds", 0.5)),
        )
        container.adapters["vectors"] = vector_store
    elif a.vectors.type != "stub":
        raise ValueError(f"未知 vectors 适配器类型: {a.vectors.type}")
    else:
        vector_store = None

    # ── 检索（sqlite-fts 单路 / hybrid=FTS+向量 RRF 融合）──
    if a.search.type == "sqlite-fts":
        container.adapters["search"] = FtsSearchAdapter(store)
    elif a.search.type == "hybrid":
        from testpilot_adapters.hybrid_search import HybridSearchAdapter

        if model_gw is None or vector_store is None:
            # 装配期就拦住降级配置错误：hybrid 缺通道时运行期只会静默退化成
            # FTS-only，难以察觉；要纯 FTS 请显式用 sqlite-fts
            raise ValueError(
                "search=hybrid 要求 model 与 vectors 均为真实适配器"
                f"（当前 model={a.model.type}, vectors={a.vectors.type}）；"
                "纯 FTS 检索请显式配置 search: sqlite-fts"
            )
        ss = a.search.settings
        container.adapters["search"] = HybridSearchAdapter(
            store,
            model_gw,
            vector_store,
            project=config.project.name,
            channel_top_k=int(ss.get("channel_top_k", 20)),
            rrf_k=int(ss.get("rrf_k", 60)),
            quality_power=float(ss.get("quality_power", 1.0)),
            half_life_days=(
                float(ss["half_life_days"])
                if ss.get("half_life_days") is not None
                else None
            ),
        )
    elif a.search.type != "stub":
        raise ValueError(f"未知 search 适配器类型: {a.search.type}")

    # stub 位（S1+ 实现）：scm / cases / environment / logs
    for name in ("scm", "cases", "environment", "logs"):
        ref = getattr(a, name)
        if ref.type != "stub":
            raise ValueError(f"适配器 {name} 的类型 {ref.type} 将在后续里程碑实现")

    return container
