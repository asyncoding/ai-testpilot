"""适配器装配工厂（依赖注入根，docs/08 FR-CFG-002 AC3）。

换 profile = 换适配器实现，工作流代码零改动。
S0 支持的 type：stub / fs-docs / json / json-replay / recorded / localfs /
sqlite-fts / sqlite / noop / langfuse；其余在 S1–S3 引入（git / ollama / xcode …）。
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
    docs_roots = a.docs.settings.get("roots", [])
    if a.docs.type == "fs-docs":
        container.adapters["docs"] = FileSystemDocsAdapter(
            [_p(base_dir, r) for r in docs_roots] or [fixtures_dir / "docs"]
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

    # ── 检索 / 模型 / trace ──
    if a.search.type == "sqlite-fts":
        container.adapters["search"] = FtsSearchAdapter(store)
    elif a.search.type != "stub":
        raise ValueError(f"未知 search 适配器类型: {a.search.type}")

    if a.model.type == "recorded":
        container.adapters["model"] = RecordedModelGateway(fixtures_dir / "model" / "responses.json")
    elif a.model.type != "stub":
        raise ValueError(f"未知 model 适配器类型: {a.model.type}")

    if a.tracer.type == "noop":
        container.adapters["tracer"] = NoopTracer()
    elif a.tracer.type == "langfuse":
        import os

        container.adapters["tracer"] = LangfuseTracer(
            host=os.environ.get("LANGFUSE_HOST", "http://localhost:3030"),
            public_key=os.environ.get("LANGFUSE_PUBLIC_KEY", "pk-lf-local"),
            secret_key=os.environ.get("LANGFUSE_SECRET_KEY", "sk-lf-local"),
        )
    else:
        raise ValueError(f"未知 tracer 类型: {a.tracer.type}")

    # stub 位（S1+ 实现）：scm / cases / environment / logs
    for name in ("scm", "cases", "environment", "logs"):
        ref = getattr(a, name)
        if ref.type != "stub":
            raise ValueError(f"适配器 {name} 的类型 {ref.type} 将在后续里程碑实现")

    return container
