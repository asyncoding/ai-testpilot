"""oracle 有效率专家盲评纯逻辑（docs/06 §3.2，S2.6d 五门禁之第五门）。

指标定义（docs/06 §3.2 场景②）：**oracle 有效率** = 挖出的 oracle 被专家判为
「可执行且正确」的比例，分类统计 UI/日志/状态/人工四类，POC 门禁 ≥0.7。

盲标纪律与 ``testpoint_eval`` 同构（docs/06 §3.2：专家必须在看到 AI 输出**之前**
独立判定，否则被锚定）：

- 专家清单以 ``OracleExpertList`` 落盘并先封存（``seal_oracle_manifest`` 哈希），
  评测函数要求清单非空、归属文档匹配且**逐条覆盖**全部用例，否则
  ``OracleBlindError``——不允许专家只评容易的用例；
- 专家按用例文档的人读 ID（TC-xxx）独立给出「该用例应能自动断言的 oracle 类型
  集合」，不接触挖掘结果；人工见证类用例给空集合（对应探索性出口）；
- 判定在 hostless 纯函数内完成：可执行类型仅 UI/日志/状态三类（manual 不是
  可执行 oracle，与 ``OracleMiningResult.executable_oracles`` 同口径），挖出的
  可执行 oracle 类型 ∈ 专家期望集合即「可执行且正确」，否则记 invalid；
  专家期望但没挖到的类型记 missing（覆盖缺口，单独报告不混进有效率分母）；
- 探索性用例（``exploratory=True``）无可执行 oracle，天然不进有效率分母
  （docs/06：「剩余标探索性用例是正常出口」），但专家若对其给了非空期望，
  说明挖掘侧未达专家口径，missing 照记、``coverage_complete=False``。
"""

from __future__ import annotations

import hashlib
import json

from pydantic import BaseModel, Field

from .enums import OracleType
from .models import TestCase
from .oracle_mining import OracleMiningResult

# docs/06 §3.2 POC 门禁（与 testpoint_eval 的阈值常量同风格集中存放）
ORACLE_EFFECTIVENESS_GATE = 0.7

# 可自动化断言的三类；manual 单列统计、不进有效率分母
_EXECUTABLE_TYPES = frozenset({
    OracleType.UI_ASSERTION,
    OracleType.LOG_ASSERTION,
    OracleType.STATE_ASSERTION,
})
_ALL_TYPES = (
    OracleType.UI_ASSERTION,
    OracleType.LOG_ASSERTION,
    OracleType.STATE_ASSERTION,
    OracleType.MANUAL,
)


class OracleBlindError(ValueError):
    """专家清单缺失/未覆盖全部用例/文档不匹配：盲评流程被破坏，拒绝评测。"""


class OracleExpertCase(BaseModel):
    """专家对单条用例的独立判定：应能自动断言的 oracle 类型集合（空=纯人工）。"""

    case_external_id: str  # 用例文档里的人读 ID（TC-xxx），专家侧稳定键
    expected_executable: list[OracleType] = Field(default_factory=list)
    note: str = ""


class OracleExpertList(BaseModel):
    """oracle 盲评专家清单：封存发生在挖掘之前，sealed_at 是流程证据。"""

    cases_doc_external_id: str
    sealed_at: str  # ISO 时间戳，封存动作发生在 AI 挖掘之前（流程证据）
    expert: str = "blind-expert"
    cases: list[OracleExpertCase] = Field(default_factory=list)


class OracleCaseVerdict(BaseModel):
    """单条用例的判定明细，供报告与 UI 逐条复核。"""

    case_external_id: str
    case_urn: str
    exploratory: bool
    evaluated: bool  # False=探索性用例，不进有效率分母
    mined_types: list[str] = Field(default_factory=list)
    valid_types: list[str] = Field(default_factory=list)
    invalid_types: list[str] = Field(default_factory=list)
    missing_types: list[str] = Field(default_factory=list)
    mined_executable: int = 0
    valid_count: int = 0
    invalid_count: int = 0
    manual_count: int = 0


class OracleEvalReport(BaseModel):
    cases_doc_external_id: str
    expert_seal_hash: str
    generation_source: str
    cases_total: int
    exploratory_excluded: int
    evaluated_cases: int
    mined_executable_total: int
    valid_total: int
    invalid_total: int
    missing_total: int
    manual_total: int  # 非探索性用例中的 manual 条数（单列，不进分母）
    effective_rate: float
    coverage_complete: bool  # 专家期望类型零缺失（含探索性用例期望非空的情形）
    by_type: dict[str, int]  # 全量四分类盘点（含探索性用例，口径在模块文档说明）
    gate_ok: bool
    verdicts: list[OracleCaseVerdict] = Field(default_factory=list)


