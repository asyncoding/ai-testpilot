"""配置加载与适配器装配工厂测试（docs/08 FR-CFG-001/002）。"""

from pathlib import Path

import pytest

from testpilot_adapters.chroma_vector import ChromaVectorStore
from testpilot_adapters.fs_docs import FileSystemDocsAdapter
from testpilot_adapters.fts_search import FtsSearchAdapter
from testpilot_adapters.hybrid_search import HybridSearchAdapter
from testpilot_adapters.json_events import JsonReplayEventSource
from testpilot_adapters.json_tickets import JsonTicketAdapter
from testpilot_adapters.git_repo import GitCodeRepository
from testpilot_adapters.ollama_model import OllamaModelGateway
from testpilot_adapters.recorded_git import RecordedGitRepository
from testpilot_adapters.recorded_model import RecordedModelGateway
from testpilot_adapters.recorded_runner import RecordedTestRunner
from testpilot_adapters.tracers import NoopTracer
from testpilot_orchestrator.config_loader import load_profile
from testpilot_orchestrator.factory import build_container

SUBSYSTEMS = Path(__file__).resolve().parents[1]
PROFILE = SUBSYSTEMS / "config" / "profiles" / "offline-demo.yaml"
IOS_PROFILE = SUBSYSTEMS / "config" / "profiles" / "ios-local-poc.yaml"


def test_load_profile_and_validate():
    config = load_profile(PROFILE)
    assert config.profile == "offline-demo"
    assert config.adapters.runner.type == "recorded"

    with pytest.raises(ValueError):
        load_profile(SUBSYSTEMS / "config" / "profiles" / "missing.yaml")


def test_container_wires_every_adapter(tmp_path, monkeypatch):
    config = load_profile(PROFILE)
    config.paths["data_dir"] = str(tmp_path / "data")
    config.paths["fixtures_dir"] = str(SUBSYSTEMS / "tests" / "fixtures")
    container = build_container(config, SUBSYSTEMS)
    try:
        assert isinstance(container.get("docs"), FileSystemDocsAdapter)
        assert isinstance(container.get("tickets"), JsonTicketAdapter)
        assert isinstance(container.get("runner"), RecordedTestRunner)
        assert isinstance(container.get("model"), RecordedModelGateway)
        assert isinstance(container.get("search"), FtsSearchAdapter)
        assert isinstance(container.get("ci_events"), JsonReplayEventSource)
        assert isinstance(container.get("tracer"), NoopTracer)
        # S1.3：离线 profile 装录制型 scm（不访问任何真实 git 仓）
        assert isinstance(container.get("scm"), RecordedGitRepository)
        # 离线 profile 不装向量适配器：入库跳过 embed、检索为 FTS-only
        assert container.get("vectors") is None
        # 文档/检索默认分支来自 project.default_branch（branch 一致性修复）
        assert container.get("docs").branch == "alarmkit-spike"
        assert container.get("search").default_branch == "alarmkit-spike"
    finally:
        container.get("store").close()


