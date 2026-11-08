"""E5 语义裁判适配层（docs/06 §6.3，FR-EV-003）：隔离模型调用 + prompt 装配。

裁判纪律：
- 裁判模型（默认 deepseek-r1:7b）必须与生成模型（qwen2.5:7b）不同体，禁止同体自评；
  复用容器内 OllamaModelGateway.structured，仅以 model= 覆盖目标模型，零新基础设施。
- 裁判规则/解析/kappa 在 core.semantic_judge（零 IO 纯函数，红→绿测试独立守）；
  本模块只负责把 qa_runs 行装配成裁判消息、发请求、把降级/异常收口为 fail-closed。
- 单题异常不炸整批（G22 事故同款纪律），统一落 parse_error 的 SemanticVerdict。
"""

from __future__ import annotations

from typing import Any

from testpilot_core.semantic_judge import (
    SemanticVerdict,
    failed_semantic_judgement,
    parse_semantic_judgement,
)

JUDGE_PROMPT_VERSION = "semantic-judge-1"
DEFAULT_JUDGE_MODEL = "deepseek-r1:7b"

# Ollama format=<JSON Schema> 约束输出；与 core 解析器的 fail-closed 形状一一对应
JUDGE_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "claim": {"type": "string"},
                    "supported": {"type": "boolean"},
                    "evidence_urn": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["claim", "supported", "evidence_urn", "reason"],
            },
        },
        "relevance": {
            "type": "object",
            "properties": {
                "passed": {"type": "boolean"},
                "reason": {"type": "string"},
            },
            "required": ["passed", "reason"],
        },
    },
    "required": ["claims", "relevance"],
}

_SYSTEM_PROMPT = (
    "你是独立的 RAG 答案裁判，与答案生成模型不是同一个体。"
    "只依据用户给出的检索证据核验，不得使用参数化知识补全事实：\n"
    "1. 把答案拆成可核验的陈述（claims），逐条判断是否被证据原文支撑，"
    "被支撑必须填写对应的证据 URN，找不到证据即 supported=false；\n"
    "2. relevance 判断答案是否直接回应问题（跑题/套话/答非所问即 false）；\n"
    "3. 只输出约定 JSON，不解释、不寒暄、不输出 JSON 以外的字符。"
)


def build_judge_messages(
    question: str, evidence: list[dict[str, Any]], answer_markdown: str
) -> list[dict[str, str]]:
    """装配裁判消息（纯函数）：问题 + 带 URN 的证据原文 + 待裁答案。"""
    evidence_blocks = [
        f"【证据 {i + 1}】URN: {e.get('urn', '')}\n{str(e.get('text', ''))}"
        for i, e in enumerate(evidence)
    ]
    user_content = (
        f"## 问题\n{question}\n\n"
        f"## 检索证据\n{chr(10).join(evidence_blocks) or '（无证据）'}\n\n"
        f"## 待裁答案\n{answer_markdown}"
    )
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


async def run_semantic_judgement(
    model_gateway: Any,
    items: list[tuple[str, str, list[dict[str, Any]], str]],
    *,
    judge_model: str = DEFAULT_JUDGE_MODEL,
) -> dict[str, SemanticVerdict]:
    """对一批 (case_id, question, evidence, answer_md) 跑隔离裁判。

    每题独立 try：传输异常/结构化降级/畸形输出全部收口为 parse_error 判定，
    返回字典保证每个 case_id 都有键（调用方无需再处理缺失）。
    """
    out: dict[str, SemanticVerdict] = {}
    for case_id, question, evidence, answer_md in items:
        try:
            envelope = await model_gateway.structured(
                build_judge_messages(question, evidence, answer_md),
                JUDGE_JSON_SCHEMA,
                model=judge_model,
            )
        except Exception as exc:  # noqa: BLE001 — 裁判视角：异常即失败证据，继续下一题
            out[case_id] = failed_semantic_judgement(
                case_id, f"{type(exc).__name__}: {str(exc)[:200]}")
            continue
        if not envelope or envelope.get("data") is None:
            out[case_id] = failed_semantic_judgement(
                case_id,
                f"judge_model_degraded attempts={envelope.get('attempts') if envelope else 'none'}",
            )
            continue
        out[case_id] = parse_semantic_judgement(case_id, envelope["data"])
    return out