def seal_oracle_manifest(manifest: OracleExpertList) -> str:
    """封存哈希：规范 JSON 后 sha256 截 16 位，进报告证明评测基于哪版专家清单。"""
    canonical = json.dumps(
        manifest.model_dump(), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _doc_id_of_case_urn(case_urn: str) -> str:
    """从用例 URN 反解文档归属。

    URN 形状（``testcase.parse_case_markdown`` 约定）：
    ``urn:testpilot:<source>:test_case:<doc_external_id>#<case_id>``；
    ``OracleMiningResult.external_id`` 是用例级 ID（TC-xxx）而非文档 ID，
    跨文档守卫只能从 URN 的 key 段取 ``#`` 左侧。
    """
    key = case_urn.rsplit(":", 1)[-1]
    return key.partition("#")[0]


def _types(values: list[OracleType]) -> list[str]:
    """枚举转稳定排序的字符串列表，报告里类型口径统一走 value。"""
    return sorted(v.value for v in values)


def evaluate_oracles(
    cases: list[TestCase],
    results: list[OracleMiningResult],
    manifest: OracleExpertList,
) -> OracleEvalReport:
    """对一组挖掘结果跑专家盲评；专家清单必须先封存并逐条覆盖全部用例。"""
    if not manifest.cases:
        raise OracleBlindError(
            "oracle 专家盲评清单为空：必须先独立判定并封存，再揭示挖掘结果"
            "（docs/06 §3.2）"
        )
    if len(cases) != len(results):
        raise OracleBlindError(
            f"用例数 {len(cases)} 与挖掘结果数 {len(results)} 不一致，拒绝评测"
        )

    urn_to_case = {c.urn: c for c in cases}
    if len(urn_to_case) != len(cases):
        raise OracleBlindError("用例 URN 重复，专家判定无法逐条对账")

    expected_by_id: dict[str, frozenset[OracleType]] = {}
    for item in manifest.cases:
        if item.case_external_id in expected_by_id:
            raise OracleBlindError(
                f"专家清单存在重复用例判定: {item.case_external_id}"
            )
        expected_by_id[item.case_external_id] = frozenset(item.expected_executable)

    # 集合相等守卫：不允许只评部分用例，也不允许判定幽灵用例（空用例集配非空
    # 清单同样在此拦死，避免产出零分母的假报告）
    case_ids = {c.external_id for c in cases}
    missing_judgment = sorted(case_ids - set(expected_by_id))
    extra_judgment = sorted(set(expected_by_id) - case_ids)
    if missing_judgment or extra_judgment:
        raise OracleBlindError(
            f"专家清单与用例集合不一致：缺少用例判定 {missing_judgment}，"
            f"多余判定 {extra_judgment}（docs/06 §3.2 盲标纪律：不允许只评部分用例）"
        )

    by_type: dict[str, int] = {t.value: 0 for t in _ALL_TYPES}
    verdicts: list[OracleCaseVerdict] = []
    exploratory_excluded = 0
    valid_total = 0
    invalid_total = 0
    missing_total = 0
    manual_total = 0
    generation_sources = set()

    for result in results:
        case = urn_to_case.get(result.case_urn)
        if case is None:
            raise OracleBlindError(
                f"挖掘结果 {result.case_urn} 找不到对应用例，拒绝评测"
            )
        doc_id = _doc_id_of_case_urn(result.case_urn)
        if manifest.cases_doc_external_id != doc_id:
            raise OracleBlindError(
                f"专家清单归属 {manifest.cases_doc_external_id} 与用例 URN "
                f"文档段 {doc_id} 不一致，禁止跨文档评测"
            )
        generation_sources.add(result.generation_source)
        expected = expected_by_id[case.external_id]

        for oracle in result.mined:
            by_type[oracle.type.value] += 1

        mined_exec = [o for o in result.mined if o.type in _EXECUTABLE_TYPES]
        mined_exec_types = {o.type for o in mined_exec}
        valid_types = {o.type for o in mined_exec if o.type in expected}
        invalid_types = mined_exec_types - expected
        missing_types = expected - mined_exec_types
        manual_count = sum(1 for o in result.mined if o.type == OracleType.MANUAL)

        # 探索性用例：无类型可进有效率分母（可执行列表必为空，AC3 守卫保证）；
        # 但专家若给了非空期望，missing 照记——这是挖掘侧未达专家口径的信号。
        evaluated = not result.exploratory
        if result.exploratory:
            exploratory_excluded += 1
        else:
            valid_total += len([o for o in mined_exec if o.type in expected])
            invalid_total += len([o for o in mined_exec if o.type not in expected])
            manual_total += manual_count
        missing_total += len(missing_types)

        verdicts.append(OracleCaseVerdict(
            case_external_id=case.external_id,
            case_urn=case.urn,
            exploratory=result.exploratory,
            evaluated=evaluated,
            mined_types=_types([o.type for o in result.mined]),
            valid_types=_types(sorted(valid_types)),
            invalid_types=_types(sorted(invalid_types)),
            missing_types=_types(sorted(missing_types)),
            mined_executable=len(mined_exec),
            valid_count=len([o for o in mined_exec if o.type in expected]),
            invalid_count=len([o for o in mined_exec if o.type not in expected]),
            manual_count=manual_count,
        ))

    denominator = valid_total + invalid_total
    effective_rate = round(valid_total / denominator, 6) if denominator else 0.0
    # 同一次总评只允许确定性/模型等单一来源混合口径可解释；多来源照实拼接展示
    source = "+".join(sorted(generation_sources)) if generation_sources else "unknown"
    return OracleEvalReport(
        cases_doc_external_id=manifest.cases_doc_external_id,
        expert_seal_hash=seal_oracle_manifest(manifest),
        generation_source=source,
        cases_total=len(results),
        exploratory_excluded=exploratory_excluded,
        evaluated_cases=len(results) - exploratory_excluded,
        mined_executable_total=denominator,
        valid_total=valid_total,
        invalid_total=invalid_total,
        missing_total=missing_total,
        manual_total=manual_total,
        effective_rate=effective_rate,
        coverage_complete=missing_total == 0,
        by_type=dict(sorted(by_type.items())),
        gate_ok=effective_rate >= ORACLE_EFFECTIVENESS_GATE,
        verdicts=verdicts,
    )
