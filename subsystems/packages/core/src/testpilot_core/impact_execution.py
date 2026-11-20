"""impact-execution：影响面分析 → only-testing 选择集纯函数（docs/01 E4 切片）。

零 IO、零三方（仅 pydantic + 标准库）：吃 :class:`~testpilot_core.impact.ImpactAnalysis`
与 automation-map-v1 的 bindings，产出可直接交给 execution_service 的
``-only-testing`` 选择集、未映射清单、幂等键与重跑配额键。

纪律（docs/01 §4 E4）：

- 只有 ``runnable`` 分类的用例进自动选择；``no_automation``/``conflict_update``
  只报告不自动选（冲突用例的标记可能已失效，须人工更新后再纳入）；
- runnable 但 map 中无绑定 → 显式进未映射清单；选择集为空时 fail-closed，
  调用方必须 exit 非零，**禁止静默退化为全量执行**；
- 同一 Swift 方法被多个 TC 绑定时 only_testing 标识符去重；每个标识符再过
  :func:`~testpilot_core.xcode_commands.validate_only_testing` 形态校验，
  与 xcodebuild 适配器的空选择拒绝互证；
- map 的 ``head_commit`` 必须等于本次 ``--head``：演示分支前进后旧 map 过期，
  必须先重跑 testpilot-scan-automation，杜绝拿旧映射跑新代码；
- 幂等键 ``impact:<head短sha>:<选择集 hash>``：同参数重放由
  execution_service 收敛为同一 run_id，jobs 零增生。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel, Field

from .automation_mapping import AUTOMATION_MAP_SCHEMA_VERSION
from .impact import ImpactAnalysis
from .xcode_commands import validate_only_testing

# git 短 sha 取 12 位（与 impact tracer metadata 同口径）；选择集 hash 取 16 位
HEAD_SHORT_LEN = 12
SELECTION_DIGEST_LEN = 16


class ImpactSelectionError(ValueError):
    """选择集硬错误（空选择/map 过期/map 版本不符），worker 映射 exit 2。"""


class ImpactSelection(BaseModel):
    """影响面 → 自动化选择的翻译结果（纯数据，可直接序列化进 dry-run 输出）。"""

    head_commit: str
    # 进入自动选择的 runnable 用例（有 ≥1 条绑定），排序稳定
    selected_case_ids: list[str] = Field(default_factory=list)
    # 去重排序后的 -only-testing 标识符（Target/Class/method）
    only_testing_ids: list[str] = Field(default_factory=list)
    # runnable 但缺少自动化绑定：响亮上报，由人工补标记/补映射
    unmapped_runnable_case_ids: list[str] = Field(default_factory=list)
    # 三分类中不自动选择的两桶，只报告
    no_automation_case_ids: list[str] = Field(default_factory=list)
    conflict_case_ids: list[str] = Field(default_factory=list)
    idempotency_key: str
    request_id: str
    # 挂每日重跑配额的账本 key（按 head 分桶）；置空则不占重跑预算
    daily_budget_key: str


def head_short(head_commit: str) -> str:
    """统一短 sha 口径，非法输入直接拒（空 sha 不允许进选择集身份）。"""
    if not isinstance(head_commit, str) or not head_commit.strip():
        raise ImpactSelectionError("head_commit 不能为空")
    return head_commit[:HEAD_SHORT_LEN]


def selection_digest(only_testing_ids: Sequence[str]) -> str:
    """排序后规范化 JSON 的 sha256 摘要：输入乱序不影响身份。"""
    blob = json.dumps(
        sorted(only_testing_ids), ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:SELECTION_DIGEST_LEN]


def impact_idempotency_key(head_commit: str, only_testing_ids: Sequence[str]) -> str:
    """docs/01 契约：``impact:<head短sha>:<排序后ID集 hash>``。"""
    return f"impact:{head_short(head_commit)}:{selection_digest(only_testing_ids)}"


def ensure_map_fresh(map_payload: Mapping[str, Any], head_commit: str) -> None:
    """map 版本与鲜度守卫：版本不符或 head 不一致一律 fail-closed。"""
    version = map_payload.get("map_schema_version")
    if version != AUTOMATION_MAP_SCHEMA_VERSION:
        raise ImpactSelectionError(
            f"automation-map 版本不支持: {version!r}，期望 {AUTOMATION_MAP_SCHEMA_VERSION!r}；"
            "请重跑 testpilot-scan-automation 生成新 map")
    map_head = map_payload.get("head_commit", "")
    if map_head != head_commit:
        raise ImpactSelectionError(
            "automation-map 已过期：map.head_commit="
            f"{str(map_head)[:HEAD_SHORT_LEN] or '（空）'} 与 --head "
            f"{head_short(head_commit)} 不一致；请先在演示分支重跑 "
            "testpilot-scan-automation，禁止拿旧映射跑新代码")


def build_impact_selection(
    *,
    analysis: ImpactAnalysis,
    bindings: Sequence[Mapping[str, Any]],
    head_commit: str,
) -> ImpactSelection:
    """把影响面三分类 + map 绑定翻译成 only_testing 选择集（纯函数）。

    bindings 取 automation-map-v1 载荷的 ``bindings`` 数组，每项至少含
    ``case_external_id`` 与 ``only_testing_id``。
    """
    short = head_short(head_commit)

    runnable_ids: list[str] = []
    no_automation_ids: list[str] = []
    conflict_ids: list[str] = []
    for case in analysis.cases:
        if case.classification == "runnable":
            runnable_ids.append(case.external_id)
        elif case.classification == "no_automation":
            no_automation_ids.append(case.external_id)
        else:  # conflict_update
            conflict_ids.append(case.external_id)

    # case → OID 列表（一个 TC 可绑多个方法）
    bindings_by_case: dict[str, list[str]] = {}
    for row in bindings:
        case_id = row.get("case_external_id")
        oid = row.get("only_testing_id")
        if not case_id or not oid:
            raise ImpactSelectionError(
                f"automation-map 绑定行缺字段: {dict(row)}")
        bindings_by_case.setdefault(case_id, []).append(oid)

    selected_case_ids: list[str] = []
    unmapped_runnable: list[str] = []
    oids: list[str] = []
    for case_id in runnable_ids:
        case_oids = bindings_by_case.get(case_id)
        if not case_oids:
            unmapped_runnable.append(case_id)
            continue
        selected_case_ids.append(case_id)
        oids.extend(case_oids)

    # 多 TC 标记同一方法时去重；再过一遍 xcodebuild 标识符契约（坏 map  fail-closed）
    only_testing_ids = sorted(set(oids))
    for oid in only_testing_ids:
        try:
            validate_only_testing(oid)
        except ValueError as exc:
            raise ImpactSelectionError(
                f"automation-map 含非法 only-testing 标识符 {oid!r}: {exc}") from exc

    digest = selection_digest(only_testing_ids)
    return ImpactSelection(
        head_commit=head_commit,
        selected_case_ids=sorted(selected_case_ids),
        only_testing_ids=only_testing_ids,
        unmapped_runnable_case_ids=sorted(unmapped_runnable),
        no_automation_case_ids=sorted(no_automation_ids),
        conflict_case_ids=sorted(conflict_ids),
        idempotency_key=f"impact:{short}:{digest}",
        request_id=f"impact-req-{short}-{digest[:8]}",
        daily_budget_key=f"impact:{short}",
    )


def validate_selection_nonempty(selection: ImpactSelection) -> None:
    """空选择守卫：runnable 全未映射时硬失败，绝不退化为全量（红证守卫点）。"""
    if selection.only_testing_ids:
        return
    unmapped = ", ".join(selection.unmapped_runnable_case_ids) or "（本次无 runnable 用例）"
    raise ImpactSelectionError(
        "影响面选择集为空：runnable 用例全部缺少自动化映射"
        f"（未映射: {unmapped}）；请先在演示分支补 // testpilot-case 标记并重跑 "
        "testpilot-scan-automation；本系统禁止静默退化为全量执行")
