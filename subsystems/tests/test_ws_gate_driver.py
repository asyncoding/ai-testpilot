"""ws_gate_driver 协议主循环的 hermetic 测试（内存假连接，不起真实 socket）。

血缘：out/cs-architecture/tca1-carryin/70-hermetic-tests 的 26 条批次测试转正，
改动仅限：全包导入替代 sys.path 黑活、夹具指向 tests/fixtures/ws-gates/、
延迟导入守护改读 ws_gate_driver.__file__，并增补 --transcript-out 收口断言。

覆盖：
- 绿路：结构跑三闸门完整会话，values/fields 同字节双写、证据落盘、complete/1000；
- 红证四条 fail-loud：错令牌、未知 gate_id、未知消息 type、hello 缺 only_testing；
- 交互采集：必填重问、choice 越界/序号选择、可选文本留空、reject（acknowledge 与 /reject）；
- 闸门 JSON 校验红证（畸形表单/重复 id/auto_values 问题等）；
- 驱动转录：complete/fail-loud/connect_timeout/peer_closed 四收口路径落盘且绝不写明文 token；
- peer_closed（转正补洞）：reject 后 Runner 拆连接，rc=1 显式收口、rejected 闸门留痕。
"""

from __future__ import annotations

import asyncio
import base64
import copy
import json
from pathlib import Path

import pytest

from testpilot_workers import ws_gate_driver as wd
from testpilot_workers.ws_gate_driver import (
    DRIVER_TRANSCRIPT_SCHEMA,
    OUTCOME_PEER_CLOSED,
    GateSpecError,
    PeerDisconnected,
    collect_acknowledge,
    collect_verdict,
    load_gates,
    run_session,
    write_driver_transcript,
)

SESSION = "hsess-p0"
TOKEN = "tok-p0"

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "ws-gates"
GATES_JSON = FIXTURE_DIR / "ws-gates.human.json"
GATES_SIM_JSON = FIXTURE_DIR / "ws-gates.structural.json"


# ── 内存夹具 ────────────────────────────────────────────────────────────────


class ScriptExhausted(RuntimeError):
    """Runner 剧本耗尽仍被 recv——测试脚本本身写错了。"""


class PeerDropConnection:
    """剧本消息发完后，后续 recv/send 全部报 PeerDisconnected（模拟 XCTest 拆连接）。"""

    def __init__(self, inbound: list[dict], *, drop_send: bool = False) -> None:
        self._inbound = list(inbound)
        self._drop_send = drop_send
        self.sent: list[dict] = []
        self.close_codes: list[int] = []

    async def send_json(self, msg: dict) -> None:
        # 只在裁决回送阶段炸：wait_gate/hello_ack 等前置发送必须正常，
        # 才能精确复现"reject 已入账、gate_result 发送撞关闭态"的现场。
        if self._drop_send and msg.get("type") == "gate_result":
            raise PeerDisconnected("模拟对端已关闭，gate_result 发送失败")
        self.sent.append(copy.deepcopy(msg))

    async def recv_json(self) -> dict:
        if not self._inbound:
            raise PeerDisconnected("对端在协议完成前断链：模拟 XCTest 拆连接")
        return self._inbound.pop(0)

    async def aclose(self, code: int = 1000) -> None:
        self.close_codes.append(code)


class FakeConnection:
    """模拟 Runner 侧 socket：入站剧本走队列，出站消息全留存，close 码记录。"""

    def __init__(self, inbound: list[dict]) -> None:
        self._inbound: asyncio.Queue[dict] = asyncio.Queue()
        for msg in inbound:
            self._inbound.put_nowait(msg)
        self.sent: list[dict] = []
        self.close_codes: list[int] = []

    async def send_json(self, msg: dict) -> None:
        self.sent.append(copy.deepcopy(msg))

    async def recv_json(self) -> dict:
        try:
            return self._inbound.get_nowait()
        except asyncio.QueueEmpty as exc:
            raise ScriptExhausted("Runner 剧本耗尽") from exc

    async def aclose(self, code: int = 1000) -> None:
        self.close_codes.append(code)


