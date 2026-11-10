"""xcresulttool 结果解析适配器（FR-EX-005，S3.3）。

实现 :class:`XcodeTestRunner` 的 ``result_parser`` 接缝（签名
request/result_bundle_path/env_id/returncode），把 S3.2 的
``unavailable_xcresult_parser`` 占位替换为真实链路：

1. ``xcresulttool get test-results tests --format json`` → core 纯函数展平用例树；
2. ``get test-results attachments --format json`` → 附件清单（case_ref 归属）；
3. ``export attachments --output-path <临时目录>`` → 附件字节落盘；
4. 每个附件经 :class:`LocalFsArtifactAdapter` 内容寻址入制品根，URN 回链到
   失败用例的 ``artifact_urns``（AC1）；大文件只存 URI+hash，不进向量库/SQLite；
5. tests 原始 JSON 同样入制品根，作为 ``raw_report_ref`` 与 XCRESULT 制品。

纪律：xcresulttool 非零退出、导出文件缺失一律 AdapterUnavailableError 响亮失败，
绝不用假数据冒充结论；真实 xcresulttool 输出形态以手动探针校准（见 S3.3 handoff）。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from testpilot_core.enums import ArtifactType
from testpilot_core.errors import AdapterUnavailableError, SchemaValidationError
from testpilot_core.execution import run_id_for
from testpilot_core.models import ArtifactRef, TestExecutionRequest, TestRun
from testpilot_core.xcode_commands import DEVICE_ONLY_CAPABILITIES
from testpilot_core.xcresult_parse import (
    link_attachment_urns,
    parse_attachments,
    parse_test_nodes,
    test_run_from_xcresult,
)

from .localfs_artifact import LocalFsArtifactAdapter
from .process_executor import SubprocessExecutor

# 模拟器物理层跑不到的三类能力，任何 simulator run 都显式声明未覆盖
_SIMULATOR_UNCOVERED = sorted(DEVICE_ONLY_CAPABILITIES)


class XcresultToolResultParser:
    """对上 XcodeTestRunner.result_parser 接缝；进程/文件 IO 集中在本类。"""

    def __init__(
        self,
        artifact_store: LocalFsArtifactAdapter,
        *,
        work_dir: str | Path,
        executor=None,
        xcrun: str = "xcrun",
    ) -> None:
        self._store = artifact_store
        self._work_dir = Path(work_dir)
        self._work_dir.mkdir(parents=True, exist_ok=True)
        self._executor = executor or SubprocessExecutor()
        self._xcrun = xcrun

    def _tests_argv(self, bundle: str) -> list[str]:
        return [
            self._xcrun, "xcresulttool", "get", "test-results", "tests",
            "--format", "json", "--path", bundle,
        ]

    def _attachments_argv(self, bundle: str) -> list[str]:
        return [
            self._xcrun, "xcresulttool", "get", "test-results", "attachments",
            "--format", "json", "--path", bundle,
        ]

    def _export_argv(self, bundle: str, output_dir: str) -> list[str]:
        return [
            self._xcrun, "xcresulttool", "export", "attachments",
            "--path", bundle, "--output-path", output_dir,
        ]

    async def _run_loud(self, argv: list[str]) -> str:
        """跑 xcresulttool 子命令；非零退出按适配器不可用收口（含 stderr 证据）。"""
        result = await self._executor.run(argv)
        if result.returncode != 0:
            raise AdapterUnavailableError(
                f"xcresulttool 失败 rc={result.returncode}: "
                f"{' '.join(argv[:4])}… stderr={result.stderr[:300]}"
            )
        return result.stdout

    @staticmethod
    def _maybe_parse_attachments(stdout: str) -> dict:
        """全绿 run 没有附件是常态：空清单返回 {}，畸形 JSON 仍 fail-closed。"""
        if not stdout.strip():
            return {}
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise SchemaValidationError(
                f"附件清单 JSON 解析失败: {exc}"
            ) from exc
        if not isinstance(data, dict) or not data.get("attachments"):
            return {}
        return parse_attachments(data)

    async def __call__(
        self,
        *,
        request: TestExecutionRequest,
        result_bundle_path: str | Path,
        env_id: str,
        returncode: int,
    ) -> TestRun:
        bundle = str(result_bundle_path)
        run_id = run_id_for(request.idempotency_key)
        run_dir = self._work_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        # ① 用例树
        tests_stdout = await self._run_loud(self._tests_argv(bundle))
        cases = parse_test_nodes(tests_stdout)

        # ② 附件清单（无附件时不触发导出）
        manifest_stdout = await self._run_loud(self._attachments_argv(bundle))
        grouped = self._maybe_parse_attachments(manifest_stdout)

        artifact_refs: list[ArtifactRef] = []
        urns_by_case: dict[str, list[str]] = {}
        if grouped:
            # ③ 导出附件字节
            export_dir = run_dir / "attachments"
            export_dir.mkdir(parents=True, exist_ok=True)
            await self._run_loud(self._export_argv(bundle, str(export_dir)))

            # ④ 内容寻址入制品根并按用例回链
            for case_ref, attachments in grouped.items():
                for att in attachments:
                    src = export_dir / att.file_name
                    if not src.exists():
                        # 清单声称有附件但导出缺文件：证据链断裂，响亮失败不静默
                        raise AdapterUnavailableError(
                            f"附件导出缺失: {att.file_name}（case_ref={case_ref}）"
                        )
                    urn = await self._store.put(
                        str(src), metadata={"type": att.artifact_type.value}
                    )
                    digest = hashlib.sha256(src.read_bytes()).hexdigest()[:12]
                    artifact_refs.append(
                        ArtifactRef(
                            type=att.artifact_type, uri=urn, content_hash=digest
                        )
                    )
                    urns_by_case.setdefault(case_ref, []).append(urn)

        # ⑤ 原始 tests JSON 入制品根，作为可回放的 raw_report_ref
        raw_path = run_dir / "test-results.json"
        raw_bytes = tests_stdout.encode("utf-8")
        raw_path.write_bytes(raw_bytes)
        raw_urn = await self._store.put(
            str(raw_path), metadata={"type": "xcresult"}
        )
        raw_hash = hashlib.sha256(raw_bytes).hexdigest()[:12]
        artifact_refs.append(
            ArtifactRef(type=ArtifactType.XCRESULT, uri=raw_urn,
                        content_hash=raw_hash)
        )

        run = test_run_from_xcresult(
            cases,
            request,
            env_id=env_id,
            capabilities=list(request.environment.capabilities),
            artifacts=artifact_refs,
            raw_report_ref=raw_urn,
            uncovered_layers=_SIMULATOR_UNCOVERED,
        )
        if urns_by_case:
            linked = link_attachment_urns(run.case_results, urns_by_case)
            run = run.model_copy(update={"case_results": linked})
        return run
