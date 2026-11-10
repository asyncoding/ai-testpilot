"""适配器装配工厂（依赖注入根，docs/08 FR-CFG-002 AC3）。

换 profile = 换适配器实现，工作流代码零改动。
支持的 type：stub / fs-docs / json / json-replay / recorded / junit-xml / xcode（S3.2）/
localfs / sqlite-fts / hybrid / sqlite / noop / langfuse / ollama（model）/
chroma（vectors）/ git 与 recorded（scm，S1.3）/ ios-simulator（environment，S3.2）；
logs 仍为 stub 位，S3.3 随 xcresulttool / os_log 落地。

降级是运行期行为（向量通道故障→FTS-only），不是装配期选项：hybrid 必须配齐
model+vectors，否则 build_container 直接 ValueError（防止静默退化为单路而不自知）。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

from testpilot_core.config_schema import ProfileConfig
from testpilot_core.models import RepoRef

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
class WritePolicyState:
    """运行期外部写策略（FR-WR-001 一键 kill switch）。

    profile 的 ``external_write_enabled`` 是硬边界：profile 关着时运行期永远
    不能越权打开（API 层只允许收紧）；profile 开着时可用 ``runtime_disabled``
    一键停用/恢复。适配器的 kill_switch 闭包读本对象，翻转对同进程所有适配器
    立即生效，无需重新装配。
    """

    profile_enabled: bool
    runtime_disabled: bool = False

    @property
    def writes_enabled(self) -> bool:
        return self.profile_enabled and not self.runtime_disabled


@dataclass
class Container:
    config: ProfileConfig
    base_dir: Path
    data_dir: Path
    adapters: dict[str, object] = field(default_factory=dict)
    # app lifespan 安排的模型预热后台任务（仅 ollama profile 非 None），关停时取消
    warmup_task: asyncio.Task | None = None
    # S2.5：运行期写策略状态，审批页 kill switch 直接翻转本对象
    write_policy: WritePolicyState | None = None

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
    container.write_policy = WritePolicyState(
        profile_enabled=config.external_write_enabled)
    store = bootstrap_store(data_dir)
    container.adapters["store"] = store
    container.adapters["queue"] = SqliteQueue(store.conn)

    # ── 被测仓库引用（路径配置化，禁止写死机器绝对路径）──
    repo_path = Path(config.project.repo_path).expanduser()
    if not repo_path.is_absolute():
        repo_path = (base_dir / repo_path).resolve()
    repo_ref = RepoRef(
        project=config.project.name,
        repo_path=str(repo_path),
        default_branch=config.project.default_branch,
        tech_stack=config.project.tech_stack,
    )
    container.adapters["repo"] = repo_ref

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

        def _resolve(root: str) -> Path:
            root = root.replace("${REPO_PATH}", str(repo_path))
            return _p(base_dir, root)

        container.adapters["docs"] = FileSystemDocsAdapter(
            [_resolve(r) for r in docs_roots] or [fixtures_dir / "docs"],
            code_roots=[_resolve(r) for r in docs_settings.get("code_roots", [])],
            excludes=docs_settings.get("excludes", []),
            authority_overrides=overrides,
            branch=config.project.default_branch,
        )
    elif a.docs.type != "stub":
        raise ValueError(f"未知 docs 适配器类型: {a.docs.type}")

    # ── Swift 切块器选择（E6 FR-ING-002 AC3 选型复核）──
    # 默认 lexical（core 零依赖词法器，行为不变）；tree-sitter 是 adapters optional
    # dependency，缺包时 TreeSwiftChunker 构造抛 AdapterUnavailableError——显式配置了
    # 不可用能力应在装配期响亮失败，单文件解析失败的回退在 ingestion 内处理。
    swift_chunker_kind = docs_settings.get("swift_chunker", "lexical")
    if swift_chunker_kind == "tree-sitter":
        from testpilot_adapters.tree_sitter_chunker import TreeSwiftChunker

        container.adapters["swift_chunker"] = TreeSwiftChunker()
    elif swift_chunker_kind != "lexical":
        raise ValueError(f"未知 swift_chunker 配置: {swift_chunker_kind}")

    # ── 用例文档连接器（S2.1 FR-CON-003：fs-cases 只读扫描 + core 解析）──
    if a.cases.type == "fs-cases":
        from testpilot_adapters.case_docs import CaseDocumentAdapter

        cs = a.cases.settings
        case_roots = [_resolve(r) for r in cs.get("roots", [])] or [fixtures_dir / "cases"]
        container.adapters["cases"] = CaseDocumentAdapter(
            case_roots,
            excludes=cs.get("excludes", []),
            branch=config.project.default_branch,
        )
    elif a.cases.type != "stub":
        raise ValueError(f"未知 cases 适配器类型: {a.cases.type}")

    # ── 需求文档连接器（S2.2 FR-TD-001：fs-reqs 只读扫描 + core 骨架抽取）──
    if a.requirements.type == "fs-reqs":
        from testpilot_adapters.requirement_docs import RequirementDocumentAdapter

        rs = a.requirements.settings
        req_roots = [_resolve(r) for r in rs.get("roots", [])] or [
            fixtures_dir / "requirements"
        ]
        container.adapters["requirements"] = RequirementDocumentAdapter(
            req_roots,
            excludes=rs.get("excludes", []),
            branch=config.project.default_branch,
        )
    elif a.requirements.type != "stub":
        raise ValueError(f"未知 requirements 适配器类型: {a.requirements.type}")

    # ── 工单 ──
    if a.tickets.type == "json":
        ticket_fixtures = _p(fixtures_dir, "tickets")
        container.adapters["tickets"] = JsonTicketAdapter(
            read_dirs=[ticket_fixtures],
            write_dir=data_dir / "ticket-store",
            approval_provider=store.get_approval,
            kill_switch=lambda: not container.write_policy.writes_enabled,
        )
    elif a.tickets.type != "stub":
        raise ValueError(f"未知 tickets 适配器类型: {a.tickets.type}")

    # ── 人审回写用例库（S2.5 FR-TD-004：写根独立于只读语料仓，默认 data_dir/case-writeback）──
    if a.case_repo.type == "markdown-fs":
        from testpilot_adapters.case_repo import MarkdownCaseRepository

        write_subdir = a.case_repo.settings.get("write_subdir", "case-writeback")
        container.adapters["case_repo"] = MarkdownCaseRepository(
            data_dir / write_subdir,
            approval_provider=store.get_approval,
            kill_switch=lambda: not container.write_policy.writes_enabled,
        )
    elif a.case_repo.type != "stub":
        raise ValueError(f"未知 case_repo 适配器类型: {a.case_repo.type}")

    # ── CI 事件 ──
    if a.ci_events.type == "json-replay":
        container.adapters["ci_events"] = JsonReplayEventSource(fixtures_dir / "events")
    elif a.ci_events.type != "stub":
        raise ValueError(f"未知 ci_events 适配器类型: {a.ci_events.type}")

    # ── 执行器 ──
    if a.runner.type == "recorded":
        container.adapters["runner"] = RecordedTestRunner(fixtures_dir / "runs")
    elif a.runner.type == "junit-xml":
        # S3.1 FR-EX-001：真实 Surefire XML → 语言中立 TestRun（与 xcode 同模型）
        from testpilot_adapters.junit_runner import JUnitXmlRunner

        rs = a.runner.settings
        reports_dir = _p(fixtures_dir, rs.get("reports_dir", "runs/surefire"))
        container.adapters["runner"] = JUnitXmlRunner(
            reports_dir,
            provider=rs.get("provider", "junit-local"),
            env_id=rs.get("env_id", "local-jvm"),
            capabilities=rs.get("capabilities", []),
            version=rs.get("version", ""),
        )
    elif a.runner.type == "xcode":
        # S3.2 FR-EX-003：真实 xcodebuild，进程出口唯一走 SubprocessExecutor
        # （argv 列表 create_subprocess_exec，永不 shell）；result parser 是
        # S3.3 FR-EX-005 的 xcresulttool 接缝，当前注入 fail-closed 占位。
        from testpilot_adapters.xcode_runner import (
            XcodeTestRunner,
            unavailable_xcresult_parser,
        )

        rs = a.runner.settings
        container.adapters["runner"] = XcodeTestRunner(
            workspace=rs["workspace"],
            scheme=rs["scheme"],
            repo_path=repo_path,
            result_bundle_dir=_p(
                base_dir, rs.get("result_bundle_dir", "store/xcresult")),
            result_parser=unavailable_xcresult_parser,
        )
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
            keep_alive=str(ms.get("keep_alive", "30m")),
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

    # ── SCM（S1.3：git 真实 / recorded 离线 / stub 不装配；纯懒构造不起 git 进程）──
    if a.scm.type == "git":
        from testpilot_adapters.git_repo import GitCodeRepository

        ss = a.scm.settings
        container.adapters["scm"] = GitCodeRepository(
            repo_ref,
            git_path=ss.get("git_path", "git"),
            timeout_s=float(ss.get("timeout_s", 15.0)),
        )
    elif a.scm.type == "recorded":
        from testpilot_adapters.recorded_git import RecordedGitRepository

        ss = a.scm.settings
        git_fixtures = _p(fixtures_dir, ss.get("fixtures_dir", "git"))
        container.adapters["scm"] = RecordedGitRepository(
            repo_ref, git_fixtures, path_map=ss.get("path_map"),
        )
    elif a.scm.type != "stub":
        raise ValueError(f"未知 scm 适配器类型: {a.scm.type}")
    scm = container.get("scm")  # stub 时为 None：live-verify 以 degraded 返回并留痕

    # ── 检索（sqlite-fts 单路 / hybrid=FTS+向量 RRF 融合）──
    if a.search.type == "sqlite-fts":
        container.adapters["search"] = FtsSearchAdapter(
            store, project=config.project.name, scm=scm, repo=repo_ref,
            tracer=tracer, default_branch=config.project.default_branch,
        )
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
            scm=scm,
            repo=repo_ref,
            tracer=tracer,
            default_branch=config.project.default_branch,
        )
    elif a.search.type != "stub":
        raise ValueError(f"未知 search 适配器类型: {a.search.type}")

    # ── 环境（S3.2 FR-EX-004：ios-simulator 模拟器池；stub=不装配，
    #    执行服务见不到 environment 时走旧路径，行为与 S3.1 完全一致）──
    if a.environment.type == "ios-simulator":
        from testpilot_adapters.ios_simulator_env import (
            SimctlEnvironment,
            SimulatorDevice,
        )

        es = a.environment.settings
        devices = [SimulatorDevice.model_validate(d) for d in es.get("devices", [])]
        # 空清单在构造期即 AdapterUnavailableError，不让坏 profile 拖到运行期
        container.adapters["environment"] = SimctlEnvironment(
            devices,
            lease_timeout_s=int(es.get("lease_timeout_s", 300)),
        )
    elif a.environment.type != "stub":
        raise ValueError(f"未知 environment 适配器类型: {a.environment.type}")

    # logs 仍是 stub 位（S3.3 随 xcresulttool / os_log 一起落地）
    if a.logs.type != "stub":
        raise ValueError(f"适配器 logs 的类型 {a.logs.type} 将在后续里程碑实现")

    return container
