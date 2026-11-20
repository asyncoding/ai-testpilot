"""核心错误类型与降级标记（docs/01 §0：检索为空/冲突时显式降级，不编造）。"""

from __future__ import annotations


class TestPilotError(Exception):
    """所有核心错误的基类。"""


class AdapterUnavailableError(TestPilotError):
    """外部系统/适配器不可用（超时、连接失败、凭证失效）。"""


class PathMissingAtCommitError(TestPilotError):
    """仓库可达，但指定 commit 下不存在该路径（git show 退出 128 的可识别子类）。

    live-verify 据此判 exists=False（证据在目标版本消失），而不是 degraded（仓库不可达）。
    """


class UnsupportedCapabilityError(TestPilotError):
    """环境不满足能力要求（如模拟器上请求真机后台/签名验证）。

    docs/08 FR-EX-004：必须显式拒绝并说明未覆盖层，不得冒充已验。
    """


class PolicyDeniedError(TestPilotError):
    """写操作未经审批/越权被策略层拒绝（docs/08 FR-CON-004 AC3）。"""


class SchemaValidationError(TestPilotError):
    """适配器返回数据无法归一化为核心模型（契约错误）。"""


class EvidenceInsufficientError(TestPilotError):
    """证据不足以支撑结论；工作流应降级为 NEEDS_HUMAN/“未知”，不得硬归因。"""


class NotFoundError(TestPilotError):
    """引用的资源不存在（如对不存在的 qa_run 写反馈）。

    S1.4 用于拒绝孤儿 feedback 行（feedback.target_urn NOT NULL 且无 DB FK，
    服务层是唯一守卫）；S1.5 HTTP 层直接映射 404。
    """
