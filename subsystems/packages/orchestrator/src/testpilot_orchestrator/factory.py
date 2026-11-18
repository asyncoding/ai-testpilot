"""适配器装配工厂（依赖注入根，docs/08 FR-CFG-002 AC3）。

换 profile = 换适配器实现，工作流代码零改动。
支持的 type：stub / fs-docs / json / json-replay / recorded / junit-xml / xcode（S3.2，
S3.3 起接 xcresulttool 真实 parser）/ localfs / sqlite-fts / hybrid / sqlite / noop /
langfuse / ollama（model）/ chroma（vectors）/ git 与 recorded（scm，S1.3）/
ios-simulator / ios-device（environment，S3.2 / 真机切片一）、
os-log / os-log-device（logs，S3.3 / 真机切片一）。

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

    # H2（docs/15 §7）：文件态人工闸门，profile 无关、零 DDL，始终装配；
    # 落 data_dir/gates/<gate_id>.json，不进 sqlite、不需要 schema 迁移。
    from testpilot_adapters.gate_store import FileGateStore

    container.adapters["gate_store"] = FileGateStore(data_dir)

    # H3（docs/15 §3）：DH AX 语义刺激适配器，构造期不起进程；真机/Device Hub
    # 缺席时调用才报 AdapterUnavailableError。CLI 随仓入库 scripts/。
    from testpilot_adapters.dh_ax import DhAxAdapter
    from testpilot_adapters.process_executor import SubprocessExecutor

    dh_cli = base_dir / "scripts" / "device_hub_ax_cli.swift"
    container.adapters["dh_ax"] = DhAxAdapter(SubprocessExecutor(), dh_cli)

    # H3（docs/15 §3/§4）：devicectl 控制面（截图/启动/按 PID 终止），截图落
    # data_dir/devicectl-work；同样构造期不起进程，rc!=0 由适配器响亮失败。
    from testpilot_adapters.devicectl_control import DevicectlControlAdapter

    container.adapters["devicectl"] = DevicectlControlAdapter(
        SubprocessExecutor(), data_dir / "devicectl-work")

    # H3（docs/15 §6）：混合会话文件态存储，零 DDL；游标+计划+账本同文件原子写。
    from testpilot_adapters.hybrid_session_store import FileHybridSessionStore

    container.adapters["hybrid_session_store"] = FileHybridSessionStore(data_dir)

    # H4（docs/15 §6.2）：xcodebuild 混合段子 run 接 execution_service——同一队列、
    # 同一 run 账本与五态状态机，不造第二套执行通道。端口提交后轮询账本到终态：
    # failed 抛 ChildRunFailedError 走步骤 failure_policy，error/timeout/等待超时
    # 抛 AdapterUnavailableError 子类转 blocked。runner=stub 时入队仍成功，由 worker
    # 侧既有契约 fail-loud，装配期不为 profile 分叉。懒导入保持 workers→orchestrator
    # 单向包依赖（与 attribution 同模式）。
    from testpilot_workers.hybrid_child_run import ExecutionServiceChildRun

    container.adapters["hybrid_child_run"] = ExecutionServiceChildRun(container)

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

    # 三类文档连接器（docs/cases/requirements）共用的 root 解析：
    # ${REPO_PATH} 占位替换 + 相对 base_dir 解析。提升到分支外，避免
    # docs=stub 时 cases/requirements 引用未定义名（2026-11-13 缺口修复）。
    def _resolve(root: str) -> Path:
        root = root.replace("${REPO_PATH}", str(repo_path))
        return _p(base_dir, root)

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
    # S4.4：PipelineFinished handler 解析 file://fixtures/... 相对 URI 的锚点
    container.adapters["fixtures_dir"] = fixtures_dir

    # ── 制品（先于执行器装配：xcode runner 的 xcresult parser 要注入制品端口）──
    if a.artifact.type == "localfs":
        artifact_store = LocalFsArtifactAdapter(data_dir / "artifacts")
        container.adapters["artifact"] = artifact_store
    elif a.artifact.type != "stub":
        raise ValueError(f"未知 artifact 适配器类型: {a.artifact.type}")
    else:
        artifact_store = None

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
        # （argv 列表 create_subprocess_exec，永不 shell）；S3.3 FR-EX-005 起
        # result parser 换成真实 xcresulttool 链路，附件经制品端口内容寻址。
        from testpilot_adapters.xcode_runner import XcodeTestRunner
        from testpilot_adapters.xcresult_tool import XcresultToolResultParser

        if artifact_store is None:
            raise ValueError(
                "runner=xcode 要求 artifact=localfs：xcresult 附件/原始 JSON "
                "必须经制品端口内容寻址落盘，不允许裸跑无制品根的 xcode 链路"
            )
        rs = a.runner.settings
        # 真机切片一：target=device 切真机目的地（platform=iOS,id=<硬件UDID>），
        # 默认 simulator 保持 E5 模拟器链路一字不变。
        target = rs.get("target", "simulator")
        if target == "device":
            destination_platform = "iOS"
            runner_provider = "ios-device"
            parser_uncovered: list[str] | None = []  # 真机物理可验，不标设备独占未覆盖
        elif target == "simulator":
            destination_platform = "iOS Simulator"
            runner_provider = "ios-simulator"
            parser_uncovered = None  # 解析器默认填三类设备独占未覆盖
        else:
            raise ValueError(
                f"未知 runner.xcode.target（白名单 simulator/device）: {target!r}"
            )
        result_parser = XcresultToolResultParser(
            artifact_store,
            work_dir=_p(
                base_dir, rs.get("xcresult_work_dir", "store/xcresult-work")),
            provider=runner_provider,
            uncovered_layers=parser_uncovered,
        )
        container.adapters["runner"] = XcodeTestRunner(
            workspace=rs["workspace"],
            scheme=rs["scheme"],
            repo_path=repo_path,
            result_bundle_dir=_p(
                base_dir, rs.get("result_bundle_dir", "store/xcresult")),
            result_parser=result_parser,
            destination_platform=destination_platform,
            provider=runner_provider,
        )
    elif a.runner.type != "stub":
        raise ValueError(f"未知 runner 适配器类型: {a.runner.type}")

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

    # ── 环境（S3.2 FR-EX-004：ios-simulator 模拟器池；切片一增 ios-device 真机池；
    #    stub=不装配，执行服务见不到 environment 时走旧路径，行为与 S3.1 完全一致）──
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
    elif a.environment.type == "ios-device":
        from testpilot_adapters.ios_device_env import (
            IosDeviceEnvironment,
            PhysicalDevice,
        )

        es = a.environment.settings
        devices = [PhysicalDevice.model_validate(d) for d in es.get("devices", [])]
        container.adapters["environment"] = IosDeviceEnvironment(
            devices,
            lease_timeout_s=int(es.get("lease_timeout_s", 900)),
        )
    elif a.environment.type != "stub":
        raise ValueError(f"未知 environment 适配器类型: {a.environment.type}")

    # ── 日志查询（S3.3 FR-EX-006：os_log 模拟器实现；切片一增 os-log-device
    #    真机两步采集；必须显式 udid，拒绝 booted/隐式目标）──
    if a.logs.type == "os-log":
        from testpilot_adapters.os_log import OsLogQueryAdapter

        ls = a.logs.settings
        udid = ls.get("udid", "")
        if not udid:
            raise ValueError(
                "logs=os-log 要求 settings.udid：查询条件须可复现，"
                "不接受 booted 隐式目标"
            )
        container.adapters["logs"] = OsLogQueryAdapter(str(udid))
    elif a.logs.type == "os-log-device":
        from testpilot_core.oslog_commands import COLLECT_MODES
        from testpilot_adapters.device_os_log import DeviceOsLogQueryAdapter

        ls = a.logs.settings
        udid = ls.get("udid", "")
        if not udid:
            raise ValueError(
                "logs=os-log-device 要求 settings.udid：真机采集须绑定硬件 UDID"
            )
        collect_mode = ls.get("collect_mode", "auto")
        if collect_mode not in COLLECT_MODES:
            raise ValueError(
                f"未知 logs.collect_mode（白名单 {sorted(COLLECT_MODES)}）: {collect_mode!r}"
            )
        archive_work_dir = _p(
            base_dir,
            ls.get("archive_work_dir", "store/device-logarchive-work"),
        )
        container.adapters["logs"] = DeviceOsLogQueryAdapter(
            str(udid),
            archive_work_dir,
            collect_mode=collect_mode,
            use_sudo=bool(ls.get("use_sudo", False)),
        )
    elif a.logs.type != "stub":
        raise ValueError(f"未知 logs 适配器类型: {a.logs.type}")

    # ── 失败归因（S3.5 FR-EX-007）：StoreBacked 证据提供者组合账本历史 + 已装配
    #    端口（logs/scm 可为 stub=None，缺谁降级谁，端口不可达回空证据），引擎对
    #    证据不足的失败一律 needs_human（AC4）；断言/工单来源 POC 期不注入。
    #    懒导入保持 workers→orchestrator 单向包依赖，不在模块顶层反向 import。──
    from testpilot_workers.attribution_service import AttributionService
    from testpilot_workers.evidence_provider import StoreBackedEvidenceProvider

    evidence_provider = StoreBackedEvidenceProvider(
        store,
        repo=container.get("repo"),
        logs=container.get("logs"),
        scm=scm,
        log_predicates=a.logs.settings.get("attribution_predicates", []),
        diff_base=a.scm.settings.get("diff_base"),
    )
    container.adapters["attribution"] = AttributionService(evidence_provider)

    return container
