"""eval-worker：golden set 离线评测（S1 起填充真实评测）。"""

from __future__ import annotations

from ._bootstrap import bootstrap


def main() -> None:
    container = bootstrap("eval-worker")
    print(f"[eval-worker] ready; store={type(container.get('store')).__name__}")


if __name__ == "__main__":
    main()
