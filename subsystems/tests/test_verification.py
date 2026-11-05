"""live-verify 纯判定测试（docs/08 FR-QA-002）。"""

from testpilot_core.verification import verify_evidence

V = "lexical-swift-1"


def verdict(**kw):
    defaults = dict(
        exists=True,
        indexed_hash="h1",
        current_hash="h1",
        indexed_chunker_version=V,
        current_chunker_version=V,
    )
    defaults.update(kw)
    return verify_evidence(**defaults)


def test_unchanged_verifies():
    v = verdict()
    assert v.verified is True
    assert v.exists is True
    assert v.body_changed is False
    assert v.reason == "unchanged"


def test_stale_index_body_changed_caught():
    v = verdict(indexed_hash="old", current_hash="new")
    assert v.verified is False
    assert v.body_changed is True
    assert v.reason == "body_changed"


def test_missing_symbol_not_degraded_but_unverified():
    v = verdict(exists=False, current_hash=None)
    assert v.verified is False
    assert v.exists is False
    assert v.body_changed is None
    assert v.reason == "symbol_missing"


def test_chunker_version_drift_never_reports_body_changed():
    v = verdict(
        indexed_hash="old", current_hash="new",
        indexed_chunker_version="lexical-swift-0",
    )
    assert v.verified is False
    assert v.body_changed is None
    assert v.reason == "chunker_version_drift"


def test_missing_indexed_chunker_version_is_drift():
    # 旧索引没有版本位：hash 差异不可归因，同样不许误报 body_changed
    v = verdict(indexed_hash="old", current_hash="new", indexed_chunker_version=None)
    assert v.verified is False
    assert v.body_changed is None
    assert v.reason == "chunker_version_drift"
