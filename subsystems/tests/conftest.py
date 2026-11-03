"""pytest 全局夹具：保证测试与调用方 shell 环境隔离（hermetic）。

config_loader 支持 TESTPILOT_* 环境变量覆盖 profile（如手工跑 Langfuse 门禁时
export 的 TESTPILOT_TRACER=langfuse）。这些变量会泄漏进 pytest 子进程并改变
适配器装配，导致"本机全绿、别人/CI 红"或反之。每个用例开始前统一清掉已知覆盖项；
专门验证覆盖行为的用例自己用 monkeypatch.setenv 设置（同 function 作用域，
在本夹具之后生效，结束后由 monkeypatch 一并还原）。
"""

import pytest

from testpilot_orchestrator.config_loader import _ENV_OVERRIDES

# 直接从 config_loader 的映射派生，新增覆盖项时测试自动跟进，不会漏清
_OVERRIDE_ENVS = tuple(dict.fromkeys(_ENV_OVERRIDES.values()))


@pytest.fixture(autouse=True)
def _clean_testpilot_env(monkeypatch):
    for name in _OVERRIDE_ENVS:
        monkeypatch.delenv(name, raising=False)
    yield