def hello(token: str = TOKEN, session: str = SESSION, only_testing: bool = True) -> dict:
    return {
        "type": "hello",
        "token": token,
        "session": session,
        "only_testing": only_testing,
        "runner": "fake-runner/1.0",
        "last_seen_seq": 0,
    }


def step(gid: str) -> dict:
    return {"type": "step_reached", "gate_id": gid}


def make_prompt(inputs: list[str]):
    """脚本化输入采集器；记录每次提示语，输入耗尽即 fail-loud（防静默通过）。"""
    queue: asyncio.Queue[str] = asyncio.Queue()
    for item in inputs:
        queue.put_nowait(item)
    calls: list[str] = []

    async def prompt(text: str) -> str:
        calls.append(text)
        if queue.empty():
            raise AssertionError(f"驱动多问了一次：{text}")
        return queue.get_nowait()

    return prompt, calls


async def silent_prompt(_: str) -> str:
    raise AssertionError("结构跑不得发起任何交互提问")


def run(coro):
    return asyncio.run(coro)


def sent_by_type(conn: FakeConnection, mtype: str) -> list[dict]:
    return [m for m in conn.sent if m.get("type") == mtype]


def _green_session_messages() -> list[dict]:
    return [
        hello(),
        step("g-lock"),
        step("g-sidebutton"),
        step("g-sensory"),
        {"type": "evidence", "kind": "screenshot",
         "payload_b64": base64.b64encode(b"PNG-FAKE-BYTES").decode()},
        {"type": "step_finished", "step_id": "TC-A1"},
    ]


# ── 1. 出厂 JSON 自身合法 + 字段闭集对齐 core VerdictFieldKind ───────────────


def test_shipped_gates_are_valid_and_aligned_core_closed_set():
    human = load_gates(GATES_JSON, structural=False)
    sim = load_gates(GATES_SIM_JSON, structural=True)
    assert [g.gate_id for g in human] == ["g-lock", "g-sidebutton", "g-sensory"]
    assert [g.gate_id for g in sim] == ["g-lock", "g-sidebutton", "g-sensory"]
    assert human[0].form_type == "acknowledge" and human[0].fields == ()
    sensory = human[2]
    assert sensory.form_type == "verdict" and len(sensory.fields) == 4
    kinds = {f["key"]: f["kind"] for f in sensory.fields}
    assert kinds == {
        "ringtone_heard": "boolean",
        "vibration_felt": "boolean",
        "confidence": "choice",
        "note": "text",
    }
    by_key = {f["key"]: f for f in sensory.fields}
    assert by_key["confidence"]["options"] == ["high", "medium", "low"]
    assert by_key["note"]["required"] is False
    # 人工版零 auto_values，结构版每闸都有。
    assert all(g.auto_values is None for g in human)
    assert all(g.auto_values is not None for g in sim)


def test_structural_flag_rejects_human_json():
    with pytest.raises(GateSpecError, match="auto_values"):
        load_gates(GATES_JSON, structural=True)


# ── 2. 绿路：结构跑完整会话 ─────────────────────────────────────────────────


