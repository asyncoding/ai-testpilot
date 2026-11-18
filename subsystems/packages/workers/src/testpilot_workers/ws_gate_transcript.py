"""ws-gate-transcript-v1 正式闸门转录（B1 账本：本地 JSON + localfs URN）。

一期定案（2026-11-18 计划裁决 #3）：USB relay 会话不写 FileGateStore、不碰
test_runs 表——core 四态没有 rejected、gate id 闭集不同构、双时钟会给假续期。
账本形态是 data_dir/ws-relay-sessions/wssess-<UTCstamp>-<hash8>/transcript.json，
与 hybrid-sessions/、gates/ 同级同纪律（零 DDL，JSON 制品加字段带默认值）。

本模块只管四件事，全部可 hostless 测试（artifact 适配器走 Protocol 注入）：

1. 转录结构（pydantic 闭集校验）：outcome 与 ws-run 退出码 0/2/3 一一对应；
2. 会话 id/文件名闭集：wssess-<YYYYMMDDTHHMMSSZ>-<8hex> 正则拒绝路径穿越，
   转录文件名硬编码 transcript.json，调用方无法传入别的名字；
3. 证据完整性：passed 路必须证据齐（require_evidence 闸门数 ≤ 证据数且文件
   真实存在）；reject 早夭/协议红证本就无证据，按 outcome 区别不误报；
4. URN 回填：证据文件经 ArtifactStorePort.put 落 localfs 制品根并回填 URN，
   适配器抛错则整体失败，绝不写假 URN/半截转录。

密钥红线：模型没有任何 token 字段；注入记录只存环境变量**键名**，值（含
TP_TOKEN）结构上不可达，序列化结果不可能泄露明文。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from testpilot_workers.ws_gate_driver import DRIVER_TRANSCRIPT_SCHEMA

WS_GATE_TRANSCRIPT_SCHEMA = "ws-gate-transcript-v1"
RELAY_DIRNAME = "ws-relay-sessions"
TRANSCRIPT_FILENAME = "transcript.json"
EVIDENCE_DIRNAME = "evidence"

# 与 FileHybridSessionStore 的 hsess-<12hex> 同纪律：闭集正则是路径穿越的唯一闸门。
_SAFE_RELAY_SESSION_ID = re.compile(r"^wssess-\d{8}T\d{6}Z-[0-9a-f]{8}$")

RelayOutcome = Literal["passed", "product_failure", "protocol_error", "hard_error"]
RelayTarget = Literal["sim", "device"]
RelayChannel = Literal["loopback", "usb"]
GateVerdict = Literal["approve", "reject"]

# 驱动转录（ws-gate-driver-transcript-v1）的 outcome 闭集，跨 schema 消费只认这四个。
# peer_closed：对端在 step_finished 前断链（reject→Runner XCTAssert 抛错拆连接的
# 标准收口），ws-run 按 gate_records 是否含 reject 分型产品失败/硬错误。
_DRIVER_OUTCOMES = frozenset(
    {"complete", "fail_loud", "connect_timeout", "peer_closed"}
)


class TranscriptError(ValueError):
    """转录构建/校验失败；ws-run 编排器统一映射 exit 2（硬错误）。"""


class UnsafeRelaySessionIdError(TranscriptError):
    """relay 会话 id 不匹配闭集正则：拒绝一切路径拼接，防穿越。"""


# ── pydantic 结构 ───────────────────────────────────────────────────────────


class InjectionRecord(BaseModel):
    """xctestrun 注入台账：只记键名与哈希，环境变量值（含 token）结构上不可达。"""

    source_xctestrun: str
    injected_xctestrun: str
    injected_sha256: str
    env_keys: list[str] = Field(default_factory=list)

    @field_validator("injected_sha256")
    @classmethod
    def _sha256_hex(cls, value: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError("injected_sha256 必须是 64 位小写十六进制")
        return value


class GateRecord(BaseModel):
    gate_id: str
    verdict: GateVerdict
    values: dict[str, Any] = Field(default_factory=dict)
    rejected: bool = False

    @field_validator("rejected")
    @classmethod
    def _rejected_matches_verdict(cls, value: bool, info) -> bool:
        verdict = info.data.get("verdict")
        if verdict is not None and value != (verdict == "reject"):
            raise ValueError("rejected 必须与 verdict == 'reject' 同真假")
        return value


class EvidenceRecord(BaseModel):
    seq: int = Field(gt=0)
    kind: str
    saved_name: str
    size_bytes: int = Field(ge=0)
    # URN 回填前为空串；回填失败时转录不得写出（编排器在 write 前先 backfill）。
    urn: str = ""


class ProcessRecord(BaseModel):
    """子进程对账：driver/xcodebuild/iproxy 三选一，rc=None 表示被编排器收口杀掉。"""

    name: Literal["driver", "xcodebuild", "iproxy"]
    rc: int | None = None
    duration_s: float = Field(ge=0.0)
    close_code: int | None = None


class WsGateTranscript(BaseModel):
    schema_version: Literal[WS_GATE_TRANSCRIPT_SCHEMA] = WS_GATE_TRANSCRIPT_SCHEMA
    protocol: str = "tp-hybrid-ws-p0"
    relay_session_id: str
    # 线协议会话（TP_SESSION），与 relay 会话 id 是两个层：前者对线，后者对账。
    session: str
    profile: str
    commit_sha: str
    target: RelayTarget
    channel: RelayChannel
    structural: bool
    injection: InjectionRecord
    gates: list[GateRecord] = Field(default_factory=list)
    evidence: list[EvidenceRecord] = Field(default_factory=list)
    stats: dict[str, Any] = Field(default_factory=dict)
    processes: list[ProcessRecord] = Field(default_factory=list)
    outcome: RelayOutcome
    reason: str = ""
    port_cleanup_verified: bool = False
    # 一期恒 null：二期 test_runs 终态直写导入器的回填槽，先占位防 schema 漂移。
    test_run_ref: str | None = None
    started_at: str
    finished_at: str

    @field_validator("relay_session_id")
    @classmethod
    def _session_id_closed_set(cls, value: str) -> str:
        validate_relay_session_id(value)
        return value


# ── 会话 id 与落盘路径 ───────────────────────────────────────────────────────


def validate_relay_session_id(relay_session_id: str) -> str:
    if not isinstance(relay_session_id, str) or not _SAFE_RELAY_SESSION_ID.fullmatch(
        relay_session_id
    ):
        raise UnsafeRelaySessionIdError(
            f"非法 relay 会话 id（拒绝路径拼接）: {relay_session_id!r}，"
            f"要求匹配 {_SAFE_RELAY_SESSION_ID.pattern}"
        )
    return relay_session_id


def new_relay_session_id(now: datetime, seed: str) -> str:
    """确定性派生 wssess-<UTCstamp>-<sha256(seed)[:8]>；seed 通常拼协议会话与参数。"""
    if now.tzinfo is None:
        raise TranscriptError("时间戳必须带时区信息")
    stamp = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:8]
    return f"wssess-{stamp}-{digest}"


def session_dir(root: str | Path, relay_session_id: str) -> Path:
    """会话目录 <root>/ws-relay-sessions/<id>；id 过闭集后才允许拼接。"""
    validate_relay_session_id(relay_session_id)
    return Path(root) / RELAY_DIRNAME / relay_session_id


def write_transcript(
    root: str | Path, relay_session_id: str, transcript: WsGateTranscript
) -> Path:
    """tmp + os.replace 原子写 transcript.json；文件名闭集硬编码，拒收外部传名。"""
    out_dir = session_dir(root, relay_session_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / TRANSCRIPT_FILENAME
    tmp = out_dir / f".{TRANSCRIPT_FILENAME}.tmp-{os.getpid()}"
    tmp.write_text(
        json.dumps(transcript.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)
    return path


def load_transcript(path: str | Path) -> WsGateTranscript:
    return WsGateTranscript.model_validate_json(Path(path).read_text(encoding="utf-8"))


# ── 驱动转录消费（跨 schema 边界，只认闭集字段） ─────────────────────────────


def parse_driver_transcript(raw: dict[str, Any]) -> dict[str, Any]:
    """校验 ws_gate_driver 落盘的驱动转录；越界 schema/outcome 一律 TranscriptError。"""
    if not isinstance(raw, dict):
        raise TranscriptError("驱动转录不是 JSON 对象")
    if raw.get("schema_version") != DRIVER_TRANSCRIPT_SCHEMA:
        raise TranscriptError(
            f"驱动转录 schema 不匹配：期望 {DRIVER_TRANSCRIPT_SCHEMA}，"
            f"实际 {raw.get('schema_version')!r}"
        )
    outcome = raw.get("outcome")
    if outcome not in _DRIVER_OUTCOMES:
        raise TranscriptError(
            f"驱动转录 outcome 越界：{outcome!r}，合法 {sorted(_DRIVER_OUTCOMES)}"
        )
    for key in ("gate_records", "evidence", "stats", "close_code"):
        if key not in raw:
            raise TranscriptError(f"驱动转录缺必填字段：{key}")
    if not isinstance(raw["gate_records"], list) or not isinstance(raw["evidence"], list):
        raise TranscriptError("驱动转录 gate_records/evidence 必须是数组")
    return raw


def _has_rejection(driver_transcript: dict[str, Any]) -> bool:
    return any(bool(record.get("rejected")) for record in driver_transcript["gate_records"])


def assert_evidence_satisfied(
    *,
    require_evidence_gate_count: int,
    driver_transcript: dict[str, Any],
    evidence_dir: str | Path,
) -> None:
    """passed 路证据 fail-loud 校验；reject 早夭/协议红证按 outcome 豁免。

    协议证据帧不绑 gate_id，数量约束为「要求证据的闸门数 ≤ 实到证据数」，
    并逐条核对 saved_name 文件真实存在且非空，防空壳 URN。
    """
    outcome = driver_transcript.get("outcome")
    if outcome != "complete" or _has_rejection(driver_transcript):
        # fail_loud/connect_timeout 没有证据是本分；reject 合法早夭（S8 已验）。
        return

    records = driver_transcript["evidence"]
    if require_evidence_gate_count > len(records):
        raise TranscriptError(
            f"证据缺失：{require_evidence_gate_count} 个 require_evidence 闸门，"
            f"驱动转录仅 {len(records)} 条证据，拒绝产出 passed 转录"
        )
    base = Path(evidence_dir)
    for record in records:
        saved = base / str(record["saved_name"])
        if not saved.is_file():
            raise TranscriptError(f"证据文件应到未到：{saved}")
        if saved.stat().st_size == 0:
            raise TranscriptError(f"证据文件为空壳：{saved}")


# ── URN 回填 ─────────────────────────────────────────────────────────────────


async def backfill_evidence_urns(
    transcript: WsGateTranscript,
    *,
    evidence_dir: str | Path,
    artifacts,  # ArtifactStorePort；鸭子类型，测试可注入假件
) -> WsGateTranscript:
    """把证据文件逐个 put 制品库并回填 URN。

    文件缺失或适配器抛错都原样上抛（TranscriptError/底层异常），已回填的
    URN 只存在于返回的新模型上；调用方在本函数成功前不得 write_transcript，
    因此磁盘上不可能出现假 URN/半截转录。
    """
    base = Path(evidence_dir)
    updated: list[EvidenceRecord] = []
    for record in transcript.evidence:
        local = base / record.saved_name
        if not local.is_file():
            raise TranscriptError(f"证据文件应到未到，拒绝回填 URN：{local}")
        urn = await artifacts.put(str(local), {"type": record.kind})
        if not isinstance(urn, str) or not urn:
            raise TranscriptError(f"制品适配器返回非法 URN：{urn!r}")
        updated.append(record.model_copy(update={"urn": urn}))
    return transcript.model_copy(update={"evidence": updated})
