"""scan-automation-worker：automation-map-v1 自动化身份映射 CLI（docs/01 E3）。

扫描语料仓工作区（人工已切到演示分支）的 Swift 测试，按
``// testpilot-case: <TC-ID>`` 标记建立「用例 ↔ Xcode 测试方法」绑定，
落盘 ``<data_dir>/automation-map.json`` 并把绑定投影到 test_cases.raw_json
（schema v15 零变更）。

纪律：

- 纯只读访问语料仓（文件遍历 + scm.head_sha()），不发起任何写 git 操作；
- 硬错误（未知 TC 标记/悬空 manual 绑定/畸形标记）→ exit 2，不写 map；
- 扫描警告（重复标记/探索性绑定）→ map 照写但 exit 3，催人审；
- 重扫幂等：已有人审 marker 绑定与 manual 绑定从旧 map 读出并重新校验，
   reviewer 信息不丢；指向已删方法的 manual 绑定 fail-closed。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from testpilot_core.automation_mapping import (
    AutomationMapError,
    ManualBindingInput,
    PROJECT_ROOT_SEGMENT,
    ScanResult,
    automation_map_hash,
    build_automation_map,
    scan_swift_tests,
    target_for_path,
)
from testpilot_core.case_export import DEFAULT_TEST_TARGET, DEFAULT_UI_TEST_TARGET

from ._bootstrap import bootstrap

_SKIP_DIRS = {".git", "build", "DerivedData", "Pods", ".build", "logs"}
_MAP_FILENAME = "automation-map.json"


def _resolve_scan_config(
    container: Any, *,
    engineering_root: str | None,
    unit_target: str | None,
    ui_target: str | None,
) -> tuple[str, str, str]:
    """解析工程根与两个 target 名，三层优先级：CLI 显式参数 > profile
    project.ios 三键 > core legacy 常量。

    测试假容器可能挂裸 SimpleNamespace（无 project/ios 属性），全程 getattr
    兜底：缺一层就往下落，最终回退常量，行为与通用化前逐字一致。
    """
    ios = getattr(getattr(getattr(container, "config", None),
                          "project", None), "ios", None) or {}
    return (
        engineering_root or ios.get("engineering_root") or PROJECT_ROOT_SEGMENT,
        unit_target or ios.get("unit_test_target") or DEFAULT_TEST_TARGET,
        ui_target or ios.get("ui_test_target") or DEFAULT_UI_TEST_TARGET,
    )


def _iter_swift_files(
    repo_root: Path, *,
    unit_target: str, ui_target: str, project_root: str,
) -> dict[str, str]:
    """收集 workspace 工程内测试 target 的 swift 文件（相对路径 → 文本）。

    target 判定唯一走 core.target_for_path：既跳过构建/依赖目录，也挡住
    工程根 containment 之外的作废测试目录（如演示仓里根 Tests/ 的作废 SPM
    包，不在 xcworkspace 内）。工程根与 target 名全部由调用方按
    CLI > profile > core 常量解析后注入，本函数不读任何全局配置。
    """
    files: dict[str, str] = {}
    for path in sorted(repo_root.rglob("*.swift")):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        try:
            rel = path.relative_to(repo_root).as_posix()
        except ValueError:
            continue
        if target_for_path(
            rel, unit_target=unit_target, ui_target=ui_target,
            project_root=project_root,
        ) is None:
            continue
        files[rel] = path.read_text(encoding="utf-8")
    return files


def _load_existing_map(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise AutomationMapError(f"既有 automation-map.json 损坏，拒绝覆盖: {exc}") from exc
    if data.get("map_schema_version") != "automation-map-v1":
        raise AutomationMapError(
            f"既有 map 版本不支持: {data.get('map_schema_version')!r}")
    return data


def _parse_bind_spec(spec: str) -> tuple[str, str]:
    case_id, sep, oid = spec.partition("=")
    if not sep or not case_id or not oid:
        raise AutomationMapError(
            f"--bind 形态必须是 <TC-ID>=<Target/Class/method>，实际: {spec!r}")
    return case_id.strip(), oid.strip()


def _merge_review_inputs(
    existing: dict[str, Any],
    review_pairs: list[tuple[str, str]],
    bind_specs: list[str],
    stamp: str,
) -> tuple[dict[tuple[str, str], tuple[str, str]], list[ManualBindingInput]]:
    """从旧 map + CLI 参数汇总人审信息（marker 评审记录 + manual 绑定）。"""
    reviewed: dict[tuple[str, str], tuple[str, str]] = {}
    manuals: list[ManualBindingInput] = []
    for row in existing.get("bindings", []):
        if row.get("origin") == "manual":
            manuals.append(ManualBindingInput(
                case_external_id=row["case_external_id"],
                only_testing_id=row["only_testing_id"],
                reviewed_by=row.get("reviewed_by", ""),
                reviewed_at=row.get("reviewed_at") or stamp))
        elif row.get("reviewed_by"):
            reviewed[(row["case_external_id"], row["only_testing_id"])] = (
                row["reviewed_by"], row.get("reviewed_at") or stamp)
    # CLI 新输入：--bind 直接形成 manual 绑定；--review 给 marker 绑定补人审
    for spec in bind_specs:
        case_id, oid = _parse_bind_spec(spec)
        manuals.append(ManualBindingInput(
            case_external_id=case_id, only_testing_id=oid,
            reviewed_by=_pending_by(review_pairs) or "human", reviewed_at=stamp))
    return reviewed, manuals


def _pending_by(review_pairs: list[tuple[str, str]]) -> str:
    return review_pairs[-1][1] if review_pairs else ""


def _apply_reviews(
    result: ScanResult,
    reviewed: dict[tuple[str, str], tuple[str, str]],
    review_pairs: list[tuple[str, str]],
    stamp: str,
) -> None:
    cli_review = dict(review_pairs)
    for binding in result.bindings:
        if binding.origin != "marker":
            continue
        key = (binding.case_external_id, binding.only_testing_id)
        if key in reviewed:
            binding.reviewed_by, binding.reviewed_at = reviewed[key]
        if binding.case_external_id in cli_review:
            binding.reviewed_by = cli_review[binding.case_external_id]
            binding.reviewed_at = stamp


async def run_scan(
    container: Any, *,
    out: str | Path | None = None,
    review_pairs: list[tuple[str, str]] | None = None,
    bind_specs: list[str] | None = None,
    now: datetime | None = None,
    engineering_root: str | None = None,
    unit_target: str | None = None,
    ui_target: str | None = None,
) -> dict[str, Any]:
    review_pairs = review_pairs or []
    bind_specs = bind_specs or []
    # 工程根/target 名统一在扫描动作之前解析一次，文件收集与 core 扫描两处
    # 必须用同一组值，否则 containment 与 OID target 会各说各话。
    project_root, resolved_unit, resolved_ui = _resolve_scan_config(
        container, engineering_root=engineering_root,
        unit_target=unit_target, ui_target=ui_target)
    store = container.get("store")
    if store is None:
        raise RuntimeError("容器未装配 store，无法扫描自动化映射")
    repo_ref = container.get("repo")
    if repo_ref is None:
        raise RuntimeError("容器未装配 repo 坐标，无法定位语料仓")
    repo_root = Path(repo_ref.repo_path)
    if not repo_root.is_dir():
        raise FileNotFoundError(f"语料仓目录不可访问: {repo_root}")

    now = now or datetime.now(timezone.utc)
    stamp = now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    map_path = Path(out) if out else Path(container.data_dir) / _MAP_FILENAME
    existing = _load_existing_map(map_path)

    active_cases = store.list_test_cases(include_cancelled=False)
    active_ids = [c.external_id for c in active_cases]
    exploratory_ids = [c.external_id for c in active_cases if c.exploratory]

    reviewed, manuals = _merge_review_inputs(
        existing, review_pairs, bind_specs, stamp)

    files = _iter_swift_files(
        repo_root, unit_target=resolved_unit, ui_target=resolved_ui,
        project_root=project_root)
    result: ScanResult = scan_swift_tests(
        files, active_ids,
        exploratory_ids=exploratory_ids,
        unit_target=resolved_unit,
        ui_target=resolved_ui,
        project_root=project_root,
        manual_bindings=manuals)
    _apply_reviews(result, reviewed, review_pairs, stamp)

    scm = container.get("scm")
    head_commit = await scm.head_sha() if scm is not None else ""
    branch = repo_ref.default_branch
    payload = build_automation_map(
        result, repo_path=str(repo_root), branch=branch,
        head_commit=head_commit, generated_at=stamp)
    if automation_map_hash(payload) != payload["map_hash"]:  # 防御性自检
        raise AutomationMapError("map hash 自检不一致，拒绝落盘")

    map_path.parent.mkdir(parents=True, exist_ok=True)
    map_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # raw_json.automation 投影（零 schema 变更）；未映射用例投显式负态
    projected, missing = [], []
    bindings_by_case: dict[str, list[dict[str, Any]]] = {}
    for binding in result.bindings:
        bindings_by_case.setdefault(binding.case_external_id, []).append(
            binding.model_dump())
    for case in active_cases:
        automation = {
            "mapped": case.external_id in bindings_by_case,
            "bindings": bindings_by_case.get(case.external_id, []),
            "head_commit": head_commit,
            "map_hash": payload["map_hash"],
            "projected_at": stamp,
        }
        affected = store.project_case_automation(case.external_id, automation)
        (projected if affected else missing).append(case.external_id)

    return {
        "path": str(map_path),
        "map_hash": payload["map_hash"],
        "head_commit": head_commit,
        "branch": branch,
        "bindings": len(result.bindings),
        "unlinked_swift_tests": len(result.unlinked_swift_tests),
        "unmapped_active_cases": result.unmapped_active_cases,
        "scan_warnings": result.scan_warnings,
        "projection_missing_ids": sorted(missing),
        "scanned_files": len(files),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="TestPilot automation-map-v1 自动化身份映射 worker")
    parser.add_argument("--out", default=None,
                        help=f"映射文件输出路径；默认 <data_dir>/{_MAP_FILENAME}")
    parser.add_argument("--review", action="append", default=[], metavar="TC-ID",
                        help="对指定 TC 的 marker 绑定登记人审，需与 --by 配对，可重复")
    parser.add_argument("--by", default="", help="人审登记人（配合 --review/--bind）")
    parser.add_argument("--bind", action="append", default=[],
                        metavar="TC-ID=Target/Class/method",
                        help="无标记存量测试的手工绑定，可重复；方法必须真实存在")
    parser.add_argument("--engineering-root", default=None,
                        help="覆盖 profile project.ios.engineering_root（workspace "
                             "工程根目录段，containment 用）；缺省再回退内置常量")
    parser.add_argument("--unit-test-target", default=None,
                        help="覆盖 profile project.ios.unit_test_target（单元测试 "
                             "target 名）；缺省再回退内置常量")
    parser.add_argument("--ui-test-target", default=None,
                        help="覆盖 profile project.ios.ui_test_target（UI 测试 "
                             "target 名）；缺省再回退内置常量")
    args = parser.parse_args()

    if (args.review or args.bind) and not args.by:
        print("[scan-automation-worker] --review/--bind 必须配合 --by 登记人审人",
              file=sys.stderr)
        sys.exit(2)

    try:
        container = bootstrap("scan-automation-worker")
        summary = asyncio.run(run_scan(
            container,
            out=args.out,
            review_pairs=[(tc, args.by) for tc in args.review],
            bind_specs=args.bind,
            engineering_root=args.engineering_root,
            unit_target=args.unit_test_target,
            ui_target=args.ui_test_target,
        ))
    except (AutomationMapError, ValueError, RuntimeError, FileNotFoundError) as exc:
        print(f"[scan-automation-worker] 扫描被拒: {exc}", file=sys.stderr)
        sys.exit(2)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    # 三档退出码契约：0 干净；2 硬失败（无 map）；3 map 已产出但存在催人审警告
    if summary["scan_warnings"]:
        sys.exit(3)
    sys.exit(0)


def make_fake_container(store, repo_root, *, head_commit="",
                        default_branch="demo/agent-generated-tests",
                        data_dir=None, ios_config=None):
    """测试用最小容器：repo 坐标 + 可选假 scm + store。

    ``ios_config`` 给定时挂 ``config.project.ios``（与真 ProfileConfig 同构）；
    不给定则保持裸 SimpleNamespace，用于验证缺省回退 core 常量的第三层优先级。
    """
    repo = SimpleNamespace(repo_path=str(repo_root),
                           default_branch=default_branch)

    class _FakeScm:
        async def head_sha(self, repo=None):
            return head_commit

    if ios_config is None:
        config: Any = SimpleNamespace()
    else:
        config = SimpleNamespace(
            project=SimpleNamespace(ios=dict(ios_config)))
    container = SimpleNamespace(
        config=config, data_dir=data_dir or repo_root / "work",
        adapters={"store": store, "repo": repo, "scm": _FakeScm()})
    container.get = lambda name: container.adapters.get(name)  # type: ignore[attr-defined]
    return container


if __name__ == "__main__":
    main()