def test_structural_full_session_green(tmp_path: Path):
    gates = load_gates(GATES_SIM_JSON, structural=True)
    conn = FakeConnection(_green_session_messages())

    rc = run(run_session(
        conn, gates, session=SESSION, token=TOKEN, structural=True,
        evidence_dir=tmp_path / "evidence", prompt_func=silent_prompt,
        auto_delay=0, log=lambda _m: None,
    ))

    assert rc == 0
    assert sent_by_type(conn, "hello_ack") == [{
        "type": "hello_ack", "last_seen_seq": 0, "protocol": "tp-hybrid-ws-p0",
    }]
    wait_gates = sent_by_type(conn, "wait_gate")
    assert [w["gate_id"] for w in wait_gates] == ["g-lock", "g-sidebutton", "g-sensory"]
    assert wait_gates[0]["verdict_form"]["type"] == "acknowledge"
    assert wait_gates[2]["verdict_form"]["type"] == "verdict"
    assert wait_gates[2]["verdict_form"]["require_evidence"] is True

    results = sent_by_type(conn, "gate_result")
    assert [r["gate_id"] for r in results] == ["g-lock", "g-sidebutton", "g-sensory"]
    for r in results:
        # 同字节双写纪律：values 规范字段 + fields P0 兼容别名。
        assert r["values"] == r["fields"]
        assert r["evidence"] == []
    sensory_values = results[2]["values"]
    assert sensory_values["ringtone_heard"] is True
    assert sensory_values["vibration_felt"] is True
    assert sensory_values["confidence"] == "high"
    assert "AUTO structural fixture" in sensory_values["note"]

    ack = sent_by_type(conn, "evidence_ack")
    assert len(ack) == 1 and ack[0]["kind"] == "screenshot"
    saved = tmp_path / "evidence" / ack[0]["saved_name"]
    assert saved.read_bytes() == b"PNG-FAKE-BYTES"

    assert sent_by_type(conn, "complete")
    assert 1008 not in conn.close_codes
    assert conn.close_codes[-1] == 1000


# ── 3. 红证：fail-loud 四连 ─────────────────────────────────────────────────


def test_red_wrong_token_exit2_1008(tmp_path: Path):
    gates = load_gates(GATES_SIM_JSON, structural=True)
    conn = FakeConnection([hello(token="WRONG-TOKEN")])
    rc = run(run_session(
        conn, gates, session=SESSION, token=TOKEN, structural=True,
        evidence_dir=tmp_path, prompt_func=silent_prompt, auto_delay=0, log=lambda _m: None,
    ))
    assert rc == 2
    fails = sent_by_type(conn, "fail")
    assert len(fails) == 1 and "auth rejected" in fails[0]["reason"]
    assert 1008 in conn.close_codes


def test_red_missing_only_testing_exit2(tmp_path: Path):
    gates = load_gates(GATES_SIM_JSON, structural=True)
    conn = FakeConnection([hello(only_testing=False)])
    rc = run(run_session(
        conn, gates, session=SESSION, token=TOKEN, structural=True,
        evidence_dir=tmp_path, prompt_func=silent_prompt, auto_delay=0, log=lambda _m: None,
    ))
    assert rc == 2
    assert 1008 in conn.close_codes


def test_red_unknown_gate_exit2(tmp_path: Path):
    gates = load_gates(GATES_SIM_JSON, structural=True)
    conn = FakeConnection([hello(), step("g-bogus")])
    rc = run(run_session(
        conn, gates, session=SESSION, token=TOKEN, structural=True,
        evidence_dir=tmp_path, prompt_func=silent_prompt, auto_delay=0, log=lambda _m: None,
    ))
    assert rc == 2
    fails = sent_by_type(conn, "fail")
    assert "unknown gate_id" in fails[0]["reason"]
    assert 1008 in conn.close_codes


def test_red_unknown_message_type_exit2(tmp_path: Path):
    gates = load_gates(GATES_SIM_JSON, structural=True)
    conn = FakeConnection([hello(), {"type": "mystery"}])
    rc = run(run_session(
        conn, gates, session=SESSION, token=TOKEN, structural=True,
        evidence_dir=tmp_path, prompt_func=silent_prompt, auto_delay=0, log=lambda _m: None,
    ))
    assert rc == 2
    fails = sent_by_type(conn, "fail")
    assert "unknown message type" in fails[0]["reason"]
    assert 1008 in conn.close_codes


# ── 4. 交互采集：重问循环与 reject 分支 ─────────────────────────────────────


