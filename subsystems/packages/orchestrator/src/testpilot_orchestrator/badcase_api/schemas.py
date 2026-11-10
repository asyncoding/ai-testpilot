"""bad case API 的 HTTP 请求模型（编排层）；core.badcase 保持零框架依赖。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class TriageHttp(BaseModel):
    decision: Literal["accept", "reject"]
    actor: str = Field(min_length=1)
    reason: str = ""
    note: str = ""


class PromoteHttp(BaseModel):
    actor: str = Field(min_length=1)
    note: str = ""


class RegressionHttp(BaseModel):
    eval_run_id: str = Field(min_length=1)
    outcome: Literal["pass", "fail"]
    actor: str = Field(min_length=1)
    note: str = ""
