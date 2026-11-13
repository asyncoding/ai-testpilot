"""人工闸门 API 的 HTTP 请求模型（编排层）；core 的 PlanStep/HumanVerdict 冻结。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from testpilot_core.enums import StepActor
from testpilot_core.hybrid_plan import VerdictField, VerdictForm

if TYPE_CHECKING:
    from testpilot_core.hybrid_plan import PlanStep


class GateOpenHttp(BaseModel):
    """挂出闸门的请求；H3 编排器与本 HTTP 路径共用同一套 core 构造。

    actor 放开为全枚举：自动步进闸门由 core build_gate_request 统一拒绝，
    错误体走 HybridPlanError→422 映射，而不是 pydantic 原生 422 两套格式。
    """

    session_id: str = Field(min_length=1)
    step_id: str = Field(min_length=1)
    actor: StepActor
    action: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    params: dict = Field(default_factory=dict)
    verdict_form: VerdictForm | None = None
    evidence_urns: list[str] = Field(default_factory=list)
    deadline_seconds: int = Field(default=300, gt=0, le=3600)

    def to_plan_step(self) -> PlanStep:
        from testpilot_core.hybrid_plan import PlanStep

        return PlanStep(
            step_id=self.step_id,
            actor=StepActor(self.actor),
            action=self.action,
            params=self.params,
            prompt=self.prompt,
            verdict_form=self.verdict_form,
            evidence_bindings=list(self.evidence_urns),
            # API 挂单的幂等键由会话+步骤确定性派生，与 gate_id 同源
            idempotency_key=f"{self.session_id}|{self.step_id}|gate",
        )


class GateRespondHttp(BaseModel):
    """人工响应；decided_at 由服务端盖章，客户端时钟不被信任。"""

    values: dict = Field(default_factory=dict)
    evidence_urns: list[str] = Field(default_factory=list)
    note: str = ""
    decided_by: str = Field(min_length=1, description="见证人，POC 无认证但必填")


class GateRenewHttp(BaseModel):
    extra_seconds: int = Field(gt=0, le=3600)


# 供外部（测试/H3）复用的表单模型再导出
__all__ = [
    "GateOpenHttp",
    "GateRespondHttp",
    "GateRenewHttp",
    "VerdictField",
    "VerdictForm",
]
