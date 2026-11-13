"""需求文档连接器与骨架抽取管线测试（docs/08 FR-TD-001，S2.2）。

合成夹具写 tmp_path，不依赖语料仓；真档全量验证由 scripts/requirement_probe.py
在本机指向 app-calendar-alarm 仓执行，不进自动门禁。
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from testpilot_adapters.recorded_model import RecordedModelGateway
from testpilot_adapters.requirement_docs import (
    SOURCE_SYSTEM,
    RequirementDocumentAdapter,
)
from testpilot_adapters.store import bootstrap_store
from testpilot_core import SCHEMA_VERSION
from testpilot_core.errors import AdapterUnavailableError
from testpilot_workers.requirement_extraction import run_requirement_extraction

FIXTURE_DOC = (
    Path(__file__).parent / "fixtures" / "requirements" / "synthetic-requirements.md"
)
MODEL_FIXTURE = Path(__file__).parent / "fixtures" / "model" / "responses.json"

_GOOD_DOC = """# 合成需求

## 1. 功能需求（FR）

### 1.1 模块一

| ID | 需求 | 优先级 |
|---|---|---|
| FR-1 | 功能一 | P0 |

## 2. 非功能需求（NFR）

| ID | 需求 |
|---|---|
| NFR-1 | 规则一 |

## 3. 验收标准

1. FR-1 必须可观测、可回滚。
"""

_DAMAGED_DOC = """# 损坏表

## 1. 功能需求（FR）

