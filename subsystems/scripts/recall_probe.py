#!/usr/bin/env python3
"""S1.1 召回能力探针（临时工具，S1.2 混合检索落地后删除或降级为调试脚本）。

用途：输入一句自然语言，看当前索引能召回什么，建立 S1.2 之前的基线证据。

已实测确认的分词事实（sqlite 3.54，fts3tokenize 取证，勿再想当然）：
- 生产表 chunks_fts 用 unicode61：**连续汉字段整段是一个 token**，单字只能
  命中标点/Markdown 语法孤立出来的单字，二元短语只能跨 token 边界巧合命中。
  所以查询侧无论怎么整形都救不了自然语言（--unicode61/--raw 是对照组）。
- 默认走 **trigram 模式**：在临时表上用 tokenize='trigram' 对全量切块建索引
  （每次启动重建，1421 块约秒级），查询拆三元串 OR、BM25 排序。这是零三方
  依赖下 CJK 子串召回的可行方案，S1.2 FTS 一路的候选（正式实现为入库建表）。
- 同义/转述（题面"物理键" vs 文档"侧边键/锁屏键"）词面不相交，trigram 也救不了，
  只能靠 S1.2 向量路。

用法（在 subsystems/ 下）：
  TESTPILOT_REPO_PATH=~/Documents/Code/app-calendar-alarm \
  TESTPILOT_PROFILE=config/profiles/ios-local-poc.yaml \
  .venv/bin/python scripts/recall_probe.py "未关闭再响是怎么实现的？" \
    [--expect 未关闭再响] [-k 5] [--unicode61 | --raw]

  --expect 子串  期望命中的 URN 片段（HIT@K/MISS@K 判定，S1.6 golden 雏形）
  --unicode61    走生产 FTS（整形=ASCII 标识符 + 汉字整段精确 token，对照组）
  --raw          不做整形，原句直喂 MATCH（对照：语法错/0 命中）
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages" / "workers" / "src"))

from testpilot_workers._bootstrap import bootstrap  # noqa: E402

_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|[一-鿿]+")


def shape_unicode61(raw: str) -> str:
    """unicode61 下的诚实整形：ASCII 标识符整词 + 汉字段整段精确 token，OR 连接。

    连续汉字段必须与某 token 完全相等才命中（如文档标题前缀里的原词）；
    自然语言整句通常 0 命中——这正是 S1.2 要换分词的证据。
    """
    toks = _TOKEN_RE.findall(raw)
    seen: set[str] = set()
    uniq = [t for t in toks if not (t in seen or seen.add(t))]
    return " OR ".join(uniq)


def shape_trigram(raw: str) -> str:
    """trigram：ASCII 词保留；汉字段取相邻三元串（短于 3 的段退化为整段精确）。"""
    terms: list[str] = []
    for tok in _TOKEN_RE.findall(raw):
        if re.fullmatch(r"[一-鿿]+", tok):
            if len(tok) < 3:
                terms.append(tok)
            else:
                terms.extend(tok[i : i + 3] for i in range(len(tok) - 2))
        else:
            terms.append(tok)
    seen: set[str] = set()
    uniq = [t for t in terms if not (t in seen or seen.add(t))]
    return " OR ".join(uniq)


SQL_RESULTS = """
    SELECT c.urn, c.kind, c.quality_score,
           snippet({fts}, 0, '[', ']', ' … ', 12) AS snip
    FROM {fts} JOIN chunks c ON c.urn = {fts}.chunk_urn
    WHERE {fts} MATCH ? ORDER BY rank LIMIT ?
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="S1.1 召回探针（默认 trigram 模式）")
    parser.add_argument("query", help="自然语言查询")
    parser.add_argument("-k", type=int, default=5, help="TopK（默认 5）")
    parser.add_argument("--expect", help="期望 URN 包含的片段，判定 HIT@K/MISS@K")
    parser.add_argument("--unicode61", action="store_true", help="走生产 FTS + 整形（对照组）")
    parser.add_argument("--raw", action="store_true", help="原句直喂生产 FTS（对照组）")
    args = parser.parse_args()

    container = bootstrap("recall-probe")
    conn = container.adapters["store"].conn

    if args.raw or args.unicode61:
        fts = "chunks_fts"
        expr = args.query if args.raw else shape_unicode61(args.query)
        mode = "raw(unicode61)" if args.raw else "shaped(unicode61)"
    else:
        # 临时 trigram 索引：只在本次连接存活，不污染生产库
        conn.execute("DROP TABLE IF EXISTS temp.tri_fts")
        conn.execute(
            "CREATE VIRTUAL TABLE temp.tri_fts USING fts5(text, chunk_urn UNINDEXED, "
            "tokenize='trigram')"
        )
        conn.execute("INSERT INTO tri_fts(text, chunk_urn) SELECT text, urn FROM chunks")
        fts = "tri_fts"
        expr = shape_trigram(args.query)
        mode = "trigram(temp)"

    print(f"\n模式 : {mode}")
    print(f"查询 : {args.query}")
    print(f"MATCH: {expr[:160]}{' …' if len(expr) > 160 else ''}\n")

    try:
        rows = conn.execute(SQL_RESULTS.format(fts=fts), (expr, args.k)).fetchall()
    except Exception as exc:  # noqa: BLE001 - 探针就是要把语法错显形
        print(f"❌ MATCH 报错（S1.2 检索器需做查询转义/分词）：{type(exc).__name__}: {exc}")
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
