"""E5 workers 语义裁判适配测试（不触 ollama，FakeGateway 主机能）。

分工：core.semantic_judge 守裁判规则与 kappa；本文件守 prompt 装配、
裁判模型隔离（model 覆盖 deepseek-r1:7b）、降级/异常的 fail-closed 与单题隔离。
"""

from __future__ import annotations

import pytest

from testpilot_core.semantic_judge import SemanticVerdict
from testpilot_workers.semantic_judge import (
    JUDGE_JSON_SCHEMA,
    JUDGE_PROMPT_VERSION,
    build_judge_messages,
    run_semantic_judgement,
)

ANSWER_MD = "## 当前结论\nkill-backup 补发间隔为 30 秒。"
EVIDENCE = [
    {"urn": "urn:testpilot:fs-docs:chunk:docs/x.md#s1",
     "doc_urn": "urn:testpilot:fs-docs:document:docs/x.md",
     "text": "static let backupInterval: TimeInterval = 30"},
    {"urn": "urn:testpilot:fs-docs:chunk:docs/y.md#s2",
     "doc_urn": "urn:testpilot:fs-docs:document:docs/y.md",
     "text": "侧边键兜底在 iOS 17 上走本地通知。"},
]


class _FakeGateway:
    def __init__(self, envelope: dict | None):
        self._envelope = envelope
        self.calls: list[dict] = []

    async def structured(self, messages, schema, **options):  # noqa: ANN001
        self.calls.append({"messages": messages, "schema": schema, "options": options})
        if self._envelope is None:
            return {"data": None, "degraded": True, "attempts": 2,
                    "model": "deepseek-r1:7b", "tokens_in": 10, "tokens_out": 5,
                    "latency_ms": 100, "text": "not-json"}
        return {"data": self._envelope, "degraded": False, "attempts": 1,
                "model": options.get("model", "deepseek-r1:7b"),
                "tokens_in": 100, "tokens_out": 40, "latency_ms": 500, "text": "{}"}


def _good_envelope() -> dict:
    return {
        "claims": [
            {"claim": "kill-backup 补发间隔为 30 秒", "supported": True,
             "evidence_urn": EVIDENCE[0]["urn"], "reason": "源码常量即 30"},
        ],
        "relevance": {"passed": True, "reason": "直接回应间隔问题"},
    }


def test_build_judge_messages_contains_question_evidence_and_answer():
    messages = build_judge_messages("间隔多少秒？", EVIDENCE, ANSWER_MD)
    assert [m["role"] for m in messages] == ["system", "user"]
    blob = messages[1]["content"]
    assert "间隔多少秒？" in blob and ANSWER_MD in blob
    assert EVIDENCE[0]["urn"] in blob and "backupInterval" in blob
    # 裁判纪律写进 system prompt：只按给定证据、不给情面、结构化输出
    assert "证据" in messages[0]["content"]


async def test_run_judgement_fail_closed_on_degraded_envelope():
    out = await run_semantic_judgement(
        _FakeGateway(None), [("G01", "q", EVIDENCE, ANSWER_MD)],
        judge_model="deepseek-r1:7b")
    v = out["G01"]
    assert isinstance(v, SemanticVerdict) and v.parse_error
    assert v.faithfulness_passed is False and v.relevance_passed is False


async def test_run_judgement_fail_closed_on_malformed_payload():
    out = await run_semantic_judgement(
        _FakeGateway({"claims": []}), [("G01", "q", EVIDENCE, ANSWER_MD)],
        judge_model="deepseek-r1:7b")
    assert out["G01"].parse_error


async def test_run_judgement_happy_path_forwards_isolated_model_and_schema():
    gw = _FakeGateway(_good_envelope())
    out = await run_semantic_judgement(
        gw, [("G01", "间隔多少秒？", EVIDENCE, ANSWER_MD)],
        judge_model="deepseek-r1:7b")
    v = out["G01"]
    assert v.judged_ok and v.faithfulness_passed and v.relevance_passed
    # 裁判模型必须与生成模型不同体：model 显式覆盖到 deepseek-r1:7b
    assert gw.calls[0]["options"]["model"] == "deepseek-r1:7b"
    assert gw.calls[0]["schema"] is JUDGE_JSON_SCHEMA


async def test_run_judgement_isolates_per_case_exception():
    class _Mixed:
        def __init__(self):
            self.n = 0

        async def structured(self, messages, schema, **options):  # noqa: ANN001
            self.n += 1
            if self.n == 1:
                raise RuntimeError("boom")
            return {"data": _good_envelope(), "degraded": False, "attempts": 1,
                    "model": "deepseek-r1:7b", "tokens_in": 1, "tokens_out": 1,
                    "latency_ms": 1, "text": ""}

    out = await run_semantic_judgement(
        _Mixed(),
        [("G01", "q1", EVIDENCE, ANSWER_MD), ("G02", "q2", EVIDENCE, ANSWER_MD)],
        judge_model="deepseek-r1:7b")
    assert out["G01"].parse_error  # 第一题异常被收口
    assert out["G02"].judged_ok    # 第二题照常完成，整批不中断


def test_prompt_version_constant_pinned():
    assert JUDGE_PROMPT_VERSION == "semantic-judge-1"
