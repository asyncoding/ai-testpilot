"""用例文档连接器与入库管线测试（docs/08 FR-CON-003，S2.1）。

合成夹具写 tmp_path，不依赖语料仓；真档全量验证由 scripts/case_parse_probe.py
在本机指向 app-calendar-alarm 仓执行，不进自动门禁。
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from testpilot_adapters.case_docs import SOURCE_SYSTEM, CaseDocumentAdapter
from testpilot_adapters.store import bootstrap_store
from testpilot_core import SCHEMA_VERSION
from testpilot_core.errors import AdapterUnavailableError
from testpilot_workers.case_ingestion import run_case_ingestion

_GOOD_DOC = """# 合成

## 模块 A

### TC-A1 标题一（FR-1）P0

1. 步骤一
2. 步骤二

**预期**：

- 状态字段刷新

### TC-A2 无预期 P1

1. 只有步骤
"""

_DAMAGED_DOC = """# 损坏表

## 模块 B

| ID | 步骤要点 | 预期 | 优先级 |
|---|---|---|---|
| TC-B1 | 正常行 | 正常预期 | P0 |
| TC-B2 | 缺一列 | P1 |
"""


class _FakeContainer:
    def __init__(self, adapter, store):
        self._adapters = {"cases": adapter, "store": store}

    def get(self, name: str):
        return self._adapters.get(name)


def _write(root: Path, name: str, text: str) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_adapter_parses_doc_and_carries_doc_urn(tmp_path):
    _write(tmp_path, "cases/v1.md", _GOOD_DOC)
    adapter = CaseDocumentAdapter([tmp_path])
    docs, errors = adapter.load()
    assert errors == [] and len(docs) == 1
    doc = docs[0]
    assert doc.external_id == f"{tmp_path.name}/cases/v1.md"
    assert doc.doc_urn.startswith("urn:testpilot:fs-cases:document:")
    assert len(doc.result.cases) == 2
    ids = {c.external_id for c in doc.result.cases}
    assert ids == {"TC-A1", "TC-A2"}
    missing = next(c for c in doc.result.cases if c.external_id == "TC-A2")
    assert missing.oracle_missing is True


def test_adapter_excludes_glob_and_missing_root_fails_loud(tmp_path):
    _write(tmp_path, "keep/v1.md", _GOOD_DOC)
    _write(tmp_path, "skip/v2.md", _GOOD_DOC)
    adapter = CaseDocumentAdapter([tmp_path], excludes=["*/skip/*"])
    docs, _ = adapter.load()
    assert len(docs) == 1 and "keep" in docs[0].external_id

    with pytest.raises(AdapterUnavailableError):
        CaseDocumentAdapter([tmp_path / "no-such-root"]).load()


def test_unreadable_file_is_isolated_as_io_error(tmp_path):
    target = _write(tmp_path, "v1.md", _GOOD_DOC)
    os.chmod(target, 0o000)
    try:
        if os.access(target, os.R_OK):  # root 等场景下权限位不生效，跳过而不是假绿
            pytest.skip("当前用户绕过文件权限，无法模拟不可读")
        docs, errors = CaseDocumentAdapter([tmp_path]).load()
    finally:
        os.chmod(target, 0o644)
    assert docs == [] and len(errors) == 1
    assert "PermissionError" in errors[0]["error"]


def test_ingestion_persists_cases_and_parse_dead_letters(tmp_path):
    _write(tmp_path / "cases", "good.md", _GOOD_DOC)
    _write(tmp_path / "cases", "damaged.md", _DAMAGED_DOC)
    store = bootstrap_store(tmp_path / "data")
    adapter = CaseDocumentAdapter([tmp_path / "cases"])
    try:
        report = run_case_ingestion(_FakeContainer(adapter, store))
        assert report["docs_seen"] == 2
        assert report["cases_upserted"] == 3  # 2 好文档用例 + 损坏文档里唯一好行
        assert report["oracle_missing"] == 1
        assert report["parse_dead_letters"] == 1  # 列数损坏行进死信
        assert report["status"] == "partial"
        assert store.count_test_cases() == 3

        rows = store.conn.execute(
            "SELECT source, error FROM dead_letters WHERE source=?", (SOURCE_SYSTEM,)
        ).fetchall()
        assert len(rows) == 1 and "列数" in rows[0]["error"]

        active = store.list_test_cases(include_cancelled=False)
        assert {c.external_id for c in active} == {"TC-A1", "TC-A2", "TC-B1"}
        # raw_json 完整还原结构化模型（oracle 类型/来源回链不丢）
        a1 = next(c for c in active if c.external_id == "TC-A1")
        assert a1.oracles and a1.oracles[0].source_refs
    finally:
        store.close()


def test_ingestion_incremental_skips_unchanged_and_full_rebuilds(tmp_path):
    case_root = tmp_path / "cases"
    _write(case_root, "v1.md", _GOOD_DOC)
    store = bootstrap_store(tmp_path / "data")
    adapter = CaseDocumentAdapter([case_root])
    try:
        first = run_case_ingestion(_FakeContainer(adapter, store))
        assert first["docs_rebuilt"] == 1 and first["skipped_unchanged"] == 0

        second = run_case_ingestion(_FakeContainer(adapter, store))
        assert second["docs_rebuilt"] == 0 and second["skipped_unchanged"] == 1
        assert store.count_test_cases() == 2

        full = run_case_ingestion(_FakeContainer(adapter, store), full=True)
        assert full["docs_rebuilt"] == 1 and store.count_test_cases() == 2
    finally:
        store.close()


def test_reingestion_replaces_whole_doc_set_without_ghost_cases(tmp_path):
    case_root = tmp_path / "cases"
    _write(case_root, "v1.md", _GOOD_DOC)  # 含 TC-A1/TC-A2
    store = bootstrap_store(tmp_path / "data")
    adapter = CaseDocumentAdapter([case_root])
    try:
        run_case_ingestion(_FakeContainer(adapter, store))
        assert store.count_test_cases() == 2

        # 文档改版删掉 TC-A2：重灌后旧用例不得残留（FR-ING-001 AC3 同构纪律）
        _write(case_root, "v1.md", _GOOD_DOC.split("### TC-A2")[0])
        report = run_case_ingestion(_FakeContainer(adapter, store))
        assert report["docs_rebuilt"] == 1
        remaining = {c.external_id for c in store.list_test_cases()}
        assert remaining == {"TC-A1"}
    finally:
        store.close()


def test_missing_adapter_raises_and_stub_profile_safe(tmp_path):
    store = bootstrap_store(tmp_path / "data")
    try:
        with pytest.raises(ValueError, match="cases"):
            run_case_ingestion(_FakeContainer(None, store))
    finally:
        store.close()


def test_v7_database_bootstraps_test_cases_table(tmp_path):
    """老库平滑升级：v7 库没有 test_cases 表，当前版 bootstrap 幂等补建。"""
    db_dir = tmp_path / "data"
    store = bootstrap_store(db_dir)
    store.conn.execute("DROP TABLE test_cases")
    store.conn.execute("UPDATE meta SET value='7' WHERE key='schema_version'")
    store.close()

    upgraded = bootstrap_store(db_dir)
    try:
        assert upgraded.get_meta("schema_version") == str(SCHEMA_VERSION) == "14"
        tables = {
            r["name"] for r in upgraded.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "test_cases" in tables
        assert upgraded.count_test_cases() == 0
    finally:
        upgraded.close()
