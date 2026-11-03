#!/usr/bin/env python3
"""S1.1 召回能力探针（临时工具，S1.2 混合检索落地后删除或降级为调试脚本）。

用途：输入一句自然语言，看当前 FTS5 一路能召回什么，建立 S1.2 之前的基线证据。

背景：chunks_fts 用 unicode61 分词（中文=逐字成词）；store.search_fts 把查询
原样喂给 MATCH，自然语言句子里的标点（. : ? 等）会触发 FTS 语法错、中文整句
做隐式 AND 又近乎必 0 命中。本探针在适配器外做最小查询整形（标点剔除 +
逐词 OR，排序仍交 BM25 rank），只服务于验证，不进产品链路。

用法（在 subsystems/ 下）：
  TESTPILOT_REPO_PATH=~/Documents/Code/app-calendar-alarm \
  TESTPILOT_PROFILE=config/profiles/ios-local-poc.yaml \
  .venv/bin/python scripts/recall_probe.py "未关闭再响是怎么实现的？" \
    [--expect 未关闭再响实现] [-k 5] [--raw]

  --expect 子串  期望命中的 URN 片段（HIT@K/MISS@K 判定，S1.6 golden 雏形）
  --raw          不做整形，直接把原句喂 MATCH（对照：看语法错/0 命中现象）
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages" / "workers" / "src"))

from testpilot_workers._bootstrap import bootstrap  # noqa: E402

# unicode61：ASCII 标识符整词、CJK 逐字；其余字符（标点/FTS 操作符 . : " * 等）全部丢弃
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|[一-鿿]")


def shape_query(raw: str) -> str:
    tokens = _TOKEN_RE.findall(raw)
    # 去重保序（同字重复只会扭曲 BM25 词频）
    seen: set[str] = set()
    uniq = [t for t in tokens if not (t in seen or seen.add(t))]
    return " OR ".join(uniq)


def main() -> None:
    parser = argparse.ArgumentParser(description="S1.1 FTS 召回探针")
    parser.add_argument("query", help="自然语言查询")
    parser.add_argument("-k", type=int, default=5, help="TopK（默认 5）")
    parser.add_argument("--expect", help="期望 URN 包含的片段，判定 HIT@K/MISS@K")
    parser.add_argument("--raw", action="store_true", help="原样喂 MATCH（对照实验）")
    args = parser.parse_args()

    container = bootstrap("recall-probe")
    conn = container.adapters["store"].conn

    match_expr = args.query if args.raw else shape_query(args.query)
    print(f"\n查询原文 : {args.query}")
    print(f"MATCH 表达式: {match_expr!r}\n")

    try:
        rows = conn.execute(
            """SELECT c.urn, c.kind, c.quality_score, c.doc_urn,
                      snippet(chunks_fts, 0, '[', ']', ' … ', 12) AS snip
               FROM chunks_fts JOIN chunks c ON c.urn = chunks_fts.chunk_urn
               WHERE chunks_fts MATCH ? ORDER BY rank LIMIT ?""",
            (match_expr, args.k),
        ).fetchall()
    except Exception as exc:  # noqa: BLE001 - 探针就是要把语法错显形
        print(f"❌ MATCH 报错（S1.2 检索器需做查询转义/整形）：{type(exc).__name__}: {exc}")
        sys.exit(2)

    if not rows:
        print("（0 命中）")
    for i, r in enumerate(rows, 1):
        short = r["urn"].replace("urn:testpilot:fs-docs:chunk:", "")
        print(f"#{i} [{r['kind']}] q={r['quality_score']:.2f} {short}")
        print(f"    {r['snip']}")

    if args.expect:
        hit = any(args.expect in r["urn"] for r in rows)
        print(f"\n判定: {'✅ HIT@%d' % args.k if hit else '❌ MISS@%d' % args.k}（期望片段 {args.expect!r}）")


if __name__ == "__main__":
    main()