| ID | 需求 | 优先级 |
|---|---|---|
| FR-9 | 正常行 | P0 |
| FR-8 | 缺一列 |
"""


class _FakeContainer:
    def __init__(self, adapter, store, model=None):
        self._adapters = {"requirements": adapter, "store": store}
        if model is not None:
            self._adapters["model"] = model

    def get(self, name: str):
        return self._adapters.get(name)


def _run(container, **kwargs):
    return asyncio.run(run_requirement_extraction(container, **kwargs))


def _write(root: Path, name: str, text: str) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_adapter_parses_doc_and_carries_doc_urn(tmp_path):
    _write(tmp_path, "req/v1.md", _GOOD_DOC)
    adapter = RequirementDocumentAdapter([tmp_path])
    docs, errors = adapter.load()
    assert errors == [] and len(docs) == 1
    doc = docs[0]
    assert doc.external_id == f"{tmp_path.name}/req/v1.md"
    assert doc.doc_urn.startswith("urn:testpilot:fs-reqs:requirement_doc:")
    assert [f.fr_id for f in doc.result.features] == ["FR-1"]
    assert [r.rule_id for r in doc.result.rules] == ["NFR-1"]


def test_adapter_excludes_glob_and_missing_root_fails_loud(tmp_path):
    _write(tmp_path, "keep/v1.md", _GOOD_DOC)
    _write(tmp_path, "skip/v2.md", _GOOD_DOC)
    adapter = RequirementDocumentAdapter([tmp_path], excludes=["*/skip/*"])
    docs, _ = adapter.load()
    assert len(docs) == 1 and "keep" in docs[0].external_id

    with pytest.raises(AdapterUnavailableError):
        RequirementDocumentAdapter([tmp_path / "no-such-root"]).load()


def test_adapter_accepts_single_file_root_with_parent_alias(tmp_path):
    """单文件 root（xcode profile 直指语料仓 V1 文档）：只收该文件，
    alias 取父目录名，external_id 与「目录 root 扫到同文件」形态完全一致。"""
    target = _write(tmp_path, "docs/V1需求文档.md", _GOOD_DOC)
    _write(tmp_path, "docs/other.md", _GOOD_DOC)  # 同目录噪声不得被扫入
    adapter = RequirementDocumentAdapter([target])
    docs, errors = adapter.load()
    assert errors == [] and len(docs) == 1
    assert docs[0].external_id == "docs/V1需求文档.md"
    assert [f.fr_id for f in docs[0].result.features] == ["FR-1"]

    with pytest.raises(AdapterUnavailableError):
        RequirementDocumentAdapter([tmp_path / "docs" / "missing.md"]).load()


def test_unreadable_file_is_isolated_as_io_error(tmp_path):
    target = _write(tmp_path, "v1.md", _GOOD_DOC)
    os.chmod(target, 0o000)
    try:
        if os.access(target, os.R_OK):  # root 等场景权限位不生效，跳过而不是假绿
            pytest.skip("当前用户绕过文件权限，无法模拟不可读")
        docs, errors = RequirementDocumentAdapter([tmp_path]).load()
    finally:
        os.chmod(target, 0o644)
    assert docs == [] and len(errors) == 1
    assert "PermissionError" in errors[0]["error"]


def test_deterministic_extraction_persists_skeleton_and_dead_letters(tmp_path):
    req_root = tmp_path / "requirements"
    _write(req_root, "good.md", _GOOD_DOC)
    _write(req_root, "damaged.md", _DAMAGED_DOC)
    store = bootstrap_store(tmp_path / "data")
    adapter = RequirementDocumentAdapter([req_root])
    try:
        report = _run(_FakeContainer(adapter, store))
        assert report["docs_seen"] == 2
        assert report["features_active"] == 2  # FR-1 + FR-9
        assert report["rules"] == 1
        assert report["acceptance_criteria"] == 1
        assert report["ac_missing"] == 1  # FR-9 无 AC 覆盖
        assert report["parse_dead_letters"] == 1  # FR-8 列数损坏
        assert report["status"] == "partial"
        assert store.count_requirement_skeletons() == 2

        rows = store.conn.execute(
            "SELECT source, error FROM dead_letters WHERE source=?", (SOURCE_SYSTEM,)
        ).fetchall()
        assert len(rows) == 1 and "列数" in rows[0]["error"]

        results = store.list_requirement_skeletons()
        skeleton = next(r for r in results if r.external_id.endswith("good.md"))
        assert skeleton.linking_source == "deterministic"
        # raw_json 完整还原，且骨架能再走一遍 pydantic 校验（结构合法率）
        dumped = skeleton.to_skeleton().model_dump()
        assert dumped["features"] and dumped["acceptance_criteria"]
    finally:
        store.close()


def test_fixture_doc_deterministic_counts(tmp_path):
    """钉死合成夹具的确定性盘点数字（A11 门禁同源口径）。"""
    req_root = tmp_path / "requirements"
    _write(req_root, "synthetic-requirements.md", FIXTURE_DOC.read_text(encoding="utf-8"))
    store = bootstrap_store(tmp_path / "data")
    adapter = RequirementDocumentAdapter([req_root])
    try:
        report = _run(_FakeContainer(adapter, store))
        assert report == {
            "status": "partial",
            "docs_seen": 1,
            "skipped_unchanged": 0,
            "docs_rebuilt": 1,
            "features_active": 6,
            "features_cancelled": 1,
            "rules": 2,
            "acceptance_criteria": 2,
            "ac_missing": 3,
            "gaps": 6,
            "model_linked_docs": 0,
            "link_degraded_docs": 0,
            "parse_dead_letters": 1,
            "io_errors": 0,
        }
    finally:
        store.close()


def test_incremental_skips_unchanged_and_full_rebuilds(tmp_path):
    req_root = tmp_path / "requirements"
    _write(req_root, "v1.md", _GOOD_DOC)
    store = bootstrap_store(tmp_path / "data")
    adapter = RequirementDocumentAdapter([req_root])
    try:
        first = _run(_FakeContainer(adapter, store))
        assert first["docs_rebuilt"] == 1 and first["skipped_unchanged"] == 0

        second = _run(_FakeContainer(adapter, store))
        assert second["docs_rebuilt"] == 0 and second["skipped_unchanged"] == 1
        assert store.count_requirement_skeletons() == 1

        full = _run(_FakeContainer(adapter, store), full=True)
        assert full["docs_rebuilt"] == 1 and store.count_requirement_skeletons() == 1
    finally:
        store.close()


def test_reingestion_replaces_whole_doc_without_ghost_features(tmp_path):
    req_root = tmp_path / "requirements"
    _write(req_root, "v1.md", _GOOD_DOC)  # FR-1
    store = bootstrap_store(tmp_path / "data")
    adapter = RequirementDocumentAdapter([req_root])
    try:
        _run(_FakeContainer(adapter, store))
        assert store.count_requirement_skeletons() == 1

        # 文档改版删掉 FR 表：重灌后旧骨架不得残留
        _write(req_root, "v1.md", "# 只剩背景\n\n## 1. 背景\n\n无需求。\n")
        report = _run(_FakeContainer(adapter, store))
        assert report["docs_rebuilt"] == 1
        result = store.list_requirement_skeletons()[0]
        assert result.features == [] and result.rules == []
    finally:
        store.close()


def test_missing_adapter_raises_and_stub_profile_safe(tmp_path):
    store = bootstrap_store(tmp_path / "data")
    try:
        with pytest.raises(ValueError, match="requirements"):
            _run(_FakeContainer(None, store))
    finally:
        store.close()


def test_recorded_model_linking_closes_gaps_and_is_persisted(tmp_path):
    # 根目录名必须是 requirements，external_id 才能对上录制 key
    req_root = tmp_path / "requirements"
    _write(req_root, "synthetic-requirements.md",
           FIXTURE_DOC.read_text(encoding="utf-8"))
    store = bootstrap_store(tmp_path / "data")
    adapter = RequirementDocumentAdapter([req_root])
    model = RecordedModelGateway(MODEL_FIXTURE)
    try:
        report = _run(_FakeContainer(adapter, store, model=model))
        assert report["model_linked_docs"] == 1
        assert report["link_degraded_docs"] == 0
        # 模型补链 FR-2a/FR-6 后，只剩 FR-3 缺 AC
        assert report["ac_missing"] == 1
        result = store.list_requirement_skeletons()[0]
        assert result.linking_source == "model"
        covered = {fid for ac in result.acceptance for fid in ac.linked_fr_ids}
        assert {"FR-1", "FR-2", "FR-2a", "FR-5", "FR-6"} <= covered
    finally:
        store.close()


def test_missing_recorded_key_degrades_to_deterministic_without_fabrication(tmp_path):
    req_root = tmp_path / "requirements"
    _write(req_root, "other.md", _GOOD_DOC)  # 无 req_link:requirements/other.md 录制
    store = bootstrap_store(tmp_path / "data")
    adapter = RequirementDocumentAdapter([req_root])
    model = RecordedModelGateway(MODEL_FIXTURE)
    try:
        report = _run(_FakeContainer(adapter, store, model=model))
        assert report["link_degraded_docs"] == 1
        assert report["model_linked_docs"] == 0
        result = store.list_requirement_skeletons()[0]
        # 降级时确定性链接原样保留，缺口不被模型「补齐」
        assert result.linking_source == "deterministic"
        assert result.acceptance[0].linked_fr_ids == ["FR-1"]
    finally:
        store.close()


def test_legacy_database_bootstraps_s2_tables(tmp_path):
    """老库平滑升级：v8 库没有 S2 表，bootstrap 幂等补建 skeletons(v9)/test_points(v10)。"""
    db_dir = tmp_path / "data"
    store = bootstrap_store(db_dir)
    store.conn.execute("DROP TABLE requirement_skeletons")
    store.conn.execute("DROP TABLE test_points")
    store.conn.execute("UPDATE meta SET value='8' WHERE key='schema_version'")
    store.close()

    upgraded = bootstrap_store(db_dir)
    try:
        assert upgraded.get_meta("schema_version") == str(SCHEMA_VERSION) == "15"
        tables = {
            r["name"] for r in upgraded.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "requirement_skeletons" in tables
        assert "test_points" in tables
        assert upgraded.count_requirement_skeletons() == 0
        assert upgraded.count_test_points() == 0
    finally:
        upgraded.close()
