"""外部副作用策略（docs/04 §7.2 / docs/08 FR-WR-001、FR-CON-004）。

R0 只读 / R1 内部草稿：自动放行；
R2（MR 评论、用例回写）：需要已批准的 Approval；
R3（建工单、工单流转、取消执行）：需要已批准的 Approval；
R4（删数据、生产操作）：POC 一律禁止。
"""

from __future__ import annotations

from .enums import ActionLevel, ApprovalDecision
from .errors import PolicyDeniedError
from .models import Approval

_AUTO_LEVELS = {ActionLevel.R0_READ, ActionLevel.R1_INTERNAL_DRAFT}
_REVIEWED_LEVELS = {ActionLevel.R2_REVIEWED_WRITE, ActionLevel.R3_APPROVED_EXTERNAL}


def assert_writable(level: ActionLevel, approval: Approval | None, *, global_kill_switch: bool = False) -> None:
    """写操作统一守卫。全局停用开关打开时，R2/R3 立即拒绝（docs/08 FR-UI-006）。"""
    if level in _AUTO_LEVELS:
        return
    if level == ActionLevel.R4_FORBIDDEN:
        raise PolicyDeniedError("R4 动作（删数据/生产操作）在 POC 中禁止")
    if level not in _REVIEWED_LEVELS:  # pragma: no cover - 枚举穷尽防御
        raise PolicyDeniedError(f"未知动作分级: {level}")
    if global_kill_switch:
        raise PolicyDeniedError("外部写全局停用开关已打开（kill switch）")
    if approval is None or approval.decision != ApprovalDecision.APPROVED:
        raise PolicyDeniedError(f"{level} 写操作未经人审批准")
    if approval.action_level != level:
        raise PolicyDeniedError(
            f"审批分级 {approval.action_level} 与请求分级 {level} 不一致"
        )