def test_ios_profile_wires_real_ollama_chroma_hybrid(tmp_path, monkeypatch):
    # 装配是纯构造（httpx 客户端懒连接），不起 ollama/chroma 也能验证接线
    monkeypatch.setenv("TESTPILOT_REPO_PATH", "/tmp/fake-repo")
    config = load_profile(IOS_PROFILE)
    config.paths["data_dir"] = str(tmp_path / "data")
    config.paths["fixtures_dir"] = str(SUBSYSTEMS / "tests" / "fixtures")
    container = build_container(config, SUBSYSTEMS)
    try:
        model = container.get("model")
        vectors = container.get("vectors")
        search = container.get("search")
        assert isinstance(model, OllamaModelGateway)
        assert isinstance(vectors, ChromaVectorStore)
        assert isinstance(search, HybridSearchAdapter)
        assert model.embed_model == "shaw/dmeta-embedding-zh"
        assert vectors.collection_name == "alarmkitspike-chunks"
        assert search.project == "AlarmKitSpike"
        assert search.rrf_k == 60 and search.channel_top_k == 20
        assert search.half_life_days is None  # profile 显式 null
        assert search.default_branch == "alarmkit-spike"
        # 模型网关注入同一 tracer（span 随调用链）
        assert model._tracer is container.get("tracer")  # noqa: SLF001
        # S1.3：真实 git 适配器注入检索；构造期必须纯懒（/tmp/fake-repo 不是 git 仓，
        # 一旦 __init__ 起 git 进程这里就会报错）
        scm = container.get("scm")
        assert isinstance(scm, GitCodeRepository)
        assert scm._root is None  # noqa: SLF001
        assert scm.timeout_s == 15.0
        assert search._verifier.scm is scm  # noqa: SLF001
        assert container.get("docs").branch == "alarmkit-spike"
    finally:
        container.get("store").close()


def test_hybrid_requires_real_model_and_vectors(tmp_path, monkeypatch):
    monkeypatch.setenv("TESTPILOT_REPO_PATH", "/tmp/fake-repo")
    config = load_profile(IOS_PROFILE)
    config.paths["data_dir"] = str(tmp_path / "data")
    # 配了 hybrid 却把 vectors 关成 stub：装配期直接报错，不允许静默退化成 FTS-only
    config.adapters.vectors = config.adapters.vectors.model_copy(
        update={"type": "stub", "settings": {}}
    )
    with pytest.raises(ValueError, match="hybrid 要求"):
        build_container(config, SUBSYSTEMS)


def test_unknown_vectors_type_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("TESTPILOT_REPO_PATH", "/tmp/fake-repo")
    config = load_profile(IOS_PROFILE)
    config.paths["data_dir"] = str(tmp_path / "data")
    config.adapters.vectors = config.adapters.vectors.model_copy(
        update={"type": "milvus"}
    )
    with pytest.raises(ValueError, match="未知 vectors"):
        build_container(config, SUBSYSTEMS)


async def test_offline_ingest_then_recorded_live_verify(tmp_path):
    # 端到端：offline profile 入库 → FtsSearchAdapter 经录制 scm 对夹具文档
    # 真实重切块 live-verify（path_map 映射，不访问任何 git 仓）→ verified
    config = load_profile(PROFILE)
    config.paths["data_dir"] = str(tmp_path / "data")
    config.paths["fixtures_dir"] = str(SUBSYSTEMS / "tests" / "fixtures")
    container = build_container(config, SUBSYSTEMS)
    try:
        from testpilot_workers.ingestion import run_ingestion

        report = await run_ingestion(container)
        assert report.status == "succeeded" and report.created >= 1
        assert report.head_commit  # 录制 scm 的固定 HEAD sha

        row = container.get("store").conn.execute(
            "SELECT urn, valid_from FROM chunks WHERE kind='text' LIMIT 1"
        ).fetchone()
        assert row is not None and row["valid_from"] == report.head_commit
        result = await container.get("search").live_verify(row["urn"], "")
        assert result["degraded"] is False and result["verified"] is True
        assert result["reason"] == "unchanged"
    finally:
        container.get("store").close()


def test_unknown_scm_type_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("TESTPILOT_REPO_PATH", "/tmp/fake-repo")
    config = load_profile(IOS_PROFILE)
    config.paths["data_dir"] = str(tmp_path / "data")
    config.adapters.scm = config.adapters.scm.model_copy(
        update={"type": "bitbucket-cloud"}
    )
    with pytest.raises(ValueError, match="未知 scm"):
        build_container(config, SUBSYSTEMS)


