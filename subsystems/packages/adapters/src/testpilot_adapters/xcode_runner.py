"""xcodebuild 测试执行适配器（TestRunnerPort 的 iOS 真机侧实现，S3.2 FR-EX-003）。

职责切分：
- 命令拼装与能力判定复用 core 纯函数（xcode_commands.py），本类只做进程/文件 IO；
- 结果解析（xcresulttool）是 S3.3 的 FR-EX-005，经可注入 ``result_parser`` 接缝
  预留，本切片测试用假 parser，生产实现在 S3.3 接上即可零改本类；
- ``-resultBundlePath`` 已存在时先清理（FR-EX-002 AC3，xcodebuild 拒覆盖既有包）；
- 测试失败时 xcodebuild 退出码非 0 属正常，仍走 parser 产出 failed 的 TestRun；
  只有"进程结束但产物缺失"才判适配器不可用（工具链/模拟器崩溃）。
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Awaitable, Callable

from testpilot_core.errors import AdapterUnavailableError
from testpilot_core.execution import run_id_for
from testpilot_core.models import TestExecutionRequest, TestRun
from testpilot_core.xcode_commands import (
    DESTINATION_PLATFORM_DEVICE,
    DESTINATION_PLATFORM_SIMULATOR,
    build_xcodebuild_argv,
)

from .process_executor import SubprocessExecutor

# (request, result_bundle_path, env_id, returncode) -> TestRun；S3.3 接 xcresulttool
ResultParser = Callable[..., Awaitable[TestRun]]


async def unavailable_xcresult_parser(**_kwargs) -> TestRun:
    """S3.2 占位 parser：fail-closed，绝不用假数据冒充真实测试结论。

    xcresulttool 解析在 S3.3（FR-EX-005）落地；此前工厂装配 xcode runner 时
    注入本函数，任何真实跑完的取结果动作都会显式报"适配器未就绪"。
    """
    raise AdapterUnavailableError(
        "xcresulttool 结果解析器将在 S3.3（FR-EX-005）接入；"
        "当前切片只交付 xcodebuild 命令/进程层，禁止产出无解析证据的 TestRun"
    )


class XcodeTestRunner:
    """xcodebuild 执行器；模拟器与真机共用，目的地由 destination_platform 切换。

    真机切片一（2026-11-13）：profile runner.target=device 时传
    ``destination_platform="iOS"``、provider="ios-device"，udid 由真机租约透传；
    默认参数保持模拟器历史行为一字不变。
    """

    def __init__(
        self,
        *,
        workspace: str,
        scheme: str,
        repo_path: str | Path,
        result_bundle_dir: str | Path,
        result_parser: ResultParser,
        executor=None,
        destination_platform: str = DESTINATION_PLATFORM_SIMULATOR,
        provider: str = "ios-simulator",
    ) -> None:
        if destination_platform not in (
            DESTINATION_PLATFORM_SIMULATOR,
            DESTINATION_PLATFORM_DEVICE,
        ):
            raise ValueError(
                f"非法 destination_platform: {destination_platform!r}"
            )
        self.workspace = workspace
        self.scheme = scheme
        self.repo_path = str(repo_path)
        self.result_bundle_dir = Path(result_bundle_dir)
        self.result_bundle_dir.mkdir(parents=True, exist_ok=True)
        self.result_parser = result_parser
        self._parser = result_parser
        self._executor = executor or SubprocessExecutor()
        self.destination_platform = destination_platform
        self.provider = provider
        # run_id -> 作业句柄（进程/请求/产物路径）
        self._jobs: dict[str, dict] = {}

    async def submit(self, request: TestExecutionRequest) -> str:
        udid = request.environment.labels.get("udid", "")
        if not udid:
            # worker 应先经 EnvironmentPort.acquire 并把 udid 透传到 labels
            raise AdapterUnavailableError(
                "request.environment.labels 缺少 udid：未获取环境租约，拒绝裸跑 xcodebuild"
            )
        run_id = run_id_for(request.idempotency_key)
        bundle_path = self.result_bundle_dir / f"{run_id}.xcresult"

        # FR-EX-002 AC3：xcodebuild 拒覆盖既有 resultBundlePath，先删后跑
        if bundle_path.exists():
            shutil.rmtree(bundle_path)

        argv = build_xcodebuild_argv(
            workspace=self.workspace,
            scheme=self.scheme,
            udid=udid,
            only_testing=list(request.selector.test_ids),
            result_bundle_path=str(bundle_path),
            destination_platform=self.destination_platform,
        )
        process = await self._executor.spawn(argv, cwd=self.repo_path)
        self._jobs[run_id] = {
            "process": process,
            "request": request,
            "bundle_path": bundle_path,
            "udid": udid,
        }
        return run_id

    async def get_run(self, run_id: str) -> TestRun:
        job = self._jobs.get(run_id)
        if job is None:
            raise AdapterUnavailableError(f"未知 run_id: {run_id}")
        returncode = await job["process"].wait()
        bundle_path: Path = job["bundle_path"]
        if not bundle_path.exists():
            # 工具链/模拟器崩溃：xcodebuild 没留下任何可解析产物
            raise AdapterUnavailableError(
                f"xcodebuild 已退出(rc={returncode})但缺少 xcresult 产物: {bundle_path}"
            )
        run = await self._parser(
            request=job["request"],
            result_bundle_path=bundle_path,
            env_id=job["udid"],
            returncode=returncode,
        )
        # 账本以幂等键内容寻址 ID 为权威身份，覆盖 parser 内部 id
        return run.model_copy(update={"run_id": run_id})

    async def cancel(self, run_id: str) -> None:
        job = self._jobs.get(run_id)
        if job is not None and not job["process"].terminated:
            job["process"].terminate()
