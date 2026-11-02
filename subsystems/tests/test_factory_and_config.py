"""配置加载与适配器装配工厂测试（docs/08 FR-CFG-001/002）。"""

from pathlib import Path

import pytest

from testpilot_adapters.fs_docs import FileSystemDocsAdapter
from testpilot_adapters.fts_search import FtsSearchAdapter
from testpilot_adapters.json_events import JsonReplayEventSource
from testpilot_adapters.json_tickets import JsonTicketAdapter
from testpilot_adapters.recorded_model import RecordedModelGateway
from testpilot_adapters.recorded_runner import RecordedTestRunner
from testpilot_adapters.tracers import NoopTracer
from testpilot_orchestrator.config_loader import load_profile
from testpilot_orchestrator.factory import build_container

SUBSYSTEMS = Path(__file__).resolve().parents[1]
PROFILE = SUBSYSTEMS / "config" / "profiles" / "offline-demo.yaml"


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
    finally:
        container.get("store").close()


def test_repo_path_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("TESTPILOT_REPO_PATH", "/custom/repo")
    config = load_profile(PROFILE)
    assert config.project.repo_path == "/custom/repo"


def test_tracer_env_override(monkeypatch):
    # compose 起来后用 TESTPILOT_TRACER=langfuse 切换，不复制 profile 文件
    monkeypatch.setenv("TESTPILOT_TRACER", "langfuse")
    config = load_profile(PROFILE)
    assert config.adapters.tracer.type == "langfuse"
