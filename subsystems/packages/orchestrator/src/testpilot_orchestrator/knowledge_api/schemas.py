"""知识管理 API 请求体（FR-UI-004，S4.5）。

权威等级直接复用 core 闭集枚举：非法值在 pydantic 边界 422，路由层不再手写白名单。
"""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator

from testpilot_core.enums import AuthorityLevel


class GovernanceEdit(BaseModel):
    """知识 owner / 权威等级编辑（R1 内部写，审计历史落 raw_json 治理段）。"""

    owner: str | None = Field(default=None, description="知识 owner；传 null 表示本次不改")
    authority_level: AuthorityLevel | None = Field(
        default=None, description="权威等级人工覆盖；闭集 L0/L1/L2/L3"
    )
    edited_by: str = Field(default="web-console", min_length=1)

    @model_validator(mode="after")
    def _at_least_one_field(self) -> "GovernanceEdit":
        if self.owner is None and self.authority_level is None:
            raise ValueError("owner 与 authority_level 至少提供一个，空编辑不允许落账")
        if self.owner is not None and not self.owner.strip():
            raise ValueError("owner 不允许空白字符串")
        if not self.edited_by.strip():
            raise ValueError("edited_by 不允许空白（审计必须留操作人）")
        return self
