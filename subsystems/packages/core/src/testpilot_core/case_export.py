"""case-export-v1 结构化用例集导出纯函数（docs/01 §3.1，切片 E1）。

零 IO、零三方（仅 pydantic + 标准库）：吃 core 领域模型列表，吐
``case-export-v1`` 载荷 dict 与内容寻址 hash，供 worker 落盘、供外部 Agent
携出系统边界生成 Xcode 测试。

边界纪律（docs/01 §2）：

- 只导出 ``lifecycle=active`` 用例；cancelled 用例不进 ``cases``，只进
  ``excluded.cancelled_case_ids`` 留痕，导出物不假装库比实际更全；
- ``exploratory=true`` 照常导出但打标（外部 Agent 不应为其生成自动化），
  显式 ``include_exploratory=False`` 时移入 ``excluded.exploratory_case_ids``；
- 活跃用例为零（空库/全 cancelled/全探索性被排除）→ ``CaseExportError``
  响亮失败，由 CLI 映射 exit 2，绝不产空壳文件；
- 内容 hash 归一化排除 ``exported_at``：同库两次导出时间戳不同但内容 hash
  必须一致，供跨系统对账。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from pydantic import BaseModel

from .models import TestCase, TestPoint
from .oracle_mining import MinedOracle
from .requirement import RequirementParseResult

CASE_EXPORT_SCHEMA_VERSION = "case-export-v1"

# 外部 Agent 生成约定（docs/01 §9.3）：target 名是工程约定而非全域常量，
# 允许 worker 按 profile 覆盖，默认值对齐 AlarmKitSpike 演示工程。
DEFAULT_TEST_TARGET = "AlarmKitSpikeTests"
DEFAULT_UI_TEST_TARGET = "AlarmKitSpikeUITests"
DEFAULT_MARKER_COMMENT = "// testpilot-case: <TC-ID>"
DEFAULT_NAMING = "func test_<TC-ID 去横线转驼峰>_<英文短描述>()"
# 该字符串会序列化进每份导出 JSON 的 generation_convention.detail_doc，改动即契约面变更。
CONVENTION_DETAIL_DOC = "docs/01 §9.3"


class CaseExportError(ValueError):
    """导出前置条件不满足（零活跃用例/ID 重复等），CLI 映射为 exit 2。"""


class VersionQuadruple(BaseModel):
    """版本四元组（口径对齐 eval_runner：不可得时留空串，不编造版本号）。"""

    data_version: str = ""
    model_version: str = ""
    prompt_version: str = ""
    params_version: str = ""


class GenerationConvention(BaseModel):
    """写给外部 Agent 的生成约定，随导出物一起携出系统边界。"""

    marker_comment: str = DEFAULT_MARKER_COMMENT
    naming: str = DEFAULT_NAMING
    target_target: str = DEFAULT_TEST_TARGET
    ui_test_target: str = DEFAULT_UI_TEST_TARGET
    detail_doc: str = CONVENTION_DETAIL_DOC


def default_generation_convention(
    *, target_target: str = DEFAULT_TEST_TARGET,
    ui_test_target: str = DEFAULT_UI_TEST_TARGET,
) -> GenerationConvention:
    return GenerationConvention(
        target_target=target_target, ui_test_target=ui_test_target)


def tc_id_to_swift_token(tc_id: str) -> str:
    """TC-ID → Swift 方法名 token：``TC-C1`` → ``TCC1``（去横线、段首大写）。

    与 docs/01 §3.2 命名约定同源，E3 扫描器反向匹配时复用同一口径，
    避免导出侧与映射侧各写一份转换规则。
    """
    segments = [seg for seg in tc_id.split("-") if seg]
    if not segments:
        raise CaseExportError(f"非法 TC-ID（为空）: {tc_id!r}")
    token = segments[0] + "".join(seg[:1].upper() + seg[1:] for seg in segments[1:])
    # 注意 str.isalnum() 对中文等 Unicode 字母/数字返回 True，必须限定 ASCII。
    if not all(ch.isascii() and (ch.isalnum() or ch == "_") for ch in token):
        raise CaseExportError(f"TC-ID 无法转为 Swift 标识符 token: {tc_id!r}")
    return token


def utc_export_stamp(now: datetime) -> str:
    """统一的导出时间戳格式（UTC、秒级、Z 后缀）；naive 时间拒绝猜测时区。"""
    from datetime import timezone

    if now.tzinfo is None:
        raise CaseExportError("导出时间必须带时区信息")
    return now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def export_filename(now: datetime, content_hash: str) -> str:
    """落盘文件名：``case-set-<UTC时间戳>-<hash8>.json``（docs/01 §3.1）。"""
    from datetime import timezone

    stamp = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"case-set-{stamp}-{content_hash[:8]}.json"


def canonical_content_hash(payload: Mapping[str, Any]) -> str:
    """内容寻址 hash：归一化排除 ``exported_at``，canonical JSON 后 sha256。

    同库同内容两次导出（时间戳不同）hash 必须一致；任何业务字段变化
    都会改变 hash，供跨系统边界对账与重放识别。``content_hash`` 是派生
    字段，不能参与自身计算，归一化时与 ``exported_at`` 一并剔除。
    """
    normalized = {
        k: v for k, v in payload.items()
        if k not in ("exported_at", "content_hash")
    }
    blob = json.dumps(
        normalized, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), default=_json_default,
    ).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _json_default(obj: Any) -> Any:
    # 载荷构建期已全部转成 dict/标量，兜底处理遗漏的 pydantic 模型。
    if isinstance(obj, BaseModel):
        return obj.model_dump(mode="json")
    raise TypeError(f"不可序列化的导出字段类型: {type(obj)!r}")


def _requirement_dict(result: RequirementParseResult) -> dict[str, Any]:
    return {
        "urn": result.doc_urn,
        "external_id": result.external_id,
        "features": [
            {
                "fr_id": f.fr_id,
                "text": f.text,
                "priority": f.priority,
                "module": f.module,
                "lifecycle": f.lifecycle,
            }
            for f in result.features
        ],
        "rules": [
            {"rule_id": r.rule_id, "text": r.text} for r in result.rules
        ],
        "acceptance_criteria": [
            {"id": f"AC-{ac.index + 1}", "text": ac.text}
            for ac in result.acceptance
        ],
    }


def _test_point_dict(point: TestPoint) -> dict[str, Any]:
    return {
        "text": point.text,
        "priority": point.priority,
        "fr_id": point.fr_id,
        "kind": point.kind,
        "provenance": point.provenance,
        "source_refs": list(point.source_refs),
    }


def _case_dict(case: TestCase, suggestions: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "external_id": case.external_id,
        "title": case.title,
        "module": case.module,
        "priority": case.priority,
        "lifecycle": case.lifecycle,
        "exploratory": case.exploratory,
        "oracle_missing": case.oracle_missing,
        "preconditions": list(case.preconditions),
        "steps": list(case.steps),
        "oracles": [
            {
                "type": oracle.type.value,
                "expectation": oracle.expectation,
                "source_refs": list(oracle.source_refs),
                "confidence": oracle.confidence,
            }
            for oracle in case.oracles
        ],
        # 挖掘草稿不与正式 oracle 混列：带来源/状态，外部 Agent 自行判据，
        # 人审状态（status）原样携出，不把 pending 草稿夸大成确认口径。
        "mined_oracle_suggestions": [dict(s) for s in suggestions],
        "requirement_urn": case.requirement_urn,
        "tags": list(case.tags),
    }


def _suggestion_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    oracle: MinedOracle = row["oracle"]
    return {
        "type": oracle.type.value,
        "expectation": oracle.expectation,
        "source_refs": list(oracle.source_refs),
        "confidence": oracle.confidence,
        "source_kind": oracle.source_kind,
        "provenance": oracle.provenance,
        "status": str(row.get("status", "")),
    }


def build_case_export(
    *,
    profile: str,
    project: str,
    source_branch: str,
    requirements: Sequence[RequirementParseResult],
    test_points: Sequence[TestPoint],
    cases: Sequence[TestCase],
    now: datetime,
    oracle_suggestions: Sequence[Mapping[str, Any]] = (),
    version: VersionQuadruple | None = None,
    convention: GenerationConvention | None = None,
    include_exploratory: bool = True,
) -> dict[str, Any]:
    """把 core 模型列表组装成 ``case-export-v1`` 载荷（零 IO 纯函数）。

    入参允许含 cancelled 用例（整库读出后由本函数分区），但导出的
    ``cases`` 段保证只有 active 用例。
    """
    version = version or VersionQuadruple()
    convention = convention or default_generation_convention()

    cancelled_ids = sorted(
        case.external_id for case in cases if case.lifecycle == "cancelled"
    )
    active_cases = [case for case in cases if case.lifecycle == "active"]
    exploratory_ids: list[str] = []
    if not include_exploratory:
        exploratory_ids = sorted(
            case.external_id for case in active_cases if case.exploratory
        )
        excluded_exploratory = set(exploratory_ids)
        active_cases = [
            case for case in active_cases
            if case.external_id not in excluded_exploratory
        ]

    if not active_cases:
        raise CaseExportError(
            "无可导出的活跃用例（库为空、全部 cancelled"
            + (" 或被 --exclude-exploratory 排除" if not include_exploratory else "")
            + "），拒绝产出空壳导出文件")

    seen_ids: set[str] = set()
    duplicate_ids = sorted({
        case.external_id for case in active_cases
        if case.external_id in seen_ids or seen_ids.add(case.external_id)  # type: ignore[func-returns-value]
    })
    if duplicate_ids:
        raise CaseExportError(f"活跃用例 external_id 重复，拒绝歧义导出: {duplicate_ids}")

    suggestions_by_case: dict[str, list[dict[str, Any]]] = {}
    for row in oracle_suggestions:
        suggestions_by_case.setdefault(
            str(row.get("case_urn", "")), []).append(_suggestion_dict(row))

    payload: dict[str, Any] = {
        "export_schema_version": CASE_EXPORT_SCHEMA_VERSION,
        "exported_at": utc_export_stamp(now),
        "profile": profile,
        "project": project,
        "source_branch": source_branch,
        "version_quadruple": version.model_dump(),
        "generation_convention": convention.model_dump(),
        "requirements": [_requirement_dict(r) for r in requirements],
        "test_points": [_test_point_dict(p) for p in test_points],
        "cases": [
            _case_dict(case, suggestions_by_case.get(case.urn, ()))
            for case in active_cases
        ],
        "excluded": {
            "cancelled_case_ids": cancelled_ids,
            "exploratory_case_ids": exploratory_ids,
            "reason": "lifecycle=cancelled 不导出；exploratory 仅在显式排除时列入",
        },
    }
    payload["content_hash"] = canonical_content_hash(payload)
    return payload
