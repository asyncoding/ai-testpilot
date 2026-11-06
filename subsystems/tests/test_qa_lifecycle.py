"""QaService 异步生命周期测试（S1.4 C6）：AsyncSqliteSaver 落盘/重开/收口。

覆盖计划事实 #6：同步构造期不能起 saver（驱动取 running loop），因此服务分
两阶段——构造只存 data_dir，await astart() 建 data_dir/checkpoints/qa.sqlite
并编译图，aclose() 关连接留文件。断言用 checkpoint 表行数差，不做存在性假绿。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from qa_fakes import FakeHybridSearch, FakeModel, make_chunk, structured_envelope
from testpilot_adapters.store import bootstrap_store
from testpilot_core.enums import QaStatus
from testpilot_core.models import AskRequest
from testpilot_orchestrator.qa_graph.nodes import QaDeps
from testpilot_orchestrator.qa_graph.service import CHECKPOINT_REL, QaService

DOC1 = "urn:testpilot:fs-docs:document:docs/runbook.md"
CHUNK1 = "urn:testpilot:fs-docs:chunk:docs/runbook.md#section-one"
SYM1 = "urn:testpilot:fs-docs:symbol:Sources/Alarm.swift#Alarm.ring"
SYM_DOC = "urn:testpilot:fs-docs:document:Sources/Alarm.swift"


def _deps(tmp_path, **kw) -> tuple[QaDeps, object, FakeModel]:
    store = bootstrap_store(tmp_path / "store")
    chunk = make_chunk(SYM1, doc_urn=SYM_DOC, kind="code_symbol")
    model = FakeModel({
        "qa_intent_t1": structured_envelope({"intent": "code"}),
        "qa_answer_t1": structured_envelope({
            "summary": "结论：证据原文",
            "sections": [],
            "citations": [{"urn": SYM1, "quote": "证据原文"}],
        }),
    })
    search = FakeHybridSearch([chunk])
    return QaDeps(model=model, search=search, store=store, **kw), store, model


def _checkpoint_rows(db) -> int:
    con = sqlite3.connect(str(db))
    try:
        return con.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0]
    finally:
        con.close()


async def test_sqlite_saver_persists_and_state_survives_reopen(tmp_path):
    deps, _store, _model = _deps(tmp_path)
    db = tmp_path / CHECKPOINT_REL
    service = QaService(deps, _store, data_dir=tmp_path)
    # 未启动先拦截：不能让 ask 静默走一个没编译的图
    with pytest.raises(RuntimeError, match="astart"):
        await service.ask(AskRequest(question="Alarm.ring 的逻辑？", project="demo",
                                     branch="main"), run_id="run-never")

    await service.astart()
    assert db.exists()  # 启动即建库建表
    assert _checkpoint_rows(db) == 0  # 前置：空库行数基线

    resp = await service.ask(
        AskRequest(question="Alarm.ring 的逻辑？", project="demo", branch="main"),
        case_key="t1", run_id="run-life-1",
    )
    assert resp.status == QaStatus.ANSWERED
    rows_after = _checkpoint_rows(db)
    assert rows_after >= 1  # 行数差自证：一次 run 真实落了 checkpoint

    await service.aclose()
    assert db.exists()  # 收口只关连接，文件保留供对账/重开

    # 跨连接重开：同 thread_id 的 state 必须能从盘上取回
    deps2, store2, _ = _deps(tmp_path)
    service2 = QaService(deps2, store2, data_dir=tmp_path)
    await service2.astart()
    try:
        assert _checkpoint_rows(db) == rows_after  # 重开不丢历史
        snapshot = await service2._graph.aget_state(
            {"configurable": {"thread_id": "run-life-1"}}
        )
        assert snapshot.values["run_id"] == "run-life-1"
        assert snapshot.values["status"] == QaStatus.ANSWERED.value
    finally:
        await service2.aclose()


async def test_inmemory_path_astart_idempotent_and_aclose_safe(tmp_path):
    deps, store, _ = _deps(tmp_path)
    service = QaService(deps, store)  # 无 data_dir：默认 InMemorySaver，构造即就绪
    assert await service.astart() is service  # 幂等直通，不重建图
    resp = await service.ask(
        AskRequest(question="Alarm.ring 的逻辑？", project="demo", branch="main"),
        case_key="t1", run_id="run-mem-1",
    )
    assert resp.status == QaStatus.ANSWERED
    await service.aclose()  # 无 saver 资源，收口无副作用
    await service.aclose()  # 幂等：重复收口不报错


async def test_build_qa_service_wires_real_offline_container(tmp_path):
    """真实 offline-demo 容器走通异步装配边界（同步 factory 全程不碰 langgraph）。

    chitchat 在 acl 节点即收口（out_of_scope），无需预灌知识库即可验证：
    recorded 模型接通、sqlite saver 落盘、服务可正常收口。
    """
    from testpilot_orchestrator.config_loader import load_profile
    from testpilot_orchestrator.factory import build_container
    from testpilot_orchestrator.qa_graph.service import build_qa_service

    subsystems = Path(__file__).resolve().parents[1]
    config = load_profile(subsystems / "config" / "profiles" / "offline-demo.yaml")
    config.paths["data_dir"] = str(tmp_path / "data")
    container = build_container(config, subsystems)
    try:
        service = await build_qa_service(container)
        try:
            db = container.data_dir / CHECKPOINT_REL
            assert db.exists()
            resp = await service.ask(
                AskRequest(question="你好，讲个笑话", project=config.project.name,
                           branch=config.project.default_branch),
                case_key="chitchat", run_id="run-wire-1",
            )
            assert resp.status == QaStatus.UNKNOWN and resp.unknown_reason == "out_of_scope"
            assert _checkpoint_rows(db) >= 1
        finally:
            await service.aclose()
    finally:
        container.get("store").close()


async def test_sqlite_astart_is_idempotent_and_checkpoint_dir_is_fixed(tmp_path):
    deps, store, _ = _deps(tmp_path)
    service = QaService(deps, store, data_dir=tmp_path)
    await service.astart()
    graph_first = service._graph
    await service.astart()  # 已启动：不得重连重建
    assert service._graph is graph_first
    assert service._saver_ctx is not None
    await service.aclose()
    # 固定相对路径，探针/运维可直接定位（CHECKPOINT_REL 即契约）
    assert (tmp_path / CHECKPOINT_REL).exists()
    assert str(CHECKPOINT_REL) == "checkpoints/qa.sqlite"
