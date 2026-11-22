"""impact-rerun-worker 集成测试（docs/01 E4，hermetic）。

tmp 真 sqlite（store + SqliteQueue）+ 合成语料 + fake scm + 落盘 automation-map：
钉死 analyze_impact→map 鲜度守卫→only_testing 翻译→submit_execution 全链、
幂等重放 jobs 零增生、dry-run 不写账本、空选择/过期 map fail-closed、
CLI 退出码 0/2 两档。不碰真语料仓、不起 git 进程、不占端口。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import pytest

from testpilot_adapters.store import SqliteQueue, bootstrap_store
from testpilot_core.automation_mapping import AUTOMATION_MAP_SCHEMA_VERSION
from testpilot_core.case_relations import RELATION_SCOPE_URN, CaseRelation, case_relation_urn
from testpilot_core.enums import AuthorityLevel, ChangeType, RunStatus
from testpilot_core.errors import AdapterUnavailableError
from testpilot_core.impact_execution import ImpactSelectionError
from testpilot_core.models import (
    ChangeSet,
    KnowledgeChunk,
    RepoRef,
    SourceDocument,
    SymbolChange,
    TestCase,
)
from testpilot_core.urn import make_urn
from testpilot_workers import impact_rerun as ir
from testpilot_workers.impact_rerun import make_fake_container, run_impact_rerun

_SOURCE = "fs-docs"
GIT_PATH = "Sources/Alarm.swift"
EXT = f"code/{GIT_PATH}"
DOC_URN = make_urn(_SOURCE, "document", EXT)
BASE = "1" * 40
HEAD = "2" * 40
MOD_ANCHOR = "AlarmScheduler.schedulePrimary()"
REM_ANCHOR = "AlarmScheduler.legacyAlarmBell()"
MOD_URN = make_urn(_SOURCE, "symbol", f"{EXT}#{MOD_ANCHOR}")
REM_URN = make_urn(_SOURCE, "symbol", f"{EXT}#{REM_ANCHOR}")

CASE_RUN = "TC-RUN"
CASE_CONF = "TC-CONF"
CASE_EXP = "TC-EXP"
CASE_UNMAPPED = "TC-N2"
OID_RUN = "AlarmKitSpikeTests/AgentTests/test_TCRUN_ring"
OID_RUN2 = "AlarmKitSpikeTests/AgentTests/test_TCRUN_more"
REPO = RepoRef(project="demo", repo_path="/tmp/demo-repo", default_branch="main")
# 用户从终端粘贴的短 sha（8 位）；归一化前不得进入鲜度守卫/选择集身份
SHORT_BASE = BASE[:8]
SHORT_HEAD = HEAD[:8]


class _FakeScm:
    def __init__(self, change_set: ChangeSet):
        self._cs = change_set
        self.resolve_calls: list[str] = []

    async def head_sha(self, repo=None):
        return HEAD

    async def resolve_commit(self, rev: str) -> str:
        # 身份归一：夹具里全 sha 直通（对齐真实适配器 40 位 hex 零进程快采）
        self.resolve_calls.append(rev)
        return rev

    async def get_change_set(self, repo, base, head):
        assert (base, head) == (BASE, HEAD)
        return self._cs


class _ResolvingScm(_FakeScm):
    """模拟真实 GitCodeRepository：短 sha 经 rev-parse 展开为 40 位全 sha。"""

    async def resolve_commit(self, rev: str) -> str:
        self.resolve_calls.append(rev)
        mapping = {
            SHORT_BASE: BASE, BASE: BASE,
            SHORT_HEAD: HEAD, HEAD: HEAD,
        }
        if rev not in mapping:
            raise AdapterUnavailableError(f"无法解析 git 版本 {rev!r}")
        return mapping[rev]


def _change_set() -> ChangeSet:
    return ChangeSet(
        repo=REPO, base_commit=BASE, head_commit=HEAD, changed_files=[GIT_PATH],
        symbol_changes=[
            SymbolChange(symbol_id=f"{GIT_PATH}#{MOD_ANCHOR}",
                         change_type=ChangeType.MODIFIED),
            SymbolChange(symbol_id=f"{GIT_PATH}#{REM_ANCHOR}",
                         change_type=ChangeType.REMOVED),
        ],
    )


def _case(case_id: str, *, exploratory: bool = False) -> TestCase:
    return TestCase(
        urn=f"urn:case:{case_id}", external_id=case_id,
        title=f"用例 {case_id}", steps=["步骤"], exploratory=exploratory)


def _refs(case_urn: str, symbol_urn: str) -> CaseRelation:
    return CaseRelation(
        urn=case_relation_urn(case_urn, "case_references_symbol", symbol_urn),
        source_urn=case_urn, target_urn=symbol_urn,
        kind="case_references_symbol", confidence=0.8,
        provenance=["oracle_suggestion"])


def _seed(store) -> None:
    """索引 + 用例（runnable/conflict/exploratory/额外未映射 runnable）+ 关系。"""
    store.upsert_document(SourceDocument(
        urn=DOC_URN, source_system=_SOURCE, uri="file:///repo/Alarm.swift",
        title="Alarm", version="v1", content_hash="h1",
        authority_level=AuthorityLevel.L1_AUTHORITATIVE, acl_scope="default",
        metadata_={"kind_hint": "swift"}))
    for idx, (anchor, urn) in enumerate(((MOD_ANCHOR, MOD_URN), (REM_ANCHOR, REM_URN))):
        store.upsert_chunk(KnowledgeChunk(
            urn=make_urn(_SOURCE, "chunk", f"{EXT}#{anchor}"),
            doc_urn=DOC_URN, index=idx, kind="code_symbol",
            text=f"func {anchor}", symbol_urn=urn,
            quality_score=0.9, valid_from=BASE, content_hash=f"ch-{idx}"))
    doc = "urn:testpilot:fs-cases:document:synthetic-cases"
    for cid, exploratory in (
        (CASE_RUN, False), (CASE_CONF, False),
        (CASE_EXP, True), (CASE_UNMAPPED, False),
    ):
        store.upsert_test_case(doc, _case(cid, exploratory=exploratory),
                               content_hash="hc-1")
    store.replace_case_relations(RELATION_SCOPE_URN, [
        _refs(f"urn:case:{CASE_RUN}", MOD_URN),
        _refs(f"urn:case:{CASE_CONF}", REM_URN),
        _refs(f"urn:case:{CASE_EXP}", MOD_URN),
        _refs(f"urn:case:{CASE_UNMAPPED}", MOD_URN),
    ])


def _write_map(data_dir, *, head: str = HEAD, bindings=None) -> str:
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / "automation-map.json"
    payload = {
        "map_schema_version": AUTOMATION_MAP_SCHEMA_VERSION,
        "generated_at": "2026-11-13T08:00:00Z",
        "repo_path": "/tmp/demo-repo",
        "branch": "demo/agent-generated-tests",
        "head_commit": head,
        "map_hash": "m" * 64,
        "bindings": bindings if bindings is not None else [
            {"case_external_id": CASE_RUN, "only_testing_id": OID_RUN,
             "source_file": "AgentTests.swift", "line": 9, "origin": "marker",
             "reviewed_by": "alice", "reviewed_at": "2026-11-13T09:00:00Z"},
        ],
        "unlinked_swift_tests": [],
        "unmapped_active_cases": [],
        "scan_warnings": [],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return str(path)


def _container(tmp_path, scm=None):
    store = bootstrap_store(tmp_path / "data")
    _seed(store)
    queue = SqliteQueue(store.conn)
    container = make_fake_container(
        store, scm or _FakeScm(_change_set()), queue,
        repo=REPO, data_dir=tmp_path / "work")
    return container, store, queue


def _queued_jobs(store) -> int:
    return store.conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"]


def test_submit_one_request_with_mapped_oids_and_reports_other_buckets(tmp_path):
    container, store, queue = _container(tmp_path)
    _write_map(tmp_path / "work")

    payload = asyncio.run(run_impact_rerun(
        container, base_commit=BASE, head_commit=HEAD))

    assert payload["dry_run"] is False
    selection = payload["selection"]
    assert selection["selected_case_ids"] == [CASE_RUN]
    assert selection["only_testing_ids"] == [OID_RUN]
    # 未映射 runnable 显式上报；冲突/无自动化两桶只报告
    assert selection["unmapped_runnable_case_ids"] == [CASE_UNMAPPED]
    assert selection["no_automation_case_ids"] == [CASE_EXP]
    assert selection["conflict_case_ids"] == [CASE_CONF]

    submission = payload["submission"]
    assert submission["replayed"] is False
    assert submission["status"] == RunStatus.QUEUED.value
    # 账本 + 队列各一条，请求选择集就是映射 OID
    rows = store.list_test_runs()
    assert len(rows) == 1
    row = rows[0]
    assert row["commit_sha"] == HEAD and row["project"] == "demo"
    job = asyncio.run(queue.dequeue(["test_execution"], lease_seconds=60))
    assert job["payload"]["selector"]["test_ids"] == [OID_RUN]
    assert job["payload"]["idempotency_key"] == selection["idempotency_key"]
    # 占重跑配额账本（daily_budget_key 非空），且只扣一次
    today = datetime.now(timezone.utc).date().isoformat()
    entries = store.list_budget_entries(
        selection["daily_budget_key"], today)
    assert len(entries) == 1


def test_replay_same_params_returns_same_run_with_zero_job_growth(tmp_path):
    container, store, _queue = _container(tmp_path)
    _write_map(tmp_path / "work")

    first = asyncio.run(run_impact_rerun(
        container, base_commit=BASE, head_commit=HEAD))
    assert _queued_jobs(store) == 1
    second = asyncio.run(run_impact_rerun(
        container, base_commit=BASE, head_commit=HEAD))

    assert second["submission"]["replayed"] is True
    assert second["submission"]["run_id"] == first["submission"]["run_id"]
    assert _queued_jobs(store) == 1  # jobs 零增生
    assert len(store.list_test_runs()) == 1


def test_dry_run_writes_no_ledger_or_queue(tmp_path):
    container, store, _queue = _container(tmp_path)
    _write_map(tmp_path / "work")

    payload = asyncio.run(run_impact_rerun(
        container, base_commit=BASE, head_commit=HEAD, dry_run=True))

    assert payload["submission"] is None
    assert payload["request"]["selector"]["test_ids"] == [OID_RUN]
    assert _queued_jobs(store) == 0
    assert store.list_test_runs() == []


def test_empty_selection_when_runnable_all_unmapped_fails_closed(tmp_path):
    container, _store, _queue = _container(tmp_path)
    _write_map(tmp_path / "work", bindings=[])  # 无任何绑定

    with pytest.raises(ImpactSelectionError, match="选择集为空"):
        asyncio.run(run_impact_rerun(
            container, base_commit=BASE, head_commit=HEAD))
    assert _queued_jobs(_store) == 0  # 硬失败前不允许入队


def test_stale_map_head_mismatch_rejected_before_submit(tmp_path):
    container, store, _queue = _container(tmp_path)
    _write_map(tmp_path / "work", head="9" * 40)  # map 停在旧 HEAD

    with pytest.raises(ImpactSelectionError, match="已过期"):
        asyncio.run(run_impact_rerun(
            container, base_commit=BASE, head_commit=HEAD))
    assert _queued_jobs(store) == 0


def test_short_sha_inputs_normalized_before_freshness_guard(tmp_path):
    """红证缺陷（2026-11-22 实测）：map.head_commit 存 40 位全 sha，用户贴短 sha
    时鲜度守卫曾做字面比较误判「map 已过期」。归一化必须发生在守卫之前，且短 sha
    与全 sha 两条输入产出逐字相同的选择集/请求（幂等身份不随输入长短漂移）。"""
    scm = _ResolvingScm(_change_set())
    container, _store, _queue = _container(tmp_path, scm=scm)
    _write_map(tmp_path / "work")  # map.head_commit = HEAD（40 位）

    short_payload = asyncio.run(run_impact_rerun(
        container, base_commit=SHORT_BASE, head_commit=SHORT_HEAD, dry_run=True))

    # 两个短 sha 确实先经过 rev 解析（worker 入口 + analyze_impact 内各一轮）
    assert scm.resolve_calls[:2] == [SHORT_BASE, SHORT_HEAD]
    # 载荷里只剩全 sha，短 sha 不泄漏到任何下游身份
    assert short_payload["base_commit"] == BASE
    assert short_payload["head_commit"] == HEAD
    assert short_payload["request"]["commit_sha"] == HEAD
    assert short_payload["selection"]["head_commit"] == HEAD

    # 与全 sha 输入逐字等价：dry-run 不写账本，可同容器复跑对照
    full_payload = asyncio.run(run_impact_rerun(
        container, base_commit=BASE, head_commit=HEAD, dry_run=True))
    assert short_payload["selection"] == full_payload["selection"]
    assert short_payload["request"] == full_payload["request"]


def test_unresolvable_rev_cli_exit_2_not_traceback(tmp_path, monkeypatch):
    """坏 rev（拼写错误/已删除分支）在解析阶段响亮失败，CLI 收口 exit 2。"""

    class _BrokenResolveScm:
        async def head_sha(self, repo=None):
            return HEAD

        async def resolve_commit(self, rev):
            raise AdapterUnavailableError(f"无法解析 git 版本 {rev!r}")

        async def get_change_set(self, repo, base, head):
            raise AssertionError("rev 解析失败时不应走到取 diff")

    container, store, _queue = _container(tmp_path, scm=_BrokenResolveScm())
    _write_map(tmp_path / "work")
    monkeypatch.setattr(ir, "bootstrap", lambda name: container)
    monkeypatch.setattr("sys.argv", [
        "testpilot-impact-rerun", "--base", SHORT_BASE, "--head", SHORT_HEAD])

    with pytest.raises(SystemExit) as exc:
        ir.main()
    assert exc.value.code == 2
    assert _queued_jobs(store) == 0


def test_missing_map_is_filenotfound(tmp_path):
    container, _store, _queue = _container(tmp_path)  # 不写 map
    with pytest.raises(FileNotFoundError, match="automation-map 不存在"):
        asyncio.run(run_impact_rerun(
            container, base_commit=BASE, head_commit=HEAD))


def test_one_case_many_methods_all_oids_submitted(tmp_path):
    container, _store, queue = _container(tmp_path)
    _write_map(tmp_path / "work", bindings=[
        {"case_external_id": CASE_RUN, "only_testing_id": OID_RUN,
         "source_file": "A.swift", "line": 1, "origin": "marker"},
        {"case_external_id": CASE_RUN, "only_testing_id": OID_RUN2,
         "source_file": "A.swift", "line": 2, "origin": "marker"},
    ])
    payload = asyncio.run(run_impact_rerun(
        container, base_commit=BASE, head_commit=HEAD))
    job = asyncio.run(queue.dequeue(["test_execution"], lease_seconds=60))
    assert job["payload"]["selector"]["test_ids"] == sorted([OID_RUN, OID_RUN2])
    assert payload["selection"]["selected_case_ids"] == [CASE_RUN]


def test_cli_adapter_unavailable_maps_to_exit_2_not_traceback(tmp_path, monkeypatch):
    """git diff 不可达等外部依赖失败必须收口 exit 2（对齐 runner CLI 分型）。"""

    class _BrokenScm:
        async def head_sha(self, repo=None):
            return HEAD

        async def resolve_commit(self, rev):
            return rev  # 解析环节放行，让故障点留在取 diff

        async def get_change_set(self, repo, base, head):
            raise AdapterUnavailableError("git diff 失败: fatal: bad object")

    store = bootstrap_store(tmp_path / "data")
    _seed(store)
    queue = SqliteQueue(store.conn)
    container = make_fake_container(
        store, _BrokenScm(), queue, repo=REPO, data_dir=tmp_path / "work")
    _write_map(tmp_path / "work")
    monkeypatch.setattr(ir, "bootstrap", lambda name: container)
    monkeypatch.setattr("sys.argv", [
        "testpilot-impact-rerun", "--base", BASE, "--head", HEAD])

    with pytest.raises(SystemExit) as exc:
        ir.main()
    assert exc.value.code == 2
    assert _queued_jobs(store) == 0


def test_cli_exit_codes_success_and_hard_error(tmp_path, monkeypatch):
    container, _store, _queue = _container(tmp_path)
    _write_map(tmp_path / "work")
    monkeypatch.setattr(ir, "bootstrap", lambda name: container)

    monkeypatch.setattr("sys.argv", [
        "testpilot-impact-rerun", "--base", BASE, "--head", HEAD, "--dry-run"])
    with pytest.raises(SystemExit) as exc:
        ir.main()
    assert exc.value.code == 0

    # 硬错误路径：map 过期 → exit 2
    _write_map(tmp_path / "work", head="9" * 40)
    monkeypatch.setattr("sys.argv", [
        "testpilot-impact-rerun", "--base", BASE, "--head", HEAD])
    with pytest.raises(SystemExit) as exc:
        ir.main()
    assert exc.value.code == 2