def test_interactive_verdict_reprompt_choice_index_and_optional_skip(tmp_path: Path):
    gates = load_gates(GATES_JSON, structural=False)
    # g-lock y；g-sidebutton y；感官：铃声留空（必填重问）→ y；震动 n；
    # 置信度越界 huge → 序号 2（medium）；备注留空（可选跳过）。
    prompt, calls = make_prompt(["y", "y", "", "y", "n", "huge", "2", ""])
    conn = FakeConnection([
        hello(), step("g-lock"), step("g-sidebutton"), step("g-sensory"),
        {"type": "step_finished", "step_id": "TC-A1"},
    ])
    rc = run(run_session(
        conn, gates, session=SESSION, token=TOKEN, structural=False,
        evidence_dir=tmp_path, prompt_func=prompt, auto_delay=0, log=lambda _m: None,
    ))
    assert rc == 0
    results = {r["gate_id"]: r for r in sent_by_type(conn, "gate_result")}
    assert results["g-lock"]["verdict"] == "approve"
    sv = results["g-sensory"]["values"]
    assert sv == {
        "ringtone_heard": True,
        "vibration_felt": False,
        "confidence": "medium",
        # note 留空：可选字段缺省即不写入，SDK optionalText 回 nil。
    }
    # 两次重问真实发生（铃声空值 + 置信度越界），且提问总数=8（2 ack + 6 字段轮次）。
    assert len(calls) == 8


def test_acknowledge_reject_is_legal_outcome(tmp_path: Path):
    gates = load_gates(GATES_JSON, structural=False)
    prompt, _ = make_prompt(["n", "y", "y", "y", "n", "1", "fine"])
    conn = FakeConnection([
        hello(), step("g-lock"), step("g-sidebutton"), step("g-sensory"),
        {"type": "step_finished", "step_id": "TC-A1"},
    ])
    rc = run(run_session(
        conn, gates, session=SESSION, token=TOKEN, structural=False,
        evidence_dir=tmp_path, prompt_func=prompt, auto_delay=0, log=lambda _m: None,
    ))
    assert rc == 0
    results = {r["gate_id"]: r for r in sent_by_type(conn, "gate_result")}
    # reject 是合法 outcome：驱动照常回 gate_result，由 Runner 层 XCTAssert 响亮失败。
    assert results["g-lock"]["verdict"] == "reject"
    assert results["g-lock"]["values"] == {}
    complete = sent_by_type(conn, "complete")
    assert complete[0]["summary"]["rejected"] == ["g-lock"]


def test_verdict_reject_escape(tmp_path: Path):
    gates = load_gates(GATES_JSON, structural=False)
    # 两个 acknowledge 先放行，感官第一字段直接 /reject。
    prompt, _ = make_prompt(["y", "y", "/reject"])
    conn = FakeConnection([
        hello(), step("g-lock"), step("g-sidebutton"), step("g-sensory"),
        {"type": "step_finished", "step_id": "TC-A1"},
    ])
    rc = run(run_session(
        conn, gates, session=SESSION, token=TOKEN, structural=False,
        evidence_dir=tmp_path, prompt_func=prompt, auto_delay=0, log=lambda _m: None,
    ))
    assert rc == 0
    sensory = [r for r in sent_by_type(conn, "gate_result") if r["gate_id"] == "g-sensory"][0]
    assert sensory["verdict"] == "reject" and sensory["values"] == {}


# ── 5. 闸门声明校验红证（tmp JSON 变异） ────────────────────────────────────


def _write_gates(tmp_path: Path, payload: dict) -> Path:
    f = tmp_path / "bad-gates.json"
    f.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return f


def _base_payload() -> dict:
    return json.loads(GATES_SIM_JSON.read_text(encoding="utf-8"))


@pytest.mark.parametrize("mutate", [
    lambda p: p.update(protocol="wrong"),
    lambda p: p.update(gates=[]),
    lambda p: p["gates"][0]["verdict_form"].update(type="sign_here"),
    lambda p: p["gates"][2]["verdict_form"]["fields"][2].update(kind="multi_choice"),
    lambda p: p["gates"][2]["verdict_form"]["fields"][2].update(options=[]),
    lambda p: p["gates"].append(dict(p["gates"][0])),  # 重复 gate_id
    lambda p: p["gates"][0]["verdict_form"].update(fields=[{"key": "x", "kind": "boolean"}]),
    lambda p: p["gates"][2]["verdict_form"].update(fields=[]),
    lambda p: p["gates"][2]["auto_values"].update(confidence="ultra"),
    lambda p: p["gates"][2]["auto_values"].pop("ringtone_heard"),
    lambda p: p["gates"][2]["auto_values"].update(ghost=True),
])
def test_red_gate_spec_mutations(tmp_path: Path, mutate):
    payload = _base_payload()
    mutate(payload)
    path = _write_gates(tmp_path, payload)
    with pytest.raises(GateSpecError):
        load_gates(path, structural=True)


