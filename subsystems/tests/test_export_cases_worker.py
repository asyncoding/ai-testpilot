"""export-cases-worker 与 SQLite 存储集成测试（docs/01 E1，hermetic）。

tmp_path 临时库上播种需求骨架/测试点/正式用例/oracle 草稿，跑 ``run_export``：
钉死默认落盘路径、--out 两种形态、空库 exit 2 不产空壳、同内容两次导出
hash 一致而文件名时间戳不同、cancelled/探索性分区口径。
不依赖语料仓、不碰端口、不装配真容器。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from testpilot_adapters.store import bootstrap_store
from testpilot_core.case_export import CaseExportError
from testpilot_core.enums import OracleType
from testpilot_core.models import Oracle, TestCase, TestPoint
from testpilot_core.oracle_mining import MinedOracle
from testpilot_core.requirement import (
    RequirementAcceptance,
    RequirementFeature,
    RequirementParseResult,
    RequirementRule,
)
from testpilot_workers.export_cases import make_fake_container, run_export

_T0 = datetime(2026, 11, 13, 8, 0, tzinfo=timezone.utc)
_DOC_URN = "urn:testpilot:fs-reqs:requirement:req/alarm.md"
_CASE_URN_A = "urn:testpilot:fs-cases:test_case:cases.md#TC-A1"
_CASE_URN_B = "urn:testpilot:fs-cases:test_case:cases.md#TC-B2"


def _case(case_id: str, urn: str, **over) -> TestCase:
    kwargs = dict(
        urn=urn, external_id=case_id, title=f"用例 {case_id}",
        module="RoutineAlarm", priority="P0", steps=["步骤1"],
        oracles=[Oracle(type=OracleType.STATE_ASSERTION, expectation="快照存在",
                        confidence=0.8)],
        requirement_urn=_DOC_URN)
    kwargs.update(over)
    return TestCase(**kwargs)


def _seed_store(tmp_path):
    store = bootstrap_store(tmp_path / "store")
    req = RequirementParseResult(
        doc_urn=_DOC_URN, external_id="REQ-ALARM-1",
        features=[RequirementFeature(
            urn="urn:fr1", fr_id="FR-1", text="倒计时响铃", priority="P0")],
        rules=[RequirementRule(urn="urn:r1", rule_id="R-1", text="不可静音")],
        acceptance=[RequirementAcceptance(index=0, text="全屏响铃页")],
    )
    store.upsert_requirement_skeleton(_DOC_URN, req, content_hash="h-req")
    store.upsert_test_point(
        _DOC_URN, "TP-1",
        TestPoint(text="重启后设置保留", priority="P0", fr_id="FR-1"),
        content_hash="h-tp")
    store.upsert_test_case(
        _DOC_URN, _case("TC-A1", _CASE_URN_A), content_hash="h-c1")
    store.upsert_test_case(
        _DOC_URN, _case("TC-B2", _CASE_URN_B, exploratory=True,
                        oracle_missing=True, oracles=[]),
        content_hash="h-c2")
    store.upsert_test_case(
        _DOC_URN, _case("TC-C3", "urn:cancelled", lifecycle="cancelled"),
        content_hash="h-c3")
    store.upsert_oracle_suggestion(
        _DOC_URN, _CASE_URN_A, "TC-A1",
        MinedOracle(type=OracleType.LOG_ASSERTION, expectation="ring fire 日志",
                    source_kind="incident", provenance="deterministic",
                    source_refs=["urn:incident:INC-9"], confidence=0.7),
        status="approved", content_hash="h-s1")
    return store


def _container(store, tmp_path):
    return make_fake_container(
        store, profile="ios-local-poc", project="AlarmKitSpike",
        data_dir=tmp_path / "work", default_branch="alarmkit-spike")


def test_default_export_writes_versioned_file_under_exports_dir(tmp_path):
    store = _seed_store(tmp_path)
    summary = run_export(_container(store, tmp_path), now=_T0)

    path = tmp_path / "work" / "exports"
    files = list(path.glob("case-set-*.json"))
    assert len(files) == 1
    assert summary["path"] == str(files[0])
    assert files[0].name == f"case-set-20261113T080000Z-{summary['content_hash'][:8]}.json"

    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert payload["export_schema_version"] == "case-export-v1"
    assert payload["profile"] == "ios-local-poc"
    assert payload["source_branch"] == "alarmkit-spike"
    assert [c["external_id"] for c in payload["cases"]] == ["TC-A1", "TC-B2"]
    assert payload["excluded"]["cancelled_case_ids"] == ["TC-C3"]
    assert payload["requirements"][0]["acceptance_criteria"][0]["id"] == "AC-1"
    mined = payload["cases"][0]["mined_oracle_suggestions"]
    assert len(mined) == 1 and mined[0]["status"] == "approved"
    # 摘要与落盘内容一致
    assert summary["cases_exported"] == 2
    assert summary["cancelled_case_ids"] == ["TC-C3"]
    assert files[0].read_text(encoding="utf-8").endswith("\n")


def test_out_as_file_and_as_directory(tmp_path):
    store = _seed_store(tmp_path)
    explicit = tmp_path / "custom.json"
    summary_file = run_export(_container(store, tmp_path), out=explicit, now=_T0)
    assert summary_file["path"] == str(explicit)
    assert explicit.exists()

    out_dir = tmp_path / "out-dir"
    summary_dir = run_export(_container(store, tmp_path), out=out_dir, now=_T0)
    assert summary_dir["path"].startswith(str(out_dir))
    assert len(list(out_dir.glob("case-set-*.json"))) == 1


def test_repeat_export_same_content_same_hash_distinct_files(tmp_path):
    store = _seed_store(tmp_path)
    first = run_export(_container(store, tmp_path), now=_T0)
    later = run_export(
        _container(store, tmp_path),
        now=datetime(2026, 11, 13, 9, 30, tzinfo=timezone.utc))
    assert first["content_hash"] == later["content_hash"]
    assert first["path"] != later["path"]
    exports = tmp_path / "work" / "exports"
    assert len(list(exports.glob("case-set-*.json"))) == 2


def test_empty_store_raises_and_writes_nothing(tmp_path):
    store = bootstrap_store(tmp_path / "empty-store")
    exports_dir = tmp_path / "work" / "exports"
    with pytest.raises(CaseExportError):
        run_export(_container(store, tmp_path), now=_T0)
    assert not exports_dir.exists() or not list(exports_dir.glob("*.json"))


def test_exclude_exploratory_partition(tmp_path):
    store = _seed_store(tmp_path)
    summary = run_export(_container(store, tmp_path),
                         include_exploratory=False, now=_T0)
    assert summary["cases_exported"] == 1
    assert summary["exploratory_excluded_ids"] == ["TC-B2"]
    export_file = next((tmp_path / "work" / "exports").glob("*.json"))
    payload = json.loads(export_file.read_text(encoding="utf-8"))
    assert [c["external_id"] for c in payload["cases"]] == ["TC-A1"]


def test_missing_store_adapter_is_runtime_error(tmp_path):
    from types import SimpleNamespace

    broken = SimpleNamespace(
        config=SimpleNamespace(
            profile="p",
            project=SimpleNamespace(name="prj", default_branch="main")),
        data_dir=tmp_path, adapters={})
    broken.get = lambda name: None
    with pytest.raises(RuntimeError):
        run_export(broken, now=_T0)
