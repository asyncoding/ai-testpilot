"""runner-worker：独立进程执行测试、超时隔离（S3 接 xcodebuild）。"""

from __future__ import annotations

from ._bootstrap import bootstrap


def main() -> None:
    container = bootstrap("runner-worker")
    # S0 仅装配自检；S3 起：从队列领 TestExecutionRequest → runner adapter → 采制品/日志 → 回写 TestRun
    runner = container.get("runner")
    print(f"[runner-worker] ready; runner_adapter={type(runner).__name__ if runner else None}")


if __name__ == "__main__":
    main()