def test_red_human_json_must_not_carry_auto_values(tmp_path: Path):
    payload = json.loads(GATES_JSON.read_text(encoding="utf-8"))
    payload["gates"][0]["auto_values"] = {}
    path = _write_gates(tmp_path, payload)
    with pytest.raises(GateSpecError, match="auto_values"):
        load_gates(path, structural=False)


def test_red_missing_gates_file(tmp_path: Path):
    with pytest.raises(GateSpecError, match="不存在"):
        load_gates(tmp_path / "nope.json", structural=False)


# ── 6. 采集器单元行为（不经过会话） ─────────────────────────────────────────


async def _collector_ack():
    gate = load_gates(GATES_JSON, structural=False)[0]
    prompt, calls = make_prompt(["maybe", "y"])
    verdict, values = await collect_acknowledge(gate, prompt)
    assert verdict == "approve" and values == {}
    assert len(calls) == 2  # 非法输入重问一次


def test_collect_acknowledge_reprompts_on_garbage():
    run(_collector_ack())


async def _collector_verdict_choice():
    gate = load_gates(GATES_JSON, structural=False)[2]
    prompt, _ = make_prompt(["y", "y", "low", ""])
    verdict, values = await collect_verdict(gate, prompt)
    assert verdict == "approve"
    assert values["confidence"] == "low"
    assert "note" not in values


def test_collect_verdict_happy_path():
    run(_collector_verdict_choice())


def test_websockets_import_stays_lazy():
    """传输层 import 必须延迟到 connect()：hermetic 测试全程不触发 websockets 安装依赖。

    转正后 websockets 已是 workers 显式依赖，延迟导入的目的变为宿主解耦
    （非 mac/无 socket 栈环境可 import 本模块），守护仍长期钉住。
    """
    src = Path(wd.__file__).read_text(encoding="utf-8")
    top_level = [
        ln for ln in src.splitlines()
        if ln.startswith("import websockets") or ln.startswith("from websockets")
    ]
    assert top_level == [], "websockets 只能在方法内延迟 import"
    assert "import websockets" in src  # 延迟导入确实存在


# ── 7. 驱动转录 --transcript-out（转正新增） ────────────────────────────────


def test_transcript_on_complete_path(tmp_path: Path):
    gates = load_gates(GATES_SIM_JSON, structural=True)
    messages = _green_session_messages()
    messages.insert(1, {"type": "heartbeat", "seq": 1})  # 心跳计数也要进转录
    conn = FakeConnection(messages)
    transcript = tmp_path / "driver-transcript.json"

    rc = run(run_session(
        conn, gates, session=SESSION, token=TOKEN, structural=True,
        evidence_dir=tmp_path / "evidence", prompt_func=silent_prompt,
        auto_delay=0, transcript_out=transcript, log=lambda _m: None,
    ))

    assert rc == 0
    raw = transcript.read_text(encoding="utf-8")
    doc = json.loads(raw)
    assert doc["schema_version"] == DRIVER_TRANSCRIPT_SCHEMA
    assert doc["protocol"] == "tp-hybrid-ws-p0"
    assert doc["session"] == SESSION
    assert doc["structural"] is True
    assert doc["outcome"] == "complete"
    assert doc["close_code"] == 1000
    assert doc["stats"]["hb"] == 1
    assert doc["stats"]["gates"] == ["g-lock", "g-sidebutton", "g-sensory"]
    assert [r["gate_id"] for r in doc["gate_records"]] == [
        "g-lock", "g-sidebutton", "g-sensory",
    ]
    sensory = doc["gate_records"][2]
    assert sensory["verdict"] == "approve" and sensory["rejected"] is False
    assert sensory["values"]["confidence"] == "high"
    assert len(doc["evidence"]) == 1
    ev = doc["evidence"][0]
    assert ev["kind"] == "screenshot" and ev["bytes"] == len(b"PNG-FAKE-BYTES")
    assert ev["saved_name"].startswith("evidence-01-screenshot.")
    assert "finished_at" in doc
    # 密钥红线：转录任何位置都不得出现明文 token。
    assert TOKEN not in raw
    assert "token" not in json.dumps(doc).lower()


