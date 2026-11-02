"""ingest-worker：连接器拉取→归一化→评分→实体链接→切块嵌入（S1 起填充）。"""

from __future__ import annotations

from ._bootstrap import bootstrap


def main() -> None:
    container = bootstrap("ingest-worker")
    # S0 仅装配自检；S1 起：消费事件 → 跑加工管线 → 写 chunks/entities/relations
    docs = container.get("docs")
    print(f"[ingest-worker] ready; docs_adapter={type(docs).__name__ if docs else None}")


if __name__ == "__main__":
    main()
