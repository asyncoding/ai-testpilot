"""export-cases-worker：case-export-v1 结构化用例集导出 CLI（docs/12 E1）。

从 profile store 读需求骨架 / 测试点 / 正式用例 / oracle 挖掘草稿，经
``core.case_export`` 纯函数组装版本化 JSON 用例集，默认落盘
``<data_dir>/exports/case-set-<utc>-<hash8>.json``，供人工携出系统边界
交给外部 Agent 生成 Xcode 测试。

纪律：

- 零活跃用例时 core 抛 ``CaseExportError``，本 CLI 映射 exit 2，**先构建后
  落盘**，绝不留下空壳文件；
- 只读 worker，不写语料仓、不调外部系统；写根只有 profile data_dir；
- 版本四元组不可得时留空串（对齐 eval_runner 口径），可用 CLI 参数显式补。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from testpilot_core.case_export import (
    CaseExportError,
    GenerationConvention,
    VersionQuadruple,
    build_case_export,
    default_generation_convention,
    export_filename,
)

from ._bootstrap import bootstrap


def run_export(
    container: Any,
    *,
    out: str | Path | None = None,
    include_exploratory: bool = True,
    version: VersionQuadruple | None = None,
    test_target: str | None = None,
    ui_test_target: str | None = None,
    source_branch: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """装配无关的导出主体：输入容器（真容器或测试假容器），返回落盘摘要。"""
    store = container.get("store")
    if store is None:
        raise RuntimeError("容器未装配 store，无法导出用例集")
    config = container.config
    now = now or datetime.now(timezone.utc)

    # 整库读出（含 cancelled），分区逻辑在 core 纯函数里，便于 hostless 测试。
    all_cases = store.list_test_cases(include_cancelled=True)
    payload = build_case_export(
        profile=config.profile,
        project=config.project.name,
        source_branch=source_branch or config.project.default_branch,
        requirements=store.list_requirement_skeletons(),
        test_points=store.list_test_points(),
        cases=all_cases,
        oracle_suggestions=store.list_oracle_suggestions(),
        now=now,
        version=version,
        convention=_build_convention(test_target, ui_test_target),
        include_exploratory=include_exploratory,
    )

    data_dir: Path = Path(container.data_dir)
    path = _resolve_out_path(Path(out) if out else data_dir / "exports",
                             now, payload["content_hash"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    return {
        "path": str(path),
        "content_hash": payload["content_hash"],
        "exported_at": payload["exported_at"],
        "export_schema_version": payload["export_schema_version"],
        "cases_exported": len(payload["cases"]),
        "requirements_exported": len(payload["requirements"]),
        "test_points_exported": len(payload["test_points"]),
        "cancelled_case_ids": payload["excluded"]["cancelled_case_ids"],
        "exploratory_excluded_ids": payload["excluded"]["exploratory_case_ids"],
    }


def _build_convention(
    test_target: str | None, ui_test_target: str | None,
) -> GenerationConvention:
    if test_target is None and ui_test_target is None:
        return default_generation_convention()
    base = default_generation_convention()
    return GenerationConvention(
        marker_comment=base.marker_comment,
        naming=base.naming,
        target_target=test_target or base.target_target,
        ui_test_target=ui_test_target or base.ui_test_target,
        detail_doc=base.detail_doc,
    )


def _resolve_out_path(out: Path, now: datetime, content_hash: str) -> Path:
    """``--out`` 指向 .json 文件则原样用；目录（含默认 exports/）则拼规范文件名。"""
    if out.suffix == ".json":
        return out
    return out / export_filename(now, content_hash)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="TestPilot case-export-v1 结构化用例集导出 worker")
    parser.add_argument("--out", default=None,
                        help="输出 .json 文件或目录；默认 <data_dir>/exports/")
    parser.add_argument("--include-exploratory", action=argparse.BooleanOptionalAction,
                        default=True,
                        help="是否导出探索性用例（默认导出并打标；--no-include-exploratory 排除）")
    parser.add_argument("--data-version", default="", help="版本四元组：语料数据版本")
    parser.add_argument("--model-version", default="", help="版本四元组：模型版本")
    parser.add_argument("--prompt-version", default="", help="版本四元组：prompt 版本")
    parser.add_argument("--params-version", default="", help="版本四元组：参数版本")
    parser.add_argument("--test-target", default=None,
                        help="覆盖生成约定中的单元测试 target（默认 AlarmKitSpikeTests）")
    parser.add_argument("--ui-test-target", default=None,
                        help="覆盖生成约定中的 UI 测试 target")
    parser.add_argument("--source-branch", default=None,
                        help="覆盖来源分支（默认取 profile project.default_branch）")
    args = parser.parse_args()

    try:
        container = bootstrap("export-cases-worker")
        summary = run_export(
            container,
            out=args.out,
            include_exploratory=args.include_exploratory,
            version=VersionQuadruple(
                data_version=args.data_version,
                model_version=args.model_version,
                prompt_version=args.prompt_version,
                params_version=args.params_version,
            ),
            test_target=args.test_target,
            ui_test_target=args.ui_test_target,
            source_branch=args.source_branch,
        )
    except (CaseExportError, ValueError, RuntimeError, FileNotFoundError) as exc:
        print(f"[export-cases-worker] 导出被拒: {exc}", file=sys.stderr)
        sys.exit(2)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


# 测试侧构造最小假容器的便捷构造器（与 test_testpoint_worker 的 SimpleNamespace 同口径）。
def make_fake_container(store: Any, *, profile: str, project: str,
                        data_dir: Path, default_branch: str = "main") -> SimpleNamespace:
    config = SimpleNamespace(
        profile=profile,
        project=SimpleNamespace(
            name=project, default_branch=default_branch),
    )
    container = SimpleNamespace(config=config, data_dir=data_dir,
                                adapters={"store": store})
    container.get = lambda name: container.adapters.get(name)  # type: ignore[attr-defined]
    return container


if __name__ == "__main__":
    main()