def test_transcript_on_fail_loud_path(tmp_path: Path):
    gates = load_gates(GATES_SIM_JSON, structural=True)
    conn = FakeConnection([hello(token="WRONG-TOKEN")])
    transcript = tmp_path / "nested" / "driver-transcript.json"

    rc = run(run_session(
        conn, gates, session=SESSION, token=TOKEN, structural=True,
        evidence_dir=tmp_path, prompt_func=silent_prompt, auto_delay=0,
        transcript_out=transcript, log=lambda _m: None,
    ))

    assert rc == 2
    doc = json.loads(transcript.read_text(encoding="utf-8"))
    assert doc["outcome"] == "fail_loud"
    assert "auth rejected" in doc["reason"]
    assert doc["close_code"] == 1008
    assert doc["gate_records"] == [] and doc["evidence"] == []
    assert TOKEN not in transcript.read_text(encoding="utf-8")


def test_transcript_records_rejected_gate(tmp_path: Path):
    gates = load_gates(GATES_JSON, structural=False)
    prompt, _ = make_prompt(["n"])  # 首闸即 reject，随后 step_finished 收口
    conn = FakeConnection([
        hello(), step("g-lock"),
        {"type": "step_finished", "step_id": "TC-A1"},
    ])
    transcript = tmp_path / "driver-transcript.json"

    rc = run(run_session(
        conn, gates, session=SESSION, token=TOKEN, structural=False,
        evidence_dir=tmp_path, prompt_func=prompt, auto_delay=0,
        transcript_out=transcript, log=lambda _m: None,
    ))

    assert rc == 0
    doc = json.loads(transcript.read_text(encoding="utf-8"))
    assert doc["stats"]["rejected"] == ["g-lock"]
    record = doc["gate_records"][0]
    assert record["gate_id"] == "g-lock"
    assert record["verdict"] == "reject" and record["rejected"] is True


def test_build_transcript_connect_timeout_shape():
    doc = wd._build_transcript(
        session=SESSION, structural=True, outcome="connect_timeout",
        reason="连不上 ws://127.0.0.1:9876/",
        stats={"hb": 0, "gates": [], "rejected": []},
        gate_records=[], evidence_records=[], close_code=None,
    )
    assert doc["schema_version"] == DRIVER_TRANSCRIPT_SCHEMA
    assert doc["outcome"] == "connect_timeout"
    assert doc["close_code"] is None
    assert "token" not in {k.lower() for k in doc}


def test_write_transcript_swallows_disk_error(tmp_path: Path):
    # 父路径是普通文件时 mkdir/写盘必抛 OSError；转录失败不得掩盖协议退出码。
    blocker = tmp_path / "a-file"
    blocker.write_text("x", encoding="utf-8")
    logs: list[str] = []
    write_driver_transcript(blocker / "transcript.json", {"k": "v"}, logs.append)
    assert logs and "转录落盘失败" in logs[0]


def test_main_connect_timeout_writes_transcript(tmp_path: Path):
    # retry-seconds=0：deadline 即刻过期，不发起任何真实 socket 连接，直接 RuntimeError。
    transcript = tmp_path / "connect-timeout.json"
    rc = wd.main([
        "--gates", str(GATES_SIM_JSON), "--structural",
        "--retry-seconds", "0", "--transcript-out", str(transcript),
    ])
    assert rc == 2
    doc = json.loads(transcript.read_text(encoding="utf-8"))
    assert doc["outcome"] == "connect_timeout"
    assert doc["close_code"] is None
    assert TOKEN not in transcript.read_text(encoding="utf-8")


