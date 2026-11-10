"""JUnit/Surefire XML 执行适配器（TestRunnerPort 的 JVM 侧实现，S3.1 FR-EX-001）。

与 :class:`RecordedTestRunner` 平级，但输入不是录制好的 TestRun JSON，而是真实
Surefire XML 报告文件——解析管线全部复用 core 的纯函数（junit_report.py），
本适配器只做文件 IO、hash 与身份填充。同一份语言中立 :class:`TestRun` 模型既能
装 xcode 结果也能装 Surefire 结果，是 FR-EX-001 AC1/AC2 的适配器侧证据。

S5 接真实 Jenkins 产物时换 reports_dir 即可，解析/归一化代码零改动。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from testpilot_core.enums import ArtifactType, RunStatus
from testpilot_core.errors import AdapterUnavailableError, SchemaValidationError
from testpilot_core.execution import run_id_for
from testpilot_core.junit_report import (
    parse_surefire_xml,
    test_run_from_junit,
)
from testpilot_core.models import ArtifactRef, TestExecutionRequest, TestRun


class JUnitXmlRunner:
    def __init__(
        self,
        reports_dir: str | Path,
        *,
        provider: str = "junit-local",
        env_id: str = "local-jvm",
        capabilities: list[str] | None = None,
        version: str = "",
        runner_capability_version: str = "junit-xml",
    ) -> None:
        self.reports_dir = Path(reports_dir)
        self.provider = provider
        self.env_id = env_id
        self.capabilities = capabilities or []
        self.version = version
        self.runner_capability_version = runner_capability_version
        self._jobs: dict[str, TestRun] = {}

    def _resolve_report(self, request: TestExecutionRequest) -> Path:
        hint = request.selector.fixture_hint
        if hint:
            name = hint if hint.endswith(".xml") else f"{hint}.xml"
            path = self.reports_dir / name
        else:
            # 无显式提示时只允许目录里恰好一份报告，多份必须点名（防静默拿错文件）
            candidates = sorted(self.reports_dir.glob("TEST-*.xml"))
            if len(candidates) > 1:
                raise SchemaValidationError(
                    f"reports_dir 存在多份 Surefire 报告，必须用 fixture_hint 点名: "
                    f"{[p.name for p in candidates]}"
                )
            path = candidates[0] if candidates else self.reports_dir / "TEST-missing.xml"
        if not path.exists():
            raise AdapterUnavailableError(f"Surefire 报告不存在: {path}")
        return path

    async def submit(self, request: TestExecutionRequest) -> str:
        path = self._resolve_report(request)
        xml_text = path.read_text(encoding="utf-8")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        run = test_run_from_junit(
            parse_surefire_xml(xml_text),
            request,
            env_id=self.env_id,
            provider=self.provider,
            capabilities=self.capabilities,
            version=self.version,
            source_name=path.name,
            runner_capability_version=self.runner_capability_version,
        )
        run = run.model_copy(
            update={
                "run_id": run_id_for(request.idempotency_key),
                "artifacts": [
                    ArtifactRef(
                        type=ArtifactType.JUNIT_XML,
                        uri=path.resolve().as_uri(),
                        content_hash=digest[:12],
                    )
                ],
                "raw_report_ref": path.resolve().as_uri(),
            }
        )
        self._jobs[run.run_id] = run
        return run.run_id

    async def cancel(self, run_id: str) -> None:
        # 报告解析是秒级本地动作，取消只对已提交未收尾的内存任务生效（与录制器同口径）
        if run_id in self._jobs:
            self._jobs[run_id] = self._jobs[run_id].model_copy(
                update={"status": RunStatus.CANCELED}
            )

    async def get_run(self, run_id: str) -> TestRun:
        if run_id not in self._jobs:
            raise AdapterUnavailableError(f"未知 run_id: {run_id}")
        return self._jobs[run_id]
