"""配置加载与适配器装配工厂测试（docs/08 FR-CFG-001/002）。"""

from pathlib import Path

import pytest

from testpilot_adapters.chroma_vector import ChromaVectorStore
from testpilot_adapters.fs_docs import FileSystemDocsAdapter
from testpilot_adapters.fts_search import FtsSearchAdapter
from testpilot_adapters.hybrid_search import HybridSearchAdapter
from testpilot_adapters.json_events import JsonReplayEventSource
from testpilot_adapters.json_tickets import JsonTicketAdapter
from testpilot_adapters.ollama_model import OllamaModelGateway
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
        # S1+ 适配器仍是占位
        assert container.get("scm") is None
        # 离线 profile 不装向量适配器：入库跳过 embed、检索为 FTS-only
        assert container.get("vectors") is None
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
        # 模型网关注入同一 tracer（span 随调用链）
        assert model._tracer is container.get("tracer")  # noqa: SLF001
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
