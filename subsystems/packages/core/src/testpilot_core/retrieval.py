"""混合检索融合纯逻辑（docs/08 FR-KNO-001 / FR-ING-003）。

- FTS5(trigram) 与向量通道的 RRF 融合；
- 终排 = RRF 分 × 质量分^质量指数 × 新鲜度衰减（半衰期），每个因子可解释；
- trigram FTS 的查询整形（unicode61 对连续汉字整段成词、trigram 对 <3 字符
  不索引，这两点均经 fts3tokenize/实测确认，见 handoff S1.1.2）。

纯函数、零 IO、零三方依赖；通道编排在 adapters 的 HybridSearchAdapter。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .chunking import parse_swift_arg_labels

if TYPE_CHECKING:
    from .models import KnowledgeChunk

# ASCII 标识符整词；连续汉字段整体取出后再切 trigram
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|[一-鿿]+")

FTS = "fts"
VECTOR = "vector"

# P3 符号路由：题面中的 `Type.member(...)` 精确符号形态。
# - 类型名强制大写开头（Swift 类型约定），小写接收者（变量/函数调用）不认；
# - 前导负向断言防止匹配长点链的后缀段（a.b.Foo.bar(x:) 不允许从 Foo 起匹配）；
# - 参数位不含嵌套括号 [^()]：题面里的 selector 不会带闭包类型，出现嵌套说明是
#   自然语言/复杂表达式，宁可不识别也不误路由。
_SYMBOL_REF_RE = re.compile(
    r"(?<![A-Za-z0-9_.])([A-Z][A-Za-z0-9_]*)\.([A-Za-z_]\w*)\s*\(([^()]*)\)"
)


@dataclass(frozen=True)
class SymbolReference:
    """题面解析出的精确符号引用；normalized_name 与 entities.name 同口径。"""

    type_name: str
    member: str
    normalized_name: str


def parse_symbol_reference(question: str) -> SymbolReference | None:
    """从题面提取唯一的 `Type.member(label:)` 符号引用并归一化到入库 selector 口径。

    返回 None 的情形（全部走常规混合检索，行为与 P3 前完全一致）：
    - 题面没有合规符号形态；括号内不是 Swift 参数标签（自然语言/中文/无冒号表达式）；
    - 出现两个及以上**不同**的符号（题面在做对比，置顶任何一个都是误判）；
      同一符号重复出现仍按唯一引用处理。
    归一化示例：`AlertListViewModel.snoozeRingingMission( minutes : Int )`
    → `AlertListViewModel.snoozeRingingMission(minutes:)`，与 chunker._selector 同形。
    """
    refs = {
        (m.group(1), m.group(2), m.group(3))
        for m in _SYMBOL_REF_RE.finditer(question)
    }
    if len(refs) != 1:
        return None
    type_name, member, args_text = next(iter(refs))
    labels = parse_swift_arg_labels(args_text)
    if labels is None:
        return None
    selector = (
        f"{member}({':'.join(labels) + ':'})" if labels else f"{member}()"
    )
    return SymbolReference(type_name, member, f"{type_name}.{selector}")


@dataclass(frozen=True)
class ChannelRanking:
    """单通道召回结果；urns 已按该通道相关性从高到低排序。"""

    channel: str
    urns: list[str]
    available: bool = True
    degraded: bool = False
    latency_ms: int = 0
    error: str = ""  # 降级原因（不吞异常，随 retrieval_events 留痕）


@dataclass(frozen=True)
class FusedScore:
    urn: str
    total: float
    rrf: float
    quality: float
    freshness: float
    channels: tuple[str, ...]


@dataclass(frozen=True)
class StageTrace:
    """KNO-001 AC1 留痕：各通道可用性、独有/重叠命中、降级标记。"""

    fts_available: bool
    vector_available: bool
    degraded: bool
    fts_urns: list[str] = field(default_factory=list)
    vector_urns: list[str] = field(default_factory=list)
    fts_only: list[str] = field(default_factory=list)
    vector_only: list[str] = field(default_factory=list)
    overlap: list[str] = field(default_factory=list)
    fts_latency_ms: int = 0
    vector_latency_ms: int = 0
    # P3 精确符号路由：entities 精确唯一命中时确定性置顶，绕过向量语义抽奖；
    # 留痕随 retrieval_events.raw_json 持久化，trace 时间线可解释「为什么它排第 1」
    symbol_pinned: bool = False
    symbol_pin_urn: str = ""
    symbol_pin_name: str = ""


@dataclass(frozen=True)
class HybridResult:
    """混合检索终结果（HybridSearchAdapter.search 的返回）。

    chunks 与 scores 一一对应且按终排排序；trace 是 KNO-001 AC1 留痕；
    event_id 为 retrieval_events 行 id（无留痕时为 None）。
    """

    chunks: list[KnowledgeChunk]
    scores: list[FusedScore]
    trace: StageTrace
    event_id: int | None = None

    @property
    def degraded(self) -> bool:
        return self.trace.degraded

    def __iter__(self):
        """向后兼容 KnowledgeSearchPort 的 list 消费方：直接迭代拿到切块。"""
        return iter(self.chunks)

    def __len__(self) -> int:
        return len(self.chunks)

    def __getitem__(self, idx):
        return self.chunks[idx]


def reciprocal_rank_fuse(
    rankings: list[ChannelRanking], *, k: int = 60
) -> list[tuple[str, float, tuple[str, ...]]]:
    """RRF：score(urn) = Σ_channel 1/(k + rank_channel)，rank 从 1 起。

    返回按分降序列表 (urn, score, channels)。不可用通道跳过；
    同分按「命中通道数多者 → 最早通道内名次高者 → 首次出现顺序」稳定排序。
    """
    scores: dict[str, float] = {}
    chans: dict[str, tuple[str, ...]] = {}
    order: dict[str, tuple[int, int]] = {}
    seen: set[str] = set()
    seq = 0
    for r in rankings:
        if not r.available:
            continue
        for rank, urn in enumerate(r.urns, start=1):
            scores[urn] = scores.get(urn, 0.0) + 1.0 / (k + rank)
            chans[urn] = chans.get(urn, ()) + (r.channel,)
            if urn not in seen:
                seen.add(urn)
                order[urn] = (seq, rank)
                seq += 1
    ordered = sorted(
        scores,
        key=lambda u: (-scores[u], -len(chans[u]), order[u][1], order[u][0]),
    )
    return [(u, round(scores[u], 8), chans[u]) for u in ordered]


def freshness_factor(*, age_days: float | None, half_life_days: float | None) -> float:
    """指数衰减 0.5^(age/half_life)；缺年龄或半衰期（不按新鲜度加权）时返回 1。"""
    if age_days is None or half_life_days is None or half_life_days <= 0:
        return 1.0
    if age_days <= 0:
        return 1.0
    return round(0.5 ** (age_days / half_life_days), 6)


def fuse_and_rank(
    rankings: list[ChannelRanking],
    *,
    quality_by_urn: dict[str, float],
    age_days_by_urn: dict[str, float] | None = None,
    rrf_k: int = 60,
    quality_power: float = 1.0,
    half_life_days: float | None = None,
) -> list[FusedScore]:
    """RRF × 质量分^指数 × 新鲜度衰减（FR-ING-003 排序公式）。"""
    age_days_by_urn = age_days_by_urn or {}
    fused: list[FusedScore] = []
    for urn, rrf, channels in reciprocal_rank_fuse(rankings, k=rrf_k):
        q = max(0.0, min(1.0, quality_by_urn.get(urn, 0.5)))
        fresh = freshness_factor(
            age_days=age_days_by_urn.get(urn), half_life_days=half_life_days
        )
        quality_factor = round(q ** quality_power, 6) if quality_power > 0 else 1.0
        total = round(rrf * quality_factor * fresh, 8)
        fused.append(
            FusedScore(
                urn=urn, total=total, rrf=rrf, quality=q, freshness=fresh, channels=channels
            )
        )
    fused.sort(key=lambda f: (-f.total, f.urn))
    return fused


def shape_trigram_query(raw: str) -> tuple[str, list[str]]:
    """trigram FTS 查询整形，返回 (MATCH 表达式, LIKE 兜底短词)。

    - ASCII 标识符：≥3 字符整词进 MATCH（trigram 按子串语义匹配）；1–2 字符
      trigram 不索引（如 LA、go），退化为 SQL LIKE 精确词；
    - 汉字段：≥3 字取相邻三元串；1–2 字同样退化为 LIKE；
    - 标点/FTS 操作符（. : " * 等）在 token 化时天然丢弃。
    """
    match_terms: list[str] = []
    like_terms: list[str] = []
    for tok in _TOKEN_RE.findall(raw):
        is_cjk = bool(re.fullmatch(r"[一-鿿]+", tok))
        if is_cjk and len(tok) >= 3:
            match_terms.extend(tok[i : i + 3] for i in range(len(tok) - 2))
        elif not is_cjk and len(tok) >= 3:
            match_terms.append(tok)
        else:
            like_terms.append(tok)
    dedup: set[str] = set()
    uniq = [t for t in match_terms if not (t in dedup or dedup.add(t))]
    like_dedup: set[str] = set()
    likes = [t for t in like_terms if not (t in like_dedup or like_dedup.add(t))]
    return " OR ".join(uniq), likes


def build_stage_trace(
    fts: ChannelRanking, vector: ChannelRanking
) -> StageTrace:
    """汇总两通道的独有/重叠命中（AC1 各阶段命中率留痕）。"""
    f, v = set(fts.urns), set(vector.urns)
    return StageTrace(
        fts_available=fts.available,
        vector_available=vector.available,
        degraded=fts.degraded or vector.degraded or not vector.available,
        fts_urns=list(fts.urns),
        vector_urns=list(vector.urns),
        fts_only=[u for u in fts.urns if u not in v],
        vector_only=[u for u in vector.urns if u not in f],
        overlap=sorted(f & v),
        fts_latency_ms=fts.latency_ms,
        vector_latency_ms=vector.latency_ms,
    )
