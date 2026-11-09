"""profile 配置 schema（纯数据；读盘/环境变量合并在 orchestrator.config_loader）。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ProjectConfig(BaseModel):
    name: str
    repo_path: str  # 机器相关路径，禁止写死在代码里
    default_branch: str = "main"
    tech_stack: Literal["ios", "java", "other"] = "ios"
    ios: dict[str, str] = Field(default_factory=dict)  # workspace/scheme/destination udid


class AdapterRef(BaseModel):
    type: str  # 如 tickets-json / runner-recorded / model-ollama
    settings: dict[str, Any] = Field(default_factory=dict)


class AdapterSet(BaseModel):
    scm: AdapterRef
    docs: AdapterRef
    cases: AdapterRef
    # S2.2 FR-TD-001：需求文档连接器；带 stub 默认值，老 profile 不配也能加载
    requirements: AdapterRef = Field(default_factory=lambda: AdapterRef(type="stub"))
    tickets: AdapterRef
    # S2.5 FR-TD-004：人审回写用例库（markdown-fs 写独立写根 / stub 不装配）；
    # 带 stub 默认值，老 profile（含 ios-local-poc）不配也能加载，零改动
    case_repo: AdapterRef = Field(default_factory=lambda: AdapterRef(type="stub"))
    ci_events: AdapterRef
    runner: AdapterRef
    environment: AdapterRef
    artifact: AdapterRef
    logs: AdapterRef
    search: AdapterRef
    model: AdapterRef
    vectors: AdapterRef  # S1.2：stub（无向量库，FTS-only）/ chroma
    queue: AdapterRef
    tracer: AdapterRef


class Limits(BaseModel):
    max_agent_steps: int = 12
    max_tokens_per_run: int = 200_000
    daily_rerun_quota: int = 20
    request_timeout_s: int = 60


class ProfileConfig(BaseModel):
    profile: str  # ios-local-poc / offline-demo
    project: ProjectConfig
    paths: dict[str, str] = Field(default_factory=dict)  # data_dir / fixtures_dir
    adapters: AdapterSet
    limits: Limits = Field(default_factory=Limits)
    external_write_enabled: bool = False  # 全局停用开关，默认只 dry-run
    # S1.6 标定接缝：问答图参数（如 retrieval_limit）；缺省即 QaParams 保守默认，行为不变
    qa: dict[str, Any] = Field(default_factory=dict)
