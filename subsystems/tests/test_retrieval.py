"""混合检索融合纯函数测试（docs/08 FR-KNO-001 / FR-ING-003）。"""

import pytest

from testpilot_core.retrieval import (
    FTS,
    VECTOR,
    ChannelRanking,
    build_stage_trace,
    freshness_factor,
    fuse_and_rank,
    reciprocal_rank_fuse,
    shape_trigram_query,
)


# ── RRF ──────────────────────────────────────────────────────────────────────

def test_rrf_single_channel_rank_order():
    r = ChannelRanking(FTS, ["a", "b", "c"])
    out = reciprocal_rank_fuse([r], k=60)
    assert [u for u, _, _ in out] == ["a", "b", "c"]
    assert out[0][1] == pytest.approx(1 / 61)


def test_rrf_overlap_beats_single_channel_and_unavailable_skipped():
    fts = ChannelRanking(FTS, ["only-fts", "shared"])
    vec = ChannelRanking(VECTOR, ["shared", "only-vec"])
    dead = ChannelRanking("other", ["x"], available=False)
    out = reciprocal_rank_fuse([fts, vec, dead], k=60)
    urns = [u for u, _, _ in out]
    assert urns[0] == "shared"  # 两通道各计一次分
    assert set(urns) == {"shared", "only-fts", "only-vec"}
    shared = next(s for u, s, _ in out if u == "shared")
    assert shared == pytest.approx(1 / 62 + 1 / 61)
    assert next(c for u, _, c in out if u == "shared") == (FTS, VECTOR)


def test_rrf_tie_is_stable_by_first_appearance():
    # 两条互不重叠的单通道结果，RRF 分相同 → 按首次出现顺序
    fts = ChannelRanking(FTS, ["a"])
    vec = ChannelRanking(VECTOR, ["b"])
    out = reciprocal_rank_fuse([fts, vec], k=60)
    assert [u for u, _, _ in out] == ["a", "b"]


# ── 新鲜度与终排 ───────────────────────────────────────────────────────────────

def test_freshness_half_life():
    assert freshness_factor(age_days=10, half_life_days=10) == pytest.approx(0.5)
    assert freshness_factor(age_days=0, half_life_days=10) == 1.0
    assert freshness_factor(age_days=None, half_life_days=10) == 1.0
    assert freshness_factor(age_days=30, half_life_days=None) == 1.0


def test_fuse_and_rank_quality_can_demote():
    fts = ChannelRanking(FTS, ["hi", "lo"])
    vec = ChannelRanking(VECTOR, ["hi"])
    out = fuse_and_rank(
        [fts, vec],
        quality_by_urn={"hi": 0.9, "lo": 0.1},
        rrf_k=60,
    )
    assert out[0].urn == "hi"
    lo = next(f for f in out if f.urn == "lo")
    assert lo.quality == 0.1
    # total = rrf × 质量（total 保留 8 位小数）
    assert lo.total == pytest.approx(lo.rrf * 0.1, abs=1e-7)


def test_fuse_and_rank_freshness_orders_equal_rrf():
    fts = ChannelRanking(FTS, ["new", "old"])
    out = fuse_and_rank(
        [fts],
        quality_by_urn={"new": 0.8, "old": 0.8},
        age_days_by_urn={"new": 0.0, "old": 30.0},
        half_life_days=10.0,
    )
    assert [f.urn for f in out] == ["new", "old"]
    old = next(f for f in out if f.urn == "old")
    assert old.freshness == pytest.approx(0.125)  # 0.5^3


def test_fuse_and_rank_missing_quality_defaults_half():
    out = fuse_and_rank(
        [ChannelRanking(FTS, ["x"])], quality_by_urn={}, half_life_days=10
    )
    assert out[0].quality == 0.5
    assert out[0].freshness == 1.0  # 无年龄信息不衰减


# ── trigram 查询整形 ──────────────────────────────────────────────────────────

def test_shape_cjk_sentence_into_trigrams():
    expr, likes = shape_trigram_query("未关闭再响？")
    assert expr == "未关闭 OR 关闭再 OR 闭再响"
    assert likes == []


def test_shape_short_ascii_and_cjk_go_to_like():
    expr, likes = shape_trigram_query("LA 与 go 闹钟")
    assert likes == ["LA", "与", "go", "闹钟"]  # 1–2 字词 trigram 不索引
    assert expr == ""


def test_shape_long_ascii_identifier_kept_and_punct_dropped():
    expr, likes = shape_trigram_query("MissionCoordinator.schedulePrimary: 再试一次?")
    terms = expr.split(" OR ")
    assert "MissionCoordinator" in terms and "schedulePrimary" in terms
    assert "再试一" in terms and "试一次" in terms
    assert "." not in expr and ":" not in expr and "?" not in expr
    assert likes == []


def test_shape_dedup_repeats():
    expr, _ = shape_trigram_query("未关闭未关闭")
    assert expr.split(" OR ").count("未关闭") == 1


# ── 阶段留痕 ──────────────────────────────────────────────────────────────────

def test_stage_trace_partitions_and_degraded():
    fts = ChannelRanking(FTS, ["a", "b"])
    vec = ChannelRanking(VECTOR, ["b", "c"])
    t = build_stage_trace(fts, vec)
    assert t.fts_only == ["a"]
    assert t.vector_only == ["c"]
    assert t.overlap == ["b"]
    assert t.degraded is False

    dead_vec = ChannelRanking(VECTOR, [], available=False)
    t2 = build_stage_trace(fts, dead_vec)
    assert t2.degraded is True
    assert t2.vector_available is False
