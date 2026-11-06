"""HTTP 请求模型（编排层）：core AskRequest 冻结，不在此改字段。

case_key 是编排层的演示故事选择器：recorded 模型按 qa_<kind>_<case_key> 取
录制响应，ollama 等真实模型忽略该字段。acl_scope 走请求体（POC 单用户，无 HTTP 认证）。
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from testpilot_core.enums import FeedbackKind, FeedbackReason


class AskHttpRequest(BaseModel):
    question: str = Field(min_length=1, description="用户问题，不允许空白")
    project: str = ""
    branch: str = "main"
    acl_scope: str = "default"
    case_key: str = ""

    @field_validator("question")
    @classmethod
    def _question_must_not_be_blank(cls, value: str) -> str:
        # min_length=1 挡不住纯空白；空白问题没有检索意义，直接 422
        if not value.strip():
            raise ValueError("question 不能为空白字符串")
        return value


class FeedbackHttpRequest(BaseModel):
    """FR-QA-004：👍/👎；not_useful 的 reason 取四原因枚举，非法值原生 422。"""

    kind: FeedbackKind
    reason: FeedbackReason | None = None
    detail: str = ""
