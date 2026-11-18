"""WS 闸门驱动（USB relay 转正组件，console script: testpilot-ws-gate-driver）。

反向监听 C/S 的 Mac 侧客户端：Runner 在设备 loopback 起 WebSocket 服务端，本驱动
作为客户端经直连（模拟器）或 iproxy（真机 USB）反连，按外置闸门声明驱动人工/
结构裁决。``testpilot-hybrid ws-run`` 编排器以子进程方式调用本模块，也可独立
手工运行。

血缘：p0-spike usb_driver_client 的连接重试 / hello 鉴权 / 证据落盘纪律 +
tca1-carryin 批次驱动的闸门工程化，转正时协议行为逐字保留，三处产品化要点：
1. 闸门声明外置 JSON（人工版 / 结构跑版），驱动按 verdict_form 对象驱动，
   不内置闸门知识；gate_id 由被测 Swift 用例的 step_reached 帧决定；
2. 交互采集：acknowledge 走 y/n，verdict 逐字段采集，必填缺失/choice 越界立即
   重问，任意字段提示处 /reject 整闸退回（reject 是合法 outcome，Runner 响亮失败）；
3. 协议逻辑与 websocket 传输解耦（Connection 协议 + 内存假连接），hermetic
   pytest 不起真实 socket 即可覆盖错令牌/未知 type/未知 gate/表单越界四条红证。

线协议稳定名（与 TestPilotKit 逐字对齐，模块改名也不动这些字面量）：
PROTOCOL_ID=tp-hybrid-ws-p0；环境变量 TP_LISTEN_PORT/TP_SESSION/TP_TOKEN/
TP_TCA1_STRUCTURAL；消息类型 hello/hello_ack/step_reached/wait_gate/gate_result/
heartbeat/evidence/evidence_ack/step_finished/complete/fail。
- hello 校验失败/未知 gate_id/未知消息 type → fail + close 1008，进程 exit 2；
- wait_gate.verdict_form 为对象 {type: acknowledge|verdict, fields[], require_evidence}；
- gate_result 同字节双写 values（规范）+ fields（P0 兼容别名），带 evidence 数组。

--transcript-out：驱动侧会话转录（ws-gate-driver-transcript-v1），complete/
fail-loud/重试窗耗尽三条收口路径都落盘，供 ws-run 编排器生成正式转录；
token 绝不写入转录。

示例（结构跑）：
  testpilot-ws-gate-driver \
      --url ws://127.0.0.1:9876/ws/hybrid/hsess-p0 --session hsess-p0 --token tok-p0 \
      --gates ws-gates.structural.json --structural \
      --evidence-dir ./ws-gate-evidence --retry-seconds 300
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Protocol

PROTOCOL_ID = "tp-hybrid-ws-p0"
FORM_TYPES = frozenset({"acknowledge", "verdict"})
FIELD_KINDS = frozenset({"boolean", "choice", "text"})
# 与 core hybrid_plan.VerdictFieldKind 闭集逐字一致。

DRIVER_TRANSCRIPT_SCHEMA = "ws-gate-driver-transcript-v1"

# 驱动转录 outcome 闭集：complete 正常收口；fail_loud 驱动侧协议红证（rc=2）；
# connect_timeout 重试窗耗尽；peer_closed 对端在协议完成前断链（典型：人工
# reject 后 Runner XCTAssert 抛错，XCTest 拆连接，驱动没等到 step_finished）。
OUTCOME_COMPLETE = "complete"
OUTCOME_FAIL_LOUD = "fail_loud"
OUTCOME_CONNECT_TIMEOUT = "connect_timeout"
OUTCOME_PEER_CLOSED = "peer_closed"

PromptFunc = Callable[[str], Awaitable[str]]


class GateSpecError(ValueError):
    """闸门声明文件不合法（CLI 映射 exit 2，与 HybridPlanError 同纪律）。"""


class PeerDisconnected(ConnectionError):
    """传输中立的"对端提前断链"：websockets 异常在连接适配层翻译成它，
    协议主循环据此落 peer_closed 转录而不是吐 traceback（rc=1 收口）。"""


# ── 闸门声明加载与校验 ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class GateSpec:
    gate_id: str
    order: int
    timeout_hint: int
    prompt: str
    form_type: str  # acknowledge | verdict
    fields: tuple[dict, ...]
    require_evidence: bool
    auto_values: dict | None


def load_gates(path: str | Path, *, structural: bool) -> list[GateSpec]:
    """加载并 fail-loud 校验闸门声明。structural=True 时强制每闸带合法 auto_values。"""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise GateSpecError(f"闸门声明文件不存在：{path}") from exc
    except json.JSONDecodeError as exc:
        raise GateSpecError(f"闸门声明 JSON 畸形：{exc}") from exc

    if raw.get("protocol") != PROTOCOL_ID:
        raise GateSpecError(f"protocol 必须是 {PROTOCOL_ID}，实际 {raw.get('protocol')!r}")
    gates_raw = raw.get("gates")
    if not isinstance(gates_raw, list) or not gates_raw:
        raise GateSpecError("gates 必须是非空数组")

    seen_ids: set[str] = set()
    gates: list[GateSpec] = []
    for idx, item in enumerate(gates_raw):
        if not isinstance(item, dict):
            raise GateSpecError(f"gates[{idx}] 不是对象")
        gid = item.get("gate_id")
        if not isinstance(gid, str) or not gid:
            raise GateSpecError(f"gates[{idx}] 缺合法 gate_id")
        if gid in seen_ids:
            raise GateSpecError(f"gate_id 重复：{gid}")
        seen_ids.add(gid)

        form = item.get("verdict_form")
        if not isinstance(form, dict):
            raise GateSpecError(f"闸门 {gid} 缺 verdict_form 对象")
        form_type = form.get("type")
        if form_type not in FORM_TYPES:
            raise GateSpecError(
                f"闸门 {gid} verdict_form.type 越界：{form_type!r}，合法 {sorted(FORM_TYPES)}"
            )
        fields_raw = form.get("fields", [])
        if not isinstance(fields_raw, list):
            raise GateSpecError(f"闸门 {gid} fields 必须是数组")
        if form_type == "acknowledge" and fields_raw:
            raise GateSpecError(f"闸门 {gid} 是 acknowledge，fields 必须为空")
        if form_type == "verdict" and not fields_raw:
            raise GateSpecError(f"闸门 {gid} 是 verdict，fields 不得为空")

        field_keys: set[str] = set()
        fields: list[dict] = []
        for f_idx, field in enumerate(fields_raw):
            key = field.get("key")
            if not isinstance(key, str) or not key:
                raise GateSpecError(f"闸门 {gid} fields[{f_idx}] 缺合法 key")
            if key in field_keys:
                raise GateSpecError(f"闸门 {gid} 字段 key 重复：{key}")
            field_keys.add(key)
            kind = field.get("kind")
            if kind not in FIELD_KINDS:
                raise GateSpecError(
                    f"闸门 {gid} 字段 {key} kind 越界：{kind!r}，合法 {sorted(FIELD_KINDS)}"
                )
            if kind == "choice":
                options = field.get("options")
                if not isinstance(options, list) or not options or not all(
                    isinstance(o, str) and o for o in options
                ):
                    raise GateSpecError(f"闸门 {gid} 字段 {key} 是 choice，必须带非空字符串 options")
            if not isinstance(field.get("label", ""), str):
                raise GateSpecError(f"闸门 {gid} 字段 {key} label 必须是字符串")
            fields.append(dict(field))

        require_evidence = bool(form.get("require_evidence", False))
        auto_values = item.get("auto_values")
        if structural:
            if not isinstance(auto_values, dict):
                raise GateSpecError(
                    f"结构跑要求闸门 {gid} 带 auto_values 对象（人工版 JSON 不得用于 --structural）"
                )
            _validate_auto_values(gid, fields, auto_values)
        elif auto_values is not None:
            # 人工版带 auto_values 属于声明污染，fail-loud 防误用。
            raise GateSpecError(f"人工版闸门 {gid} 不得携带 auto_values")

        gates.append(
            GateSpec(
                gate_id=gid,
                order=int(item.get("order", idx + 1)),
                timeout_hint=int(item.get("timeout_hint", 240)),
                prompt=str(item.get("prompt", "")),
                form_type=form_type,
                fields=tuple(fields),
                require_evidence=require_evidence,
                auto_values=auto_values,
            )
        )

    gates.sort(key=lambda g: g.order)
    return gates


def _validate_auto_values(gid: str, fields: list[dict], values: dict) -> None:
    by_key = {f["key"]: f for f in fields}
    for key, value in values.items():
        if key not in by_key:
            raise GateSpecError(f"闸门 {gid} auto_values 含未知字段 {key}")
        field = by_key[key]
        kind = field["kind"]
        if kind == "boolean" and not isinstance(value, bool):
            raise GateSpecError(f"闸门 {gid} auto_values.{key} 必须是布尔")
        if kind == "choice" and value not in field["options"]:
            raise GateSpecError(
                f"闸门 {gid} auto_values.{key} 越界：{value!r}，合法 {field['options']}"
            )
        if kind == "text" and not isinstance(value, str):
            raise GateSpecError(f"闸门 {gid} auto_values.{key} 必须是字符串")
    for field in fields:
        if field.get("required", True) and field["key"] not in values:
            raise GateSpecError(f"闸门 {gid} auto_values 缺必填字段 {field['key']}")


# ── 传输抽象（生产用 websockets，测试用内存假连接） ──────────────────────────


class Connection(Protocol):
    async def send_json(self, msg: dict) -> None: ...
    async def recv_json(self) -> dict: ...
    async def aclose(self, code: int = 1000) -> None: ...


class WebsocketsConnection:
    """websockets 库适配层；import 延迟到工厂方法。

    websockets 已是 workers 显式依赖（pyproject 声明 >=14），延迟导入的目的是让
    hermetic 测试与非 mac 宿主 import 本模块时不需要 socket 栈，不是隐藏依赖。
    """

    def __init__(self, ws) -> None:
        self._ws = ws

    @classmethod
    async def connect(cls, url: str, retry_seconds: float, log: Callable[[str], None]) -> "WebsocketsConnection":
        import websockets  # 延迟导入：守护测试 test_websockets_import_stays_lazy 长期钉住
        from websockets.exceptions import ConnectionClosed, InvalidHandshake

        deadline = time.time() + retry_seconds
        last_err: Exception | None = None
        retryable = (OSError, EOFError, InvalidHandshake, ConnectionClosed)
        while time.time() < deadline:
            try:
                ws = await websockets.connect(
                    url, max_size=16 * 1024 * 1024, ping_interval=20, ping_timeout=10
                )
                log(f"connected {url}")
                return cls(ws)
            except retryable as err:
                last_err = err
                log(f"等待 Runner listener 就位…({type(err).__name__}: {err})")
                await asyncio.sleep(1)
        raise RuntimeError(f"连不上 {url}，最后错误：{last_err}")

    async def send_json(self, msg: dict) -> None:
        from websockets.exceptions import ConnectionClosed  # 延迟导入，同 connect()
        try:
            await self._ws.send(json.dumps(msg, ensure_ascii=False))
        except (ConnectionClosed, EOFError, OSError) as exc:
            # reject 后 Runner 拆链时，gate_result 发送也可能撞上关闭态；
            # 统一翻译成传输中立异常，主循环保证转录落盘。
            raise PeerDisconnected(f"对端断链，发送失败：{exc}") from exc

    async def recv_json(self) -> dict:
        from websockets.exceptions import ConnectionClosed  # 延迟导入，同 connect()
        try:
            return json.loads(await self._ws.recv())
        except (ConnectionClosed, EOFError, OSError) as exc:
            raise PeerDisconnected(f"对端在协议完成前断链：{exc}") from exc

    async def aclose(self, code: int = 1000) -> None:
        await self._ws.close(code=code)


# ── 交互采集 ────────────────────────────────────────────────────────────────


async def default_prompt(prompt: str) -> str:
    return await asyncio.get_running_loop().run_in_executor(None, input, prompt)


async def _ask(prompt_func: PromptFunc, text: str) -> str:
    return (await prompt_func(text)).strip()


async def collect_acknowledge(gate: GateSpec, prompt_func: PromptFunc) -> tuple[str, dict]:
    while True:
        raw = await _ask(
            prompt_func,
            f"[闸门 {gate.gate_id}] {gate.prompt}\n  确认请输入 y，拒绝请输入 n：",
        )
        if raw in ("y", "yes"):
            return "approve", {}
        if raw in ("n", "no"):
            return "reject", {}
        print(f"  ↩ 非法输入 {raw!r}，只接受 y/n，重问", flush=True)


async def collect_verdict(
    gate: GateSpec, prompt_func: PromptFunc
) -> tuple[str, dict]:
    values: dict = {}
    print(f"[闸门 {gate.gate_id}] {gate.prompt}", flush=True)
    for field in gate.fields:
        key, kind = field["key"], field["kind"]
        required = field.get("required", True)
        label = field.get("label", key)
        while True:
            if kind == "boolean":
                raw = await _ask(
                    prompt_func, f"  {label}（{key}，y/n{'，必填' if required else '，可留空'}）："
                )
                if raw == "/reject":
                    return "reject", {}
                if raw in ("y", "yes"):
                    values[key] = True
                    break
                if raw in ("n", "no"):
                    values[key] = False
                    break
                if not raw and not required:
                    break
                print("  ↩ 布尔字段只接受 y/n，重问", flush=True)
            elif kind == "choice":
                options = field["options"]
                joined = "/".join(f"{i + 1}.{o}" for i, o in enumerate(options))
                raw = await _ask(prompt_func, f"  {label}（{key}，选 {joined}）：")
                if raw == "/reject":
                    return "reject", {}
                if raw in options:
                    values[key] = raw
                    break
                if raw.isdigit() and 1 <= int(raw) <= len(options):
                    values[key] = options[int(raw) - 1]
                    break
                print(f"  ↩ 越界输入 {raw!r}，合法 {options} 或序号，重问", flush=True)
            else:  # text
                raw = await prompt_func(f"  {label}（{key}，文本{'，必填' if required else '，可留空'}）：")
                raw = raw.strip()
                if raw == "/reject":
                    return "reject", {}
                if raw:
                    values[key] = raw
                    break
                if not required:
                    break
                print("  ↩ 必填文本不能为空，重问", flush=True)
    return "approve", values


# ── 驱动侧转录（进程间文件解耦：ws-run 读它生成正式转录） ────────────────────


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_driver_transcript(path: str | Path, payload: dict, log: Callable[[str], None]) -> None:
    """tmp + os.replace 原子写驱动转录；磁盘故障只记账不掩盖协议退出码。"""
    out = Path(path)
    tmp = out.with_name(f".{out.name}.tmp-{os.getpid()}")
    try:
        # mkdir 也在守卫内：父路径是普通文件时 FileExistsError（OSError 子类）
        # 同样只记账，不能掩盖协议侧既有的 0/2 退出码。
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, out)
    except OSError as exc:
        log(f"转录落盘失败（不影响协议退出码）：{exc}")


def _build_transcript(
    *,
    session: str,
    structural: bool,
    outcome: str,
    reason: str,
    stats: dict,
    gate_records: list[dict],
    evidence_records: list[dict],
    close_code: int | None,
) -> dict:
    # 刻意不含 token：转录是对账/审计制品，密钥只走内存与注入 plist。
    return {
        "schema_version": DRIVER_TRANSCRIPT_SCHEMA,
        "protocol": PROTOCOL_ID,
        "session": session,
        "structural": structural,
        "outcome": outcome,
        "reason": reason,
        "stats": stats,
        "gate_records": gate_records,
        "evidence": evidence_records,
        "close_code": close_code,
        "finished_at": _utc_now_iso(),
    }


# ── 协议主循环（传输无关，hermetic 测试直接打这一层） ────────────────────────


async def run_session(
    conn: Connection,
    gates: list[GateSpec],
    *,
    session: str,
    token: str,
    structural: bool,
    evidence_dir: str | Path,
    prompt_func: PromptFunc,
    auto_delay: float = 1.0,
    transcript_out: str | Path | None = None,
    log: Callable[[str], None] = print,
) -> int:
    """返回进程语义退出码：0 正常 complete；1 对端提前断链（peer_closed，转录留痕，
    典型 reject 后 Runner 拆连接）；2 fail-loud（鉴权/未知 type/未知 gate）或
    重试窗耗尽（connect_timeout，main 层收口）。"""
    gate_by_id = {g.gate_id: g for g in gates}
    stats: dict = {"hb": 0, "gates": [], "rejected": []}
    gate_records: list[dict] = []
    evidence_records: list[dict] = []
    evidence_seq = 0

    def _persist(outcome: str, reason: str, close_code: int | None) -> None:
        if transcript_out is None:
            return
        write_driver_transcript(
            transcript_out,
            _build_transcript(
                session=session,
                structural=structural,
                outcome=outcome,
                reason=reason,
                stats=stats,
                gate_records=gate_records,
                evidence_records=evidence_records,
                close_code=close_code,
            ),
            log,
        )

    async def fail_loud(reason: str) -> int:
        log(f"RED: {reason} -> close 1008")
        await conn.send_json({"type": "fail", "reason": reason})
        await conn.aclose(1008)
        _persist(OUTCOME_FAIL_LOUD, reason, 1008)
        return 2

    try:
        hello = await conn.recv_json()
        authed = (
            hello.get("type") == "hello"
            and hello.get("session") == session
            and hello.get("token") == token
            and hello.get("only_testing") is True
        )
        if not authed:
            return await fail_loud(f"auth rejected, hello={json.dumps(hello, ensure_ascii=False)}")

        await conn.send_json({
            "type": "hello_ack",
            "last_seen_seq": hello.get("last_seen_seq", 0),
            "protocol": PROTOCOL_ID,
        })
        log("hello_ack sent, entering message loop")

        while True:
            msg = await conn.recv_json()
            mtype = msg.get("type")
            log(f"RECV {json.dumps(msg, ensure_ascii=False)[:220]}")

            if mtype == "heartbeat":
                stats["hb"] += 1
                continue

            if mtype == "step_reached":
                gid = str(msg.get("gate_id", ""))
                gate = gate_by_id.get(gid)
                if gate is None:
                    return await fail_loud(f"unknown gate_id: {gid}")

                await conn.send_json({
                    "type": "wait_gate",
                    "gate_id": gid,
                    "prompt": gate.prompt,
                    "verdict_form": {
                        "type": gate.form_type,
                        "fields": list(gate.fields),
                        "require_evidence": gate.require_evidence,
                    },
                })

                if structural:
                    if auto_delay > 0:
                        await asyncio.sleep(auto_delay)
                    verdict, values = "approve", dict(gate.auto_values or {})
                elif gate.form_type == "acknowledge":
                    verdict, values = await collect_acknowledge(gate, prompt_func)
                else:
                    verdict, values = await collect_verdict(gate, prompt_func)

                rejected = verdict == "reject"
                if rejected:
                    stats["rejected"].append(gid)
                stats["gates"].append(gid)
                gate_records.append({
                    "gate_id": gid,
                    "verdict": verdict,
                    "values": values,
                    "rejected": rejected,
                })
                result = {
                    "type": "gate_result",
                    "gate_id": gid,
                    "verdict": verdict,
                    # 规范 values + P0 兼容别名 fields 同字节双写。
                    "values": values,
                    "fields": values,
                    "evidence": [],
                }
                log(f"gate {gid} verdict={verdict} values={json.dumps(values, ensure_ascii=False)}")
                await conn.send_json(result)
                continue

            if mtype == "evidence":
                out_dir = Path(evidence_dir)
                out_dir.mkdir(parents=True, exist_ok=True)
                kind = str(msg.get("kind", "unknown"))
                evidence_seq += 1
                suffix = "png" if kind == "screenshot" else "txt"
                out = out_dir / f"evidence-{evidence_seq:02d}-{kind}.{suffix}"
                out.write_bytes(base64.b64decode(msg.get("payload_b64", "")))
                size = out.stat().st_size
                log(f"evidence saved {out} bytes={size}")
                evidence_records.append({
                    "seq": evidence_seq, "kind": kind,
                    "saved_name": out.name, "bytes": size,
                })
                await conn.send_json({
                    "type": "evidence_ack",
                    "kind": kind,
                    "saved_name": out.name,
                    "bytes": size,
                })
                continue

            if mtype == "step_finished":
                await conn.send_json({"type": "complete", "summary": stats})
                log(f"session complete gates={stats['gates']} hb={stats['hb']} rejected={stats['rejected']}")
                await conn.aclose(1000)
                _persist(OUTCOME_COMPLETE, "", 1000)
                return 0

            return await fail_loud(f"unknown message type: {mtype}")
    except PeerDisconnected as exc:
        # 对端在 step_finished 前断链：典型是人工 reject 后 Runner XCTAssert
        # 抛错、XCTest 拆连接。转录必须留痕（含 rejected 闸门），ws-run 据此
        # 分型产品失败 exit 3，而不是让异常吐 traceback、丢掉全部对账信息。
        log(f"PEER CLOSED before complete: {exc}")
        _persist(OUTCOME_PEER_CLOSED, str(exc), None)
        return 1
    finally:
        await conn.aclose(1000)


# ── CLI ─────────────────────────────────────────────────────────────────────


def _log(msg: str) -> None:
    print(f"[ws-gate-driver] {time.strftime('%H:%M:%S')} {msg}", flush=True)


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="TestPilot WS 闸门驱动（反向监听 C/S Mac 侧客户端）")
    p.add_argument("--url", default="ws://127.0.0.1:9876/ws/hybrid/hsess-p0")
    p.add_argument("--session", default="hsess-p0")
    p.add_argument("--token", default="tok-p0")
    p.add_argument("--gates", required=True, help="闸门声明 JSON 路径")
    p.add_argument("--structural", action="store_true",
                   help="结构跑：使用 auto_values 自动裁决（也可用环境变量 TP_TCA1_STRUCTURAL=1）")
    p.add_argument("--auto-delay", type=float, default=1.0,
                   help="结构跑每闸自动裁决前的延迟秒数（保活帧调度留痕）")
    p.add_argument("--evidence-dir", default="./ws-gate-evidence")
    p.add_argument("--retry-seconds", type=float, default=300.0)
    p.add_argument("--transcript-out", default=None,
                   help="驱动侧会话转录 JSON 路径；complete/fail-loud/连不上均落盘（不含 token）")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)

    structural = args.structural or os.environ.get("TP_TCA1_STRUCTURAL") == "1"
    try:
        gates = load_gates(args.gates, structural=structural)
    except GateSpecError as exc:
        _log(f"闸门声明校验失败：{exc}")
        return 2

    async def _amain() -> int:
        conn = await WebsocketsConnection.connect(args.url, args.retry_seconds, _log)
        return await run_session(
            conn,
            gates,
            session=args.session,
            token=args.token,
            structural=structural,
            evidence_dir=args.evidence_dir,
            prompt_func=default_prompt,
            auto_delay=args.auto_delay,
            transcript_out=args.transcript_out,
            log=_log,
        )

    try:
        return asyncio.run(_amain())
    except RuntimeError as exc:  # 重试窗口耗尽
        _log(str(exc))
        if args.transcript_out:
            write_driver_transcript(
                args.transcript_out,
                _build_transcript(
                    session=args.session,
                    structural=structural,
                    outcome=OUTCOME_CONNECT_TIMEOUT,
                    reason=str(exc),
                    stats={"hb": 0, "gates": [], "rejected": []},
                    gate_records=[],
                    evidence_records=[],
                    close_code=None,
                ),
                _log,
            )
        return 2


if __name__ == "__main__":
    sys.exit(main())
