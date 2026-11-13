"""scan-generated-swift-worker：AgentGenerated Swift 静态红线 CLI（H5）。

core.generated_swift_lint 的薄 IO 封装：在语料仓内收集所有路径含
``AgentGenerated`` 段的 ``.swift`` 文件送纯函数扫描，汇总违规并按三档
退出码喊话。红线只对生成目录生效，仓里人工存量代码（含 sleep 的旧测试）
一律不扫、不误伤——作用域守卫在 core 内 fail-closed，本层只负责选文件。

退出码契约（与 scan-automation-worker 对齐）：

- 0：扫描完成且零违规；
- 2：硬错误（仓目录不可访问、IO 失败、圈外文件被误送等），无报告语义；
- 3：扫完但存在红线违规，报告已打印到 stdout，催人修生成器/模板。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from testpilot_core.generated_swift_lint import (
    AGENT_GENERATED_MARKER,
    GeneratedSwiftLintError,
    scan_generated_swift,
)

_SKIP_DIRS = {".git", "build", "DerivedData", "Pods", ".build", "logs"}


def iter_agent_generated_swift(repo_root: Path) -> dict[str, str]:
    """收集仓内所有 AgentGenerated 目录下的 swift 文件（相对路径 → 文本）。

    选盘条件只有「路径段含 AgentGenerated + .swift 后缀」，不按 target 猜：
    单测目录 Tests/AgentGenerated 与 UI 目录 UITests/AgentGenerated 都受
    同一红线管辖。构建/依赖目录整体跳过。
    """
    files: dict[str, str] = {}
    for path in sorted(repo_root.rglob("*.swift")):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        if AGENT_GENERATED_MARKER not in path.parts:
            continue
        rel = path.relative_to(repo_root).as_posix()
        files[rel] = path.read_text(encoding="utf-8")
    return files


def scan_agent_generated_dir(repo_root: str | Path) -> dict[str, Any]:
    """扫描整个仓的生成目录，返回可 JSON 序列化的汇总报告。"""
    root = Path(repo_root)
    if not root.is_dir():
        raise FileNotFoundError(f"语料仓目录不可访问: {root}")

    files = iter_agent_generated_swift(root)
    violations: list[dict[str, Any]] = []
    for rel_path, text in sorted(files.items()):
        # core 守卫理论上不会触发（选盘已限定目录段），保留 fail-closed 防线
        for violation in scan_generated_swift(rel_path, text):
            violations.append(asdict(violation))

    rule_breakdown: dict[str, int] = {}
    for row in violations:
        rule_breakdown[row["rule_id"]] = rule_breakdown.get(row["rule_id"], 0) + 1
    return {
        "repo_root": str(root),
        "scanned_files": len(files),
        "violation_count": len(violations),
        "rule_breakdown": dict(sorted(rule_breakdown.items())),
        "violations": violations,
    }


def _resolve_repo_arg(arg_repo: str | None) -> Path:
    if arg_repo:
        return Path(arg_repo)
    # 延迟导入：纯函数单测不依赖 bootstrap 及其配置装配
    from ._bootstrap import bootstrap

    container = bootstrap("scan-generated-swift-worker")
    repo_ref = container.get("repo")
    if repo_ref is None:
        raise RuntimeError("容器未装配 repo 坐标，请改用 --repo 显式指定")
    return Path(repo_ref.repo_path)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="TestPilot AgentGenerated Swift 静态红线 worker")
    parser.add_argument("--repo", default=None,
                        help="语料仓根目录；缺省走容器 repo 坐标")
    args = parser.parse_args()

    try:
        repo_root = _resolve_repo_arg(args.repo)
        summary = scan_agent_generated_dir(repo_root)
    except (GeneratedSwiftLintError, RuntimeError, FileNotFoundError,
            OSError, UnicodeDecodeError) as exc:
        print(f"[scan-generated-swift-worker] 扫描被拒: {exc}", file=sys.stderr)
        sys.exit(2)

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if summary["violation_count"]:
        sys.exit(3)
    sys.exit(0)


if __name__ == "__main__":
    main()