# ── 8. peer_closed：reject 后 Runner 拆连接的真实收口（转正补洞） ─────────────


def test_peer_disconnected_is_connection_error():
    # 传输中立异常走内置 ConnectionError 族，hermetic 假连接可直接抛、无需 websockets。
    assert issubclass(PeerDisconnected, ConnectionError)


def test_peer_closed_after_reject_persists_transcript_and_rc1(tmp_path: Path):
    """红证背景：真实 Swift 链路里 reject → XCTAssert 抛错 → XCTest 拆连接，
    驱动等不到 step_finished。不捕获就是 traceback rc=1 且转录丢失，ws-run
    无法做 reject→exit 3 分型。现在必须 peer_closed 留痕、rc 显式为 1。
    """
    gates = load_gates(GATES_JSON, structural=False)
    prompt, _ = make_prompt(["n"])  # 首闸 g-lock 即 reject，随后对端断链
    conn = PeerDropConnection([hello(), step("g-lock")])
    transcript = tmp_path / "driver-transcript.json"

    rc = run(run_session(
        conn, gates, session=SESSION, token=TOKEN, structural=False,
        evidence_dir=tmp_path, prompt_func=prompt, auto_delay=0,
        transcript_out=transcript, log=lambda _m: None,
    ))

    assert rc == 1
    # reject 的 gate_result 仍正常发出（协议语义：reject 是合法裁决）
    gate_result = sent_by_type(conn, "gate_result")[0]
    assert gate_result["verdict"] == "reject"
    doc = json.loads(transcript.read_text(encoding="utf-8"))
    assert doc["outcome"] == OUTCOME_PEER_CLOSED
    assert doc["close_code"] is None
    assert doc["stats"]["rejected"] == ["g-lock"]
    record = doc["gate_records"][0]
    assert record["gate_id"] == "g-lock" and record["rejected"] is True
    assert "断链" in doc["reason"]
    assert TOKEN not in transcript.read_text(encoding="utf-8")


def test_peer_closed_before_any_gate_is_also_persisted(tmp_path: Path):
    # hello 之后立刻断链（Runner 进程崩溃的近似形态）：空闸门序列也要留痕，
    # ws-run 按"无 reject 留痕的提前断链"判硬错误，不能静默丢现场。
    gates = load_gates(GATES_SIM_JSON, structural=True)
    conn = PeerDropConnection([hello()])
    transcript = tmp_path / "driver-transcript.json"

    rc = run(run_session(
        conn, gates, session=SESSION, token=TOKEN, structural=True,
        evidence_dir=tmp_path, prompt_func=silent_prompt, auto_delay=0,
        transcript_out=transcript, log=lambda _m: None,
    ))

    assert rc == 1
    doc = json.loads(transcript.read_text(encoding="utf-8"))
    assert doc["outcome"] == OUTCOME_PEER_CLOSED
    assert doc["gate_records"] == [] and doc["evidence"] == []


def test_peer_drop_during_send_same_outcome(tmp_path: Path):
    # 对端在驱动发 gate_result 前已关闭：send_json 翻译出的 PeerDisconnected
    # 走同一条收口，转录不丢。
    gates = load_gates(GATES_JSON, structural=False)
    prompt, _ = make_prompt(["n"])
    conn = PeerDropConnection([hello(), step("g-lock")], drop_send=True)
    transcript = tmp_path / "driver-transcript.json"

    rc = run(run_session(
        conn, gates, session=SESSION, token=TOKEN, structural=False,
        evidence_dir=tmp_path, prompt_func=prompt, auto_delay=0,
        transcript_out=transcript, log=lambda _m: None,
    ))

    assert rc == 1
    doc = json.loads(transcript.read_text(encoding="utf-8"))
    assert doc["outcome"] == OUTCOME_PEER_CLOSED
    assert doc["stats"]["rejected"] == ["g-lock"]
