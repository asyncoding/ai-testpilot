"""审批 API 的 HTTP 请求模型（编排层）；core 的 CaseDecisionRequest 保持冻结。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from testpilot_core.enums import ApprovalDecision, OracleType


class SuggestionDecisionHttp(BaseModel):
    suggestion_urn: str = Field(min_length=1)
    action: Literal["adopt", "modify", "reject"]
    # modify 时可改类型/期望；adopt/reject 携带会被 core 拒绝（fail-closed）
    oracle_type: OracleType | None = None
    expectation: str | None = None


class CaseDecisionHttp(BaseModel):
    decision: ApprovalDecision
    reviewer: str = Field(min_length=1, description="评审人，POC 单用户无认证但必填")
    reason: str = ""
    items: list[SuggestionDecisionHttp] = Field(min_length=1)


class WritePolicyHttp(BaseModel):
    """kill switch 运行期翻转；只允许收紧，profile 关着时请求放开会被 403。"""

    runtime_disabled: bool
