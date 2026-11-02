"""录制型测试执行器（TestRunnerPort）——离线/无 Mac 演示与契约测试用。

从录制的 TestRun JSON 返回语言中立结果；生产替代是 xcode-runner（S3）/
junit-runner（P2）。工作流层不感知差异（docs/08 FR-EX-001/FR-EX-002）。
"""

from __future__ import annotations

import json
from pathlib import Path

from testpilot_core.enums import RunStatus
from testpilot_core.errors import AdapterUnavailableError
from testpilot_core.models import TestExecutionRequest, TestRun


class RecordedTestRunner:
    def __init__(self, fixtures_dir: str | Path) -> None:
        self.fixtures_dir = Path(fixtures_dir)
        self._jobs: dict[str, TestRun] = {}

    def _load(self, name: str) -> TestRun:
        path = self.fixtures_dir / f"{name}.json"
        if not path.exists():
            raise AdapterUnavailableError(f"录制执行夹具不存在: {path}")
        return TestRun.model_validate(json.loads(path.read_text(encoding="utf-8")))

    async def submit(self, request: TestExecutionRequest) -> str:
        name = request.selector.fixture_hint or "xcode_hostless"
        recorded = self._load(name)
        # 用本次请求覆盖身份字段，保留录制的用例结果与证据结构
        run = recorded.model_copy(
            update={
                "run_id": f"recorded-{request.request_id}",
                "idempotency_key": request.idempotency_key,
                "project": request.project,
                "commit_sha": request.commit_sha,
                "build_id": request.build_id or recorded.build_id,
            }
        )
        self._jobs[run.run_id] = run
        return run.run_id

    async def cancel(self, run_id: str) -> None:
        if run_id in self._jobs:
            self._jobs[run_id] = self._jobs[run_id].model_copy(update={"status": RunStatus.CANCELED})

    async def get_run(self, run_id: str) -> TestRun:
        if run_id not in self._jobs:
            raise AdapterUnavailableError(f"未知 run_id: {run_id}")
        return self._jobs[run_id]
