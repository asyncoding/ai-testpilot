"""ws_gate_transcript hostless 测试（B1 账本：闭集 id、原子写、证据 fail-loud、URN 回填）。

红证纪律同其他转正切片：变异-复原双向、零 TEMP-RED 残留。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from testpilot_adapters.localfs_artifact import LocalFsArtifactAdapter
from testpilot_core.urn import parse_urn
from testpilot_workers import ws_gate_transcript as wt
from testpilot_workers.ws_gate_driver import DRIVER_TRANSCRIPT_SCHEMA
from testpilot_workers.ws_gate_transcript import (
    EvidenceRecord,
    GateRecord,
    InjectionRecord,
    ProcessRecord,
    TranscriptError,
    UnsafeRelaySessionIdError,
    WsGateTranscript,
    assert_evidence_satisfied,
    backfill_evidence_urns,
    load_transcript,
    new_relay_session_id,
    parse_driver_transcript,
    write_transcript,
)

NOW = datetime(2026, 11, 18, 9, 30, 0, tzinfo=timezone.utc)


# ── 构造助手 ────────────────────────────────────────────────────────────────


def _injection() -> InjectionRecord:
    return InjectionRecord(
        source_xctestrun="/data/DerivedData/Alarm.iphonesimulator.xctestrun",
        injected_xctestrun="/data/DerivedData/Alarm.injected.xctestrun",
        injected_sha256="a" * 64,
        # 只存键名：TP_TOKEN 的值结构上不可达。
        env_keys=["TP_LISTEN_PORT", "TP_SESSION", "TP_TOKEN", "TP_TCA1_STRUCTURAL"],
    )


def _transcript(relay_session_id: str, **overrides) -> WsGateTranscript:
    base = dict(
        relay_session_id=relay_session_id,
        session="hsess-p0",
        profile="ios-local-poc-xcode",
        commit_sha="cc4ddd9",
        target="sim",
        channel="loopback",
        structural=True,
        injection=_injection(),
        gates=[
            GateRecord(gate_id="g-lock", verdict="approve", rejected=False),
            GateRecord(gate_id="g-sensory", verdict="approve", rejected=False,
                       values={"confidence": "high"}),
        ],
        evidence=[
            EvidenceRecord(seq=1, kind="screenshot",
                           saved_name="evidence-01-screenshot.png", size_bytes=16),
        ],
        stats={"hb": 2, "gates": ["g-lock", "g-sensory"], "rejected": []},
        processes=[ProcessRecord(name="driver", rc=0, duration_s=18.4, close_code=1000)],
        outcome="passed",
        started_at="2026-11-18T09:30:00Z",
        finished_at="2026-11-18T09:30:18Z",
    )
    base.update(overrides)
    return WsGateTranscript(**base)


def _driver_transcript(outcome: str = "complete", *, rejected: bool = False,
                       evidence: list[dict] | None = None) -> dict:
    gate_records = [
        {"gate_id": "g-lock", "verdict": "reject" if rejected else "approve",
         "values": {}, "rejected": rejected},
    ]
    return {
        "schema_version": DRIVER_TRANSCRIPT_SCHEMA,
        "protocol": "tp-hybrid-ws-p0",
        "session": "hsess-p0",
        "structural": True,
        "outcome": outcome,
        "reason": "",
        "stats": {"hb": 0, "gates": [], "rejected": ["g-lock"] if rejected else []},
        "gate_records": gate_records,
        "evidence": evidence if evidence is not None else [
            {"seq": 1, "kind": "screenshot",
             "saved_name": "evidence-01-screenshot.png", "bytes": 16},
        ],
        "close_code": 1000 if outcome == "complete" else 1008,
        "finished_at": "2026-11-18T09:30:18Z",
    }


# ── 1. 会话 id 闭集 ─────────────────────────────────────────────────────────


def test_relay_session_id_deterministic_format():
    sid = new_relay_session_id(NOW, "hsess-p0|sim|TC-A1")
    assert sid == "wssess-20261118T093000Z-" + sid.rsplit("-", 1)[1]
    assert new_relay_session_id(NOW, "hsess-p0|sim|TC-A1") == sid
    # 时间戳必须带时区。
    with pytest.raises(TranscriptError):
        new_relay_session_id(datetime(2026, 11, 18, 9, 30), "seed")


@pytest.mark.parametrize("bad_id", [
    "wssess-20261118T093000Z-deadbeef/../../etc",
    "../wssess-20261118T093000Z-deadbeef",
    "/etc/wssess-20261118T093000Z-deadbeef",
    "wssess-20261118T093000Z-short",          # hash 不足 8 位
    "wssess-20261118-093000Z-deadbeef",       # 时间戳形态错
    "hsess-20261118T093000Z-deadbeef",        # 前缀错
    "wssess-20261118T093000Z-DEADBEEF",       # 大写越界
    "",
])
def test_relay_session_id_closed_set_rejects_traversal(bad_id):
    with pytest.raises(UnsafeRelaySessionIdError):
        wt.validate_relay_session_id(bad_id)


def test_pydantic_model_rejects_bad_relay_session_id():
    with pytest.raises(Exception):  # pydantic ValidationError 包了 UnsafeRelaySessionIdError
        _transcript("wssess-20261118T093000Z-../etc")


# ── 2. 模型闭集校验 ─────────────────────────────────────────────────────────


def test_injection_sha256_must_be_64_hex():
    with pytest.raises(Exception):
        InjectionRecord(
            source_xctestrun="a", injected_xctestrun="b",
            injected_sha256="xyz", env_keys=[],
        )


def test_gate_record_rejected_must_match_verdict():
    GateRecord(gate_id="g", verdict="reject", rejected=True)
    GateRecord(gate_id="g", verdict="approve", rejected=False)
    with pytest.raises(Exception):
        GateRecord(gate_id="g", verdict="approve", rejected=True)
    with pytest.raises(Exception):
        GateRecord(gate_id="g", verdict="reject", rejected=False)


def test_outcome_and_process_name_are_closed_sets():
    with pytest.raises(Exception):
        _transcript(new_relay_session_id(NOW, "s"), outcome="green")
    with pytest.raises(Exception):
        ProcessRecord(name="curl", rc=0, duration_s=1.0)


# ── 3. 原子写/读回 + 文件名闭集 ─────────────────────────────────────────────


def test_write_and_load_roundtrip_atomic_no_tmp_residue(tmp_path: Path):
    sid = new_relay_session_id(NOW, "seed-1")
    transcript = _transcript(sid)
    path = write_transcript(tmp_path, sid, transcript)

    assert path == tmp_path / "ws-relay-sessions" / sid / "transcript.json"
    assert path.name == wt.TRANSCRIPT_FILENAME
    # 同目录只有正式转录，无 tmp 残留。
    assert sorted(p.name for p in path.parent.iterdir()) == ["transcript.json"]
    loaded = load_transcript(path)
    assert loaded == transcript
    assert loaded.test_run_ref is None  # 一期占位槽恒 null


def test_write_rejects_traversal_before_touching_fs(tmp_path: Path):
    with pytest.raises(UnsafeRelaySessionIdError):
        write_transcript(tmp_path, "../escape", _transcript(
            new_relay_session_id(NOW, "s")))
    assert not (tmp_path.parent / "escape").exists()


# ── 4. 驱动转录跨 schema 消费 ───────────────────────────────────────────────


@pytest.mark.parametrize("outcome", ["complete", "fail_loud", "connect_timeout"])
def test_parse_driver_transcript_accepts_three_outcomes(outcome):
    parsed = parse_driver_transcript(_driver_transcript(outcome))
    assert parsed["outcome"] == outcome


def test_parse_driver_transcript_rejects_bad_schema_and_shape():
    raw = _driver_transcript()
    raw["schema_version"] = "something-else"
    with pytest.raises(TranscriptError, match="schema"):
        parse_driver_transcript(raw)

    raw = _driver_transcript("mystery")
    with pytest.raises(TranscriptError, match="outcome"):
        parse_driver_transcript(raw)

    raw = _driver_transcript()
    del raw["stats"]
    with pytest.raises(TranscriptError, match="必填字段"):
        parse_driver_transcript(raw)

    with pytest.raises(TranscriptError):
        parse_driver_transcript("not-a-dict")  # type: ignore[arg-type]


# ── 5. 证据 fail-loud（passed 路拒空壳，reject/红证豁免） ────────────────────


def test_evidence_satisfied_green(tmp_path: Path):
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    (evidence_dir / "evidence-01-screenshot.png").write_bytes(b"PNG-FAKE-BYTES")
    # 不抛即绿。
    assert_evidence_satisfied(
        require_evidence_gate_count=1,
        driver_transcript=_driver_transcript(),
        evidence_dir=evidence_dir,
    )


def test_missing_evidence_record_rejects_passed(tmp_path: Path):
    with pytest.raises(TranscriptError, match="证据缺失"):
        assert_evidence_satisfied(
            require_evidence_gate_count=2,
            driver_transcript=_driver_transcript(evidence=[]),
            evidence_dir=tmp_path,
        )


def test_missing_evidence_file_rejects_passed(tmp_path: Path):
    # 转录有条目但文件不在磁盘：空壳 URN 防线。
    with pytest.raises(TranscriptError, match="应到未到"):
        assert_evidence_satisfied(
            require_evidence_gate_count=1,
            driver_transcript=_driver_transcript(),
            evidence_dir=tmp_path / "evidence",
        )


def test_empty_evidence_file_rejects(tmp_path: Path):
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    (evidence_dir / "evidence-01-screenshot.png").write_bytes(b"")
    with pytest.raises(TranscriptError, match="空壳"):
        assert_evidence_satisfied(
            require_evidence_gate_count=1,
            driver_transcript=_driver_transcript(),
            evidence_dir=evidence_dir,
        )


@pytest.mark.parametrize("outcome,rejected", [
    ("fail_loud", False),
    ("connect_timeout", False),
    ("complete", True),  # reject 早夭：驱动仍以 complete 收口但无证据，合法
])
def test_evidence_requirement_exempted_for_red_and_reject(tmp_path, outcome, rejected):
    assert_evidence_satisfied(
        require_evidence_gate_count=1,
        driver_transcript=_driver_transcript(outcome, rejected=rejected, evidence=[]),
        evidence_dir=tmp_path / "evidence",
    )


# ── 6. URN 回填 ─────────────────────────────────────────────────────────────


def test_backfill_urns_with_real_localfs_adapter(tmp_path: Path):
    evidence_dir = tmp_path / "session" / "evidence"
    evidence_dir.mkdir(parents=True)
    png = evidence_dir / "evidence-01-screenshot.png"
    png.write_bytes(b"PNG-FAKE-BYTES")
    artifacts_root = tmp_path / "artifacts"
    adapter = LocalFsArtifactAdapter(artifacts_root)

    sid = new_relay_session_id(NOW, "seed-urn")
    transcript = _transcript(sid)

    result = asyncio.run(backfill_evidence_urns(
        transcript, evidence_dir=evidence_dir, artifacts=adapter))

    assert result.evidence[0].urn
    system, resource_type, external_id, version = parse_urn(result.evidence[0].urn)
    assert system == "localfs-artifact"
    assert resource_type == "screenshot"
    assert version is not None  # sha256 前 12 位版本段
    # 制品文件确实落盘且内容一致。
    stored = artifacts_root / external_id
    assert stored.read_bytes() == b"PNG-FAKE-BYTES"
    # 入参模型不被原地修改。
    assert transcript.evidence[0].urn == ""


def test_backfill_missing_file_raises(tmp_path: Path):
    sid = new_relay_session_id(NOW, "seed-miss")
    transcript = _transcript(sid)
    with pytest.raises(TranscriptError, match="应到未到"):
        asyncio.run(backfill_evidence_urns(
            transcript, evidence_dir=tmp_path / "gone",
            artifacts=LocalFsArtifactAdapter(tmp_path / "art")))


class _RaisingArtifactAdapter:
    async def put(self, local_path: str, metadata: dict) -> str:
        raise RuntimeError("artifact store down")

    async def resolve_uri(self, urn: str) -> str:
        raise NotImplementedError


def test_backfill_adapter_error_no_fake_urn_and_no_disk_transcript(tmp_path: Path):
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    (evidence_dir / "evidence-01-screenshot.png").write_bytes(b"PNG-FAKE-BYTES")
    sid = new_relay_session_id(NOW, "seed-raise")
    transcript = _transcript(sid)

    with pytest.raises(RuntimeError, match="artifact store down"):
        asyncio.run(backfill_evidence_urns(
            transcript, evidence_dir=evidence_dir,
            artifacts=_RaisingArtifactAdapter()))

    # 没有假 URN、没有半截转录落盘。
    assert transcript.evidence[0].urn == ""
    assert not list((tmp_path / "ws-relay-sessions").glob("**/transcript.json")) \
        if (tmp_path / "ws-relay-sessions").exists() else True


# ── 7. 密钥红线 ─────────────────────────────────────────────────────────────


def test_serialized_transcript_never_contains_token(tmp_path: Path):
    sid = new_relay_session_id(NOW, "seed-secret")
    path = write_transcript(tmp_path, sid, _transcript(sid))
    raw = path.read_text(encoding="utf-8")
    doc = json.loads(raw)
    assert "tok-p0" not in raw
    assert "WRONG-TOKEN" not in raw
    # 注入台账只有键名。
    assert doc["injection"]["env_keys"] == [
        "TP_LISTEN_PORT", "TP_SESSION", "TP_TOKEN", "TP_TCA1_STRUCTURAL"]
    assert all("token" not in k.lower() or k == "TP_TOKEN"
               for k in doc["injection"]["env_keys"])
