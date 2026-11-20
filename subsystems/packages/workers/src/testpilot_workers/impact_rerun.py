"""impact-rerun-worker：影响面 → 映射 → 选择执行一键接线 CLI（docs/01 E4）。

把既有 S4.3 影响面分析与 S3.1 执行队列两段能力焊起来：

1. ``analyze_impact``（impact_service，既有只读服务）产出三分类影响链；
2. 读 E3 的 automation-map-v1，做版本/鲜度守卫（head 不一致直接拒）；
3. core 纯函数 :mod:`testpilot_core.impact_execution` 把 runnable 用例翻译成
   ``-only-testing`` 标识符，未映射/冲突/无自动化桶显式报告；
4. 空选择 fail-closed（exit 2，绝不退化为全量）；非空则构造
   :class:`TestExecutionRequest` 走既有 ``execution_service.submit_execution``
   入队——队列、重跑配额、状态机、幂等重放全部零新造。

``--dry-run`` 只打印选择集与拟提交请求，不写任何账本。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from testpilot_core.errors import AdapterUnavailableError
from testpilot_core.impact_execution import (
    ImpactSelectionError,
    build_impact_selection,
    ensure_map_fresh,
    validate_selection_nonempty,
)
from testpilot_core.models import CaseSelector, TestExecutionRequest

from . import execution_service as svc
from ._bootstrap import bootstrap
from .impact_service import analyze_impact

_MAP_FILENAME = "automation-map.json"


def _load_map(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(
            f"automation-map 不存在: {path}；请先在演示分支运行 "
            "testpilot-scan-automation 建立用例↔测试映射")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ImpactSelectionError(f"automation-map 损坏，拒绝使用: {exc}") from exc


async def run_impact_rerun(
    container: Any,
    *,
    base_commit: str,
    head_commit: str,
    requirement_urns: list[str] | None = None,
    dry_run: bool = False,
    project: str = "",
    map_path: str | Path | None = None,
) -> dict[str, Any]:
    """分析→映射→提交的完整编排；任何硬错误抛 ValueError/RuntimeError/FileNotFoundError。"""
    store = container.get("store")
    if store is None:
        raise RuntimeError("容器未装配 store，无法做影响面选择执行")

    # 1) 影响面三分类（既有只读服务，内部要求 scm/repo 齐备）
    analysis = await analyze_impact(
        container,
        base_commit=base_commit,
        head_commit=head_commit,
        requirement_urns=requirement_urns or [],
    )

    # 2) map 落盘读取 + 版本/鲜度守卫
    map_file = Path(map_path) if map_path else Path(container.data_dir) / _MAP_FILENAME
    map_payload = _load_map(map_file)
    ensure_map_fresh(map_payload, head_commit)

    # 3) 翻译选择集（runnable → only_testing；其余桶只报告）
    selection = build_impact_selection(
        analysis=analysis,
        bindings=map_payload.get("bindings", []),
        head_commit=head_commit,
    )
    # 4) 空选择硬失败：禁止全量退化
    validate_selection_nonempty(selection)

    project_name = project or container.config.project.name
    request = TestExecutionRequest(
        request_id=selection.request_id,
        idempotency_key=selection.idempotency_key,
        project=project_name,
        selector=CaseSelector(test_ids=selection.only_testing_ids),
        commit_sha=head_commit,
        triggered_by="agent",
        daily_budget_key=selection.daily_budget_key,  # 占每日重跑配额（按 head 分桶）
    )

    payload: dict[str, Any] = {
        "dry_run": dry_run,
        "base_commit": base_commit,
        "head_commit": head_commit,
        "project": project_name,
        "map_hash": map_payload.get("map_hash", ""),
        "classification_counts": analysis.classification_counts,
        "selection": selection.model_dump(),
        "request": request.model_dump(mode="json"),
    }
    if dry_run:
        # 不触碰账本/队列：dry-run 的全部副作用只在 stdout
        payload["submission"] = None
        return payload

    payload["submission"] = await svc.submit_execution(container, request)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(
        description="TestPilot 影响面选择执行 worker（impact → automation-map → 执行队列）")
    parser.add_argument("--base", required=True, help="基线 commit sha（或空树 sha）")
    parser.add_argument("--head", required=True, help="目标 commit sha（须等于 map.head_commit）")
    parser.add_argument("--requirement", action="append", default=[],
                        help="需求 URN 入口，可重复")
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印选择集与拟提交请求，不写账本不入队")
    parser.add_argument("--project", default="",
                        help="项目名，缺省取 profile 的 project.name")
    parser.add_argument("--map", default=None,
                        help=f"automation-map 路径，缺省 <data_dir>/{_MAP_FILENAME}")
    args = parser.parse_args()

    try:
        container = bootstrap("impact-rerun-worker")
        payload = asyncio.run(run_impact_rerun(
            container,
            base_commit=args.base,
            head_commit=args.head,
            requirement_urns=args.requirement,
            dry_run=args.dry_run,
            project=args.project,
            map_path=args.map,
        ))
    except (ImpactSelectionError, ValueError, RuntimeError, FileNotFoundError,
            AdapterUnavailableError) as exc:
        # AdapterUnavailableError：git diff 不可达等外部依赖失败，与 runner CLI
        # 同口径收口为 exit 2，不抛 traceback
        print(f"[impact-rerun-worker] 选择执行被拒: {exc}", file=sys.stderr)
        sys.exit(2)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    sys.exit(0)


def make_fake_container(store, scm, queue, *, repo, project="demo",
                        data_dir=None, daily_rerun_quota=20):
    """测试用最小容器：真实 store/queue + 假 scm/repo + NoopTracer。"""
    from testpilot_adapters.tracers import NoopTracer

    config = SimpleNamespace(
        project=SimpleNamespace(name=project),
        limits=SimpleNamespace(daily_rerun_quota=daily_rerun_quota),
    )
    container = SimpleNamespace(
        config=config,
        data_dir=str(data_dir) if data_dir is not None else ".",
        adapters={
            "store": store, "queue": queue, "scm": scm,
            "repo": repo, "tracer": NoopTracer(),
        },
    )
    container.get = lambda name: container.adapters.get(name)  # type: ignore[attr-defined]
    return container


if __name__ == "__main__":
    main()
