"""资产 API 的 HTTP 请求模型（编排层）；core 的版本化纯逻辑保持冻结。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

AssetKindHttp = Literal["prompt", "few_shot", "golden", "glossary", "tool_whitelist"]


class AssetRevisionHttp(BaseModel):
    """登记新资产版本的请求体；asset_id 走路径段，版本号由服务端计算。"""

    kind: AssetKindHttp
    name: str = Field(min_length=1)
    content: dict = Field(min_length=1, description="非空结构化内容，整块文本放 template 键")
    author: str = ""
    note: str = ""


class PromoteFewshotHttp(BaseModel):
    """偏好样本回流 few-shot；缺省资产 ID 与 worker CLI 保持同一常量。"""

    asset_id: str | None = None
    name: str | None = None
    author: str = ""
    note: str = ""
