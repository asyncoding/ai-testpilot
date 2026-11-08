"""eval-worker：独立进程装配自检（verify_s0 A5）。

真正的 golden set 评测在 scripts/eval_probe.py（recorded/ollama 双形态，
FR-EV-002），与 qa_probe.py 同模式：worker 保持轻量装配面，探针承载端到端链路，
避免 A5 门禁耦合完整入库与图执行。
"""

from __future__ import annotations

from ._bootstrap import bootstrap


def main() -> None:
    container = bootstrap("eval-worker")
    print(f"[eval-worker] ready; store={type(container.get('store')).__name__}")
    container.get("store").close()


if __name__ == "__main__":
    main()
