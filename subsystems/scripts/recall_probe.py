#!/usr/bin/env python3
"""S1.2 三通道召回对照探针（docs/08 FR-KNO-001 AC1：各检索阶段命中率可对比）。

对同一题面分别跑 BM25-only（trigram FTS）、vector-only（query embedding→Chroma）、
hybrid（RRF 融合）三路 TopK，按 --expect 子串判 HIT@K，并汇总每路命中率。
三通道都走生产代码路径（HybridSearchAdapter 的 _fts_recall/_vector_recall/search），
不重复实现检索逻辑；hybrid 一路同时在 retrieval_events 留痕。

内置题集（--cases builtin，不传 query 时默认跑）：
1. 机身键/熄屏→锁屏按键日志：题面与文档词面（锁屏/按键/物理键）零三元串交集，
   BM25 只弱命中调度器符号、目标缺席，向量语义翻盘 @4、hybrid 顶到 #1；
2. 未关闭再响；3. iOS27 侧边键冷启动误挂静音门；
4. EventDetailView 定界负例（作废 SPM 包已排除：任何通道 TopK 出现该符号切块即回归；
   通用中文三元串/语义近邻带来的其他噪声不算回归）。

用法（在 subsystems/ 下）：
  TESTPILOT_REPO_PATH=~/Documents/Code/app-calendar-alarm \
  TESTPILOT_PROFILE=config/profiles/ios-local-poc.yaml \
  .venv/bin/python scripts/recall_probe.py                      # 跑内置题集
  .venv/bin/python scripts/recall_probe.py "自然语言问题" --expect URN片段 [-k 5]
  .venv/bin/python scripts/recall_probe.py --cases builtin -k 5
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages" / "workers" / "src"))

from testpilot_adapters.hybrid_search import HybridSearchAdapter  # noqa: E402
from testpilot_core.retrieval import shape_trigram_query  # noqa: E402
from testpilot_workers._bootstrap import bootstrap  # noqa: E402


@dataclass
class Case:
    name: str
    query: str
    expect: str | None = None  # 命中判定：TopK 的 URN 含此子串即 HIT
    forbid: str | None = None  # 负例回归：TopK 的 URN 一律不得含此子串
    note: str = ""


BUILTIN_CASES = [
    Case(
        name="机身键/熄屏→锁屏按键日志（向量翻盘题）",
        # 与文档词面（锁屏/按键/物理键）零三元串交集：BM25 只弱命中调度器符号、
        # 目标文档缺席；向量靠语义把目标块带进 @5；hybrid 再用 RRF+权威质量分顶到 #1。
        query="不用触屏按机身键时，熄灭的屏幕上如何记录这条操作？",
        expect="锁屏按键日志",
        note="题面与'锁屏键/物理键'零词面交集：BM25 预期 MISS、向量翻盘、hybrid 置顶",
    ),
    Case(
        name="未关闭再响实现与真机验证",
        query="未关闭再响是怎么实现的？真机上怎么验证？",
        expect="未关闭再响实现与真机验证",
    ),
    Case(
        name="iOS27 侧边键冷启动误挂静音门",
        query="iOS27 侧边键冷启动后快照误挂静音门是什么问题？怎么修的？",
        expect="侧边键冷启动",
    ),
    Case(
        name="EventDetailView 不回归（语料定界负例）",
        query="EventDetailView 是怎么把事件传给详情页的？",
        forbid="EventDetailView",
        # 不能要求整句 0 命中：查询里的通用中文三元串（怎么把/事件传/详情页）在
        # trigram OR 语义下会弱命中无关块，向量也会给语义近邻的其他 View——
        # 这些是"无证据问题"的噪声（S1.4 答案层判无证据），不是定界回归。
        # 真正要守的线：作废 SPM 包的 EventDetailView 切块绝不出现在任何通道 TopK。
        note="EventDetailView 属 2026-08 作废 SPM 包（已排除 code_roots）；TopK 出现即回归",
    ),
]


def _short(urn: str) -> str:
    return urn.replace("urn:testpilot:fs-docs:chunk:", "")


async def _channels(adapter: HybridSearchAdapter, query: str, k: int) -> dict:
    """同一题面跑三通道；返回 {channel: [urn,...]/None(通道不可用)}。"""
    match_expr, like_terms = shape_trigram_query(query)
    # 分支跟随装配（project.default_branch；S1.3 起不再写死 main）
    branch = getattr(adapter, "default_branch", "main")
    fts_rank, vec_rank, hybrid = await asyncio.gather(
        adapter._fts_recall(match_expr, like_terms, branch, "default", ()),  # noqa: SLF001
        adapter._vector_recall(query, branch, "default", ()),  # noqa: SLF001
        adapter.search(query, limit=k),
    )
    return {
        "BM25": fts_rank.urns[:k],
        "vector": None if vec_rank.error else vec_rank.urns[:k],
        "vector_error": vec_rank.error,
        "hybrid": [c.urn for c in hybrid.chunks][:k],
        "degraded": hybrid.trace.degraded,
    }


def _judge(case: Case, urns: list[str] | None) -> bool | None:
    if urns is None:
        return None  # 通道不可用：不计入命中率分母
    if case.forbid is not None:
        return not any(case.forbid in u for u in urns)  # 负例：出现禁词即失败
    if case.expect is not None:
        return any(case.expect in u for u in urns)
    return None  # 无判定条件：只展示


async def run_case(adapter: HybridSearchAdapter, case: Case, k: int, verbose: bool) -> dict:
    res = await _channels(adapter, case.query, k)
    print(f"\n{'=' * 78}")
    print(f"题: {case.name}")
    print(f"问: {case.query}")
    if case.note:
        print(f"注: {case.note}")
    if case.forbid is not None:
        label = f"负例：TopK 不得含 {case.forbid!r}"
    elif case.expect is not None:
        label = f"期望 URN 含 {case.expect!r}"
    else:
        label = "无判定（仅展示）"
    print(f"{label}（K={k}）")
    if res["vector_error"]:
        print(f"⚠️  向量通道不可用（降级 FTS-only）：{res['vector_error']}")
    if res["degraded"]:
        print("⚠️  hybrid 本次为降级结果")

    outcomes = {}
    for channel in ("BM25", "vector", "hybrid"):
        urns = res[channel]
        ok = _judge(case, urns)
        outcomes[channel] = ok
        mark = "—" if ok is None else ("✅" if ok else "❌")
        state = "不可用" if urns is None else ("0 命中" if not urns else f"{len(urns)} 条")
        print(f"  {mark} {channel:7s} [{state}]")
        if verbose and urns:
            for i, urn in enumerate(urns[:k], 1):
                if case.forbid is not None:
                    star = " ← 违禁!" if case.forbid in urn else ""
                elif case.expect is not None:
                    star = " ← HIT" if case.expect in urn else ""
                else:
                    star = ""
                print(f"       #{i} {_short(urn)}{star}")
    return outcomes


async def amain() -> int:
    parser = argparse.ArgumentParser(description="S1.2 三通道召回对照探针（BM25 / vector / hybrid）")
    parser.add_argument("query", nargs="?", help="自然语言查询；省略则跑内置题集")
    parser.add_argument("-k", type=int, default=5, help="TopK（默认 5）")
    parser.add_argument("--expect", help="期望命中的 URN 片段（单题模式）")
    parser.add_argument("--cases", choices=["builtin"], help="题集：builtin（默认，无 query 时）")
    parser.add_argument("-q", "--quiet", action="store_true", help="只打印命中行，不列 TopK")
    args = parser.parse_args()

    if args.query is None or args.cases == "builtin":
        cases = BUILTIN_CASES
    else:
        cases = [Case(name="ad-hoc", query=args.query, expect=args.expect)]

    container = bootstrap("recall-probe")
    adapter = container.get("search")
    if not isinstance(adapter, HybridSearchAdapter):
        print(
            "当前 profile 的 search 不是 hybrid（离线/FTS-only profile 无向量通道）。\n"
            "请用：TESTPILOT_PROFILE=config/profiles/ios-local-poc.yaml "
            "TESTPILOT_REPO_PATH=... python scripts/recall_probe.py",
            file=sys.stderr,
        )
        return 2

    # 预检：向量库活着才跑（embed 失败会在通道里显形，但容器没起时给前置提示更友好）
    if not await adapter.vectors.healthcheck():
        print("⚠️  Chroma healthcheck 失败：vector 通道将显示不可用，hybrid 走降级 FTS-only",
              file=sys.stderr)

    tally = {"BM25": [0, 0], "vector": [0, 0], "hybrid": [0, 0]}  # [命中数, 有效题数]
    try:
        for case in cases:
            outcomes = await run_case(adapter, case, args.k, verbose=not args.quiet)
            for ch, hit in outcomes.items():
                if hit is not None:
                    tally[ch][1] += 1
                    tally[ch][0] += int(hit)
    finally:
        container.get("store").close()

    print(f"\n{'=' * 78}")
    print(f"FR-KNO-001 AC1 各阶段 HIT@{args.k} 汇总（{len(cases)} 题；不可用通道不计分母）")
    for ch, (hits, total) in tally.items():
        rate = f"{hits}/{total}" if total else "n/a"
        pct = f"（{hits / total:.0%}）" if total else ""
        print(f"  {ch:7s} {rate}{pct}")
    failed = [ch for ch, (hits, total) in tally.items() if total and hits < total]
    return 1 if failed else 0


def main() -> None:
    sys.exit(asyncio.run(amain()))


if __name__ == "__main__":
    main()