async def test_hybrid_search_defaults_branch_to_project_branch(tmp_path, monkeypatch):
    # branch=None 时走适配器 default_branch（非 main 分支过滤回归）
    monkeypatch.setenv("TESTPILOT_REPO_PATH", "/tmp/fake-repo")
    config = load_profile(IOS_PROFILE)
    config.paths["data_dir"] = str(tmp_path / "data")
    container = build_container(config, SUBSYSTEMS)
    try:
        search = container.get("search")
        captured = {}

        def spy(match_expr, like_terms, *, branch, acl_scope, kinds, limit):
            captured["branch"] = branch
            return []

        search.store.search_fts_matches = spy  # type: ignore[method-assign]

        class _FakeModel:
            embed_model = "fake"

            async def embed(self, texts):
                return [[0.0] * 8 for _ in texts]

        class _FakeVectors:
            async def query(self, vector, top_k, where=None):
                return []

        search.model = _FakeModel()
        search.vectors = _FakeVectors()
        result = await search.search("任意问题", limit=3)  # 不传 branch
        assert captured["branch"] == "alarmkit-spike"
        assert result.trace.fts_urns == []
    finally:
        container.get("store").close()


def test_service_url_env_overrides(monkeypatch):
    monkeypatch.setenv("TESTPILOT_REPO_PATH", "/tmp/fake-repo")
    monkeypatch.setenv("TESTPILOT_OLLAMA_URL", "http://ollama.internal:11434")
    monkeypatch.setenv("TESTPILOT_CHROMA_URL", "http://chroma.internal:8000")
    config = load_profile(IOS_PROFILE)
    assert config.adapters.model.settings["base_url"] == "http://ollama.internal:11434"
    assert config.adapters.vectors.settings["base_url"] == "http://chroma.internal:8000"


def test_repo_path_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("TESTPILOT_REPO_PATH", "/custom/repo")
    config = load_profile(PROFILE)
    assert config.project.repo_path == "/custom/repo"


def test_tracer_env_override(monkeypatch):
    # compose 起来后用 TESTPILOT_TRACER=langfuse 切换，不复制 profile 文件
    monkeypatch.setenv("TESTPILOT_TRACER", "langfuse")
    config = load_profile(PROFILE)
    assert config.adapters.tracer.type == "langfuse"


# ── E6 Swift 切块器 profile 接缝（FR-ING-002 AC3）────────────────────────────

def test_default_profile_has_no_tree_sitter_chunker(tmp_path):
    # 行为不变性守卫：默认 lexical，容器里根本不出现 swift_chunker 组件
    config = load_profile(PROFILE)
    config.paths["data_dir"] = str(tmp_path / "data")
    config.paths["fixtures_dir"] = str(SUBSYSTEMS / "tests" / "fixtures")
    container = build_container(config, SUBSYSTEMS)
    try:
        assert container.get("swift_chunker") is None
    finally:
        container.get("store").close()


def test_unknown_swift_chunker_setting_raises(tmp_path):
    config = load_profile(PROFILE)
    config.paths["data_dir"] = str(tmp_path / "data")
    config.paths["fixtures_dir"] = str(SUBSYSTEMS / "tests" / "fixtures")
    config.adapters.docs.settings["swift_chunker"] = "bogus"
    with pytest.raises(ValueError, match="swift_chunker"):
        build_container(config, SUBSYSTEMS)


def test_tree_sitter_setting_registers_optional_chunker(tmp_path):
    pytest.importorskip("tree_sitter")  # optional group 未装的环境跳过装配测试
    from testpilot_adapters.tree_sitter_chunker import TreeSwiftChunker

    config = load_profile(PROFILE)
    config.paths["data_dir"] = str(tmp_path / "data")
    config.paths["fixtures_dir"] = str(SUBSYSTEMS / "tests" / "fixtures")
    config.adapters.docs.settings["swift_chunker"] = "tree-sitter"
    container = build_container(config, SUBSYSTEMS)
    try:
        assert isinstance(container.get("swift_chunker"), TreeSwiftChunker)
    finally:
        container.get("store").close()
