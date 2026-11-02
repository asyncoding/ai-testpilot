#!/usr/bin/env python3
"""core 边界 lint（docs/08 FR-CFG-002 AC1，S0 门禁）。

testpilot_core 禁止 import 任何 IO/框架/SDK 与兄弟子系统，
只允许标准库、pydantic、包内相对/绝对导入。
CI 与 tests/test_import_lint.py 都会执行本脚本。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

FORBIDDEN_TOP_LEVEL = {
    "fastapi",
    "uvicorn",
    "yaml",
    "requests",
    "httpx",
    "redis",
    "chromadb",
    "langfuse",
    "pytest",
    "sqlite3",
    "subprocess",
    "socket",
    "aiohttp",
}
FORBIDDEN_TESTPILOT = {"testpilot_adapters", "testpilot_orchestrator", "testpilot_workers"}


def scan(core_dir: Path) -> list[str]:
    violations: list[str] = []
    for py in sorted(core_dir.rglob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in ast.walk(tree):
            module = ""
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                names = [module] if node.level == 0 else []
            else:
                continue
            for name in names:
                top = name.split(".")[0]
                if top in FORBIDDEN_TOP_LEVEL or name.split(".")[0] in FORBIDDEN_TESTPILOT:
                    violations.append(f"{py}:{node.lineno} 禁止导入 {name}")
    return violations


def main() -> int:
    core_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1] / "packages/core/src/testpilot_core"
    violations = scan(core_dir)
    if violations:
        print("testpilot-core 边界 lint 失败：")
        for v in violations:
            print(f"  {v}")
        return 1
    print(f"OK: {core_dir} 未发现越界导入")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
