"""FR-TD-003 oracle 四源挖掘纯逻辑测试（S2.4，core 零三方）。

确定性三源（需求 AC / 现有断言 / 事故义务句）、代码符号只锚点不造断言、
模型白名单逐行拒入、AC3 探索性判定全部在这里钉死。投毒载荷证明 fail-closed，
干净载荷另证全绿路径——门禁对象不允许只靠 happy path 自证。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from testpilot_core.enums import OracleType
from testpilot_core.models import Oracle, TestCase
from testpilot_core.oracle_mining import (
    OracleMiningValidationError,
    apply_model_oracles,
    build_oracle_messages,
    build_oracle_whitelist,
    mine_deterministic,
    oracle_json_schema,
    suggestion_urn,
)
from testpilot_core.requirement import (
    RequirementAcceptance,
    RequirementFeature,
    RequirementParseResult,
    RequirementRule,
    parse_requirement_markdown,
)
from testpilot_core.testpoint import ImpactEvidence, acceptance_urn
from testpilot_core.urn import make_urn

REQ_EXT = "requirements/synthetic-requirements.md"
DOC_URN = make_urn("fs-reqs", "requirement_doc", REQ_EXT)
CASE_URN = "urn:testpilot:fs-cases:test_case:cases/x.md#TC-N2"
INCIDENT_URN = "urn:testpilot:fs-knowledge:chunk:incidents/postmortem.md#L10"
SYMBOL_URN = "urn:testpilot:fs-code:symbol:AlarmScheduler/schedule"


def _feature(fr_id: str, text: str, *, lifecycle: str = "active") -> RequirementFeature:
    return RequirementFeature(
        urn=make_urn("fs-reqs", "requirement", f"{REQ_EXT}#{fr_id}"),
        fr_id=fr_id, text=text, priority="P1", lifecycle=lifecycle,  # type: ignore[arg-type]
    )


def _ac(index: int, text: str, linked: list[str]) -> RequirementAcceptance:
    return RequirementAcceptance(index=index, text=text, linked_fr_ids=linked)


def _skeleton() -> RequirementParseResult:
    return RequirementParseResult(
        doc_urn=DOC_URN,
        external_id=REQ_EXT,
        features=[
            _feature("FR-1", "闹钟到点必须响铃，页面展示横幅并播放铃声"),
            _feature("FR-2", "侧边键接管后停止响铃并落库接管记录"),
            _feature("FR-4", "已取消的功能", lifecycle="cancelled"),
        ],
        rules=[RequirementRule(
            urn=make_urn("fs-reqs", "requirement_rule", f"{REQ_EXT}#RN-1"),
            rule_id="RN-1", text="响铃日志必须包含 RINGER 标记",
        )],
        acceptance=[
            _ac(0, "到点后页面弹出横幅且铃声响起", ["FR-1"]),
            _ac(1, "接管后账本写入一条接管记录，状态为 stopped", ["FR-2"]),
            _ac(2, "未链接任何 FR 的 AC", []),
        ],
    )


def _case(cid: str = "TC-N2", *, tags=None, oracles=None,
          oracle_missing: bool = False, title: str = "未锁屏时按侧边键") -> TestCase:
    return TestCase(
        urn=CASE_URN, external_id=cid, title=title,
        steps=["锁屏等待闹钟到点", "按一次侧边键"],
        tags=tags or [], oracles=oracles or [],
        oracle_missing=oracle_missing,
    )


def _incident(text: str) -> ImpactEvidence:
    return ImpactEvidence(urn=INCIDENT_URN, kind="incident",
                          title="响铃丢失复盘", text=text)


def _symbol() -> ImpactEvidence:
    return ImpactEvidence(urn=SYMBOL_URN, kind="code_symbol",
                          title="AlarmScheduler.schedule", text="func schedule(...)")


# ── 源①：需求验收标准 ───────────────────────────────────────────────────────

def test_ac_source_mines_one_oracle_per_linked_ac_with_confidence_07():
    case = _case(tags=["FR-1", "env:ios"])
    result = mine_deterministic(case, _skeleton(), [])
    ac_oracles = [o for o in result.mined if o.source_kind == "requirement_ac"]
    assert len(ac_oracles) == 1  # 只有 AC-0 链接 FR-1；AC-2 无链接不产
    oracle = ac_oracles[0]
    assert oracle.expectation == "到点后页面弹出横幅且铃声响起"
    assert oracle.type == OracleType.UI_ASSERTION  # classify_oracle 复用 S2.1 口径
    assert oracle.confidence == 0.7
    assert oracle.provenance == "deterministic"
    ac0_urn = acceptance_urn(REQ_EXT, _skeleton().acceptance[0])
    assert ac0_urn in oracle.source_refs
    assert _skeleton().features[0].urn in oracle.source_refs  # FR 锚点随行
    assert not result.exploratory  # 挖到可执行 UI oracle


def test_ac_join_uses_fr_tag_intersection_and_skips_unlinked():
    case = _case(tags=["FR-2"])
    result = mine_deterministic(case, _skeleton(), [])
    kinds = {o.source_kind: o for o in result.mined}
    assert "requirement_ac" in kinds
    assert "接管" in kinds["requirement_ac"].expectation
    # 无 FR tag 的表格体用例：AC 源整体跳过但不报错
    bare = mine_deterministic(_case(tags=[]), _skeleton(), [])
    assert all(o.source_kind != "requirement_ac" for o in bare.mined)


def test_missing_skeleton_is_empty_source_not_error():
    case = _case(tags=["FR-1"])
    result = mine_deterministic(case, None, [])
    assert result.mined == [] and result.exploratory is True
    assert "no_source_available" in result.exploratory_reasons


# ── 源②：现有断言（自身 + 相似用例） ────────────────────────────────────────

def test_self_oracles_preserved_with_original_confidence_and_refs():
    doc_ref = "urn:testpilot:fs-cases:document:cases/x.md"
    case = _case(oracles=[Oracle(
        type=OracleType.LOG_ASSERTION, expectation="日志出现 RINGER takeover",
        source_refs=[doc_ref], confidence=0.5,
    )], tags=["FR-2"])
    result = mine_deterministic(case, _skeleton(), [])
    existing = [o for o in result.mined if o.source_kind == "existing_assertion"]
    assert len(existing) == 1
    assert existing[0].expectation == "日志出现 RINGER takeover"
    assert existing[0].type == OracleType.LOG_ASSERTION
    assert existing[0].source_refs == [doc_ref]  # 原始出处不丢（AC2 可追溯）


def test_related_case_oracles_transferred_at_05_and_self_skipped():
    case = _case(oracle_missing=True)
    related = TestCase(
        urn="urn:testpilot:fs-cases:test_case:cases/x.md#TC-A4",
        external_id="TC-A4", title="相似用例", steps=["s1"],
        oracles=[Oracle(type=OracleType.STATE_ASSERTION,
                        expectation="账本新增一条 stopped 记录", confidence=0.5)],
    )
    result = mine_deterministic(case, None, [], related_cases=[related, case])
    transferred = [o for o in result.mined if o.source_kind == "existing_assertion"]
    assert len(transferred) == 1
    assert transferred[0].confidence == 0.5
    assert transferred[0].source_refs == [related.urn]
    assert not result.exploratory  # 状态断言可执行，摘掉探索性标记


def test_related_transfer_gated_on_oracle_missing():
    """R2 红证：相似用例断言平移只服务 oracle_missing 用例。

    V1 语料串案的核心机制——带自身断言的用例被整组灌入相似断言（276 行中
    257 行错挂）。旧实现无门禁，此用例在回退修复后必红。
    """
    related = TestCase(
        urn="urn:testpilot:fs-cases:test_case:cases/x.md#TC-A7",
        external_id="TC-A7", title="相似用例", steps=["s1"],
        oracles=[Oracle(type=OracleType.STATE_ASSERTION,
                        expectation="VM init 后 HEADLESS 恢复", confidence=0.5)],
    )
    # 非 missing（带自身断言）：拒绝平移，只留自己的一条
    equipped = _case(cid="TC-A1", tags=["FR-7"], oracles=[Oracle(
        type=OracleType.LOG_ASSERTION, expectation="T0 到侧边键之间不重复弹横幅",
        confidence=0.5)])
    result = mine_deterministic(equipped, None, [], related_cases=[related])
    expectations = [o.expectation for o in result.mined]
    assert "VM init 后 HEADLESS 恢复" not in expectations
    assert expectations == ["T0 到侧边键之间不重复弹横幅"]

    # 同夹具下 oracle_missing 用例：平移保留（真正的目标受众）
    missing = _case(cid="TC-A1b", tags=["FR-2e"], oracle_missing=True)
    result_missing = mine_deterministic(missing, None, [], related_cases=[related])
    transferred = [o for o in result_missing.mined
                   if o.source_kind == "existing_assertion"]
    assert [o.expectation for o in transferred] == ["VM init 后 HEADLESS 恢复"]


def test_transferred_oracle_refs_append_source_case_urn_with_doc_ref():
    """R3 红证：生产路径里相关用例 oracle 带文档级 source_refs（S2.1 解析器
    盖章），平移后必须同时保留文档 URN 并追加来源用例 URN，审批页才能区分
    「平移自 TC-Ax」与用例自有断言。"""
    doc_ref = "urn:testpilot:fs-cases:document:cases/x.md"
    related = TestCase(
        urn="urn:testpilot:fs-cases:test_case:cases/x.md#TC-A5",
        external_id="TC-A5", title="相似用例", steps=["s1"],
        oracles=[Oracle(type=OracleType.LOG_ASSERTION,
                        expectation="各补发按时响，MISSION stale state 被回收",
                        source_refs=[doc_ref], confidence=0.5)],
    )
    case = _case(cid="TC-A1b", oracle_missing=True)
    result = mine_deterministic(case, None, [], related_cases=[related])
    transferred = [o for o in result.mined
                   if o.source_kind == "existing_assertion"]
    assert len(transferred) == 1
    # 文档出处不丢，来源用例 URN 追加在后，去重保序
    assert transferred[0].source_refs == [doc_ref, related.urn]


def test_duplicate_expectation_deduped_across_sources():
    text = "接管后账本写入一条接管记录，状态为 stopped"  # 与 AC-1 同文本
    case = _case(tags=["FR-2"], oracles=[
        Oracle(type=OracleType.STATE_ASSERTION, expectation=text, confidence=0.5)])
    result = mine_deterministic(case, _skeleton(), [])
    hits = [o for o in result.mined if o.expectation == text]
    assert len(hits) == 1  # 自身断言先入，AC 同文本去重，不产双份


# ── 源③：历史缺陷义务句 ─────────────────────────────────────────────────────

def test_incident_only_obligation_sentences_become_oracles():
    evidence = _incident(
        "事故当天闹钟到点没有响。复盘结论：接管后必须写入接管账本。"
        "值班同学当时在喝咖啡。不得跳过 RINGER 日志打点。"
    )
    case = _case(oracle_missing=True)
    result = mine_deterministic(case, None, [evidence])
    incident_oracles = [o for o in result.mined if o.source_kind == "incident"]
    expectations = [o.expectation for o in incident_oracles]
    # 句子逐字保留（含「复盘结论：」前缀），证据文本可逐字回查不做二次改写
    assert "复盘结论：接管后必须写入接管账本" in expectations
    assert "不得跳过 RINGER 日志打点" in expectations
    # 叙述句没有义务标记，绝不升级成断言（防编造的核心守卫）
    assert all("咖啡" not in e and "没有响" not in e for e in expectations)
    assert all(o.source_refs == [INCIDENT_URN] for o in incident_oracles)
    assert all(o.confidence == 0.5 for o in incident_oracles)


def test_incident_sentence_capped_three_per_evidence():
    text = "。".join([f"必须检查第{i}项" for i in range(6)]) + "。"
    result = mine_deterministic(_case(oracle_missing=True), None, [_incident(text)])
    incident_oracles = [o for o in result.mined if o.source_kind == "incident"]
    assert len(incident_oracles) == 3


def test_non_incident_evidence_never_mined_deterministically():
    # code_symbol 是第四源，但确定性侧不造 expectation，只登记锚点
    result = mine_deterministic(_case(oracle_missing=True), None, [_symbol()])
    assert result.mined == []
    assert result.evidence_urns == [SYMBOL_URN]
    assert result.exploratory is True


# ── AC3 探索性判定 ──────────────────────────────────────────────────────────

def test_manual_only_oracles_remain_exploratory():
    case = _case(oracles=[Oracle(
        type=OracleType.MANUAL, expectation="主观感受铃声是否刺耳", confidence=0.5)])
    result = mine_deterministic(case, None, [])
    assert result.exploratory is True
    assert "manual_only" in result.exploratory_reasons
    assert result.executable_oracles == []


def test_oracle_missing_with_no_sources_carries_reasons():
    result = mine_deterministic(_case(oracle_missing=True), None, [])
    assert result.exploratory is True
    assert "oracle_missing_no_executable_mined" in result.exploratory_reasons
    assert "no_source_available" in result.exploratory_reasons


# ── 白名单与 prompt 自包含 ──────────────────────────────────────────────────

def test_whitelist_is_closed_union_of_requirement_evidence_related_urns():
    skeleton = _skeleton()
    related = TestCase(urn="urn:testpilot:fs-cases:test_case:c#TC-A4",
                       external_id="TC-A4", title="t", steps=["s"])
    whitelist = build_oracle_whitelist(skeleton, [_incident("必须留痕"), _symbol()], [related])
    assert skeleton.features[0].urn in whitelist
    assert skeleton.rules[0].urn in whitelist
    assert acceptance_urn(REQ_EXT, skeleton.acceptance[0]) in whitelist
    assert INCIDENT_URN in whitelist and SYMBOL_URN in whitelist
    assert related.urn in whitelist
    assert "FR-1" not in whitelist  # 裸 ID 不在白名单（S2.3 真模型踩过的坑）


def test_prompt_and_schema_are_self_contained():
    skeleton = _skeleton()
    case = _case(tags=["FR-1"], oracle_missing=True)
    spine = mine_deterministic(case, skeleton, [_symbol()])
    messages = build_oracle_messages(case, skeleton, [_symbol()], spine.mined)
    content = messages[1]["content"]
    assert skeleton.features[0].urn in content          # 完整 URN 可逐字抄写
    assert acceptance_urn(REQ_EXT, skeleton.acceptance[0]) in content
    assert SYMBOL_URN in content
    assert case.external_id in content
    system = messages[0]["content"]
    for token in ("ui_assertion", "log_assertion", "state_assertion", "manual"):
        assert token in system
    assert "confidence" in system  # 铁律 4：禁止模型自标置信度
    schema = oracle_json_schema()
    item = schema["properties"]["oracles"]["items"]
    assert set(item["required"]) == {"type", "expectation", "source_refs"}
    assert set(item["properties"]["type"]["enum"]) == {
        "ui_assertion", "log_assertion", "state_assertion", "manual"}
    assert "confidence" not in item["properties"]


# ── 模型补点：白名单 fail-closed ────────────────────────────────────────────

_UNSET = object()


def _apply(case, payload, skeleton=_UNSET, evidence=None, related=None):
    skeleton = _skeleton() if skeleton is _UNSET else skeleton
    spine = mine_deterministic(case, skeleton, evidence or [], related or [])
    return apply_model_oracles(
        case, spine, payload, skeleton, evidence or [], related or [])


def test_valid_model_rows_accepted_at_fixed_05_confidence():
    case = _case(tags=["FR-1"], oracle_missing=True)
    skeleton = _skeleton()
    ac_urn = acceptance_urn(REQ_EXT, skeleton.acceptance[0])
    payload = {"oracles": [
        {"type": "state_assertion", "expectation": "响铃状态机进入 ringing",
         "source_refs": [skeleton.features[0].urn]},
        {"type": "manual", "expectation": "人工确认横幅文案得体",
         "source_refs": [ac_urn]},
    ]}
    result = _apply(case, payload, skeleton)
    accepted = result.model_oracles
    assert len(accepted) == 2
    assert all(o.confidence == 0.5 for o in accepted)
    assert all(o.source_kind == "model_proposed" for o in accepted)
    assert result.generation_source == "model"
    # 状态断言可执行 → AC3 摘除探索性
    assert result.exploratory is False
    assert len(result.executable_oracles) >= 1


def test_reject_unknown_source_bare_id_and_fabricated_urn():
    # 不给骨架：确定性侧无 AC 可挖，模型若再全拒，用例必须保持探索性（AC3）
    case = _case(tags=["FR-1"], oracle_missing=True)
    payload = {"oracles": [
        {"type": "state_assertion", "expectation": "裸 ID 回显",
         "source_refs": ["FR-1"]},  # S2.3 真模型缺陷的同型投毒
        {"type": "ui_assertion", "expectation": "编造 URN",
         "source_refs": ["urn:testpilot:made-up:x"]},
    ]}
    result = _apply(case, payload, skeleton=None)
    assert result.model_oracles == []
    assert {r.reason for r in result.rejected} == {"unknown_source"}
    assert result.hallucination_rate == 1.0
    # 全部拒入后仍无执行 oracle → 保持探索性并记录模型未能补救
    assert result.exploratory is True
    assert "model_enrichment_failed_to_add_executable" in result.exploratory_reasons


def test_reject_bad_type_empty_text_bad_shape_rows():
    case = _case(tags=["FR-1"], oracle_missing=True)
    skeleton = _skeleton()
    good_urn = skeleton.features[0].urn
    payload = {"oracles": [
        {"type": "snapshot_assertion", "expectation": "枚举外类型",
         "source_refs": [good_urn]},
        {"type": "state_assertion", "expectation": "   ",
         "source_refs": [good_urn]},
        {"type": "state_assertion", "source_refs": [good_urn]},  # 缺 expectation
        {"expectation": "字段整体形状错", "source_refs": [good_urn]},
    ]}
    result = _apply(case, payload, skeleton)
    reasons = [r.reason for r in result.rejected]
    assert reasons.count("bad_type") == 1
    assert reasons.count("empty_text") == 1
    assert reasons.count("bad_shape") == 2
    assert result.model_oracles == []


def test_payload_shape_errors_raise_for_worker_fallback():
    case = _case(oracle_missing=True)
    spine = mine_deterministic(case, _skeleton(), [])
    with pytest.raises(OracleMiningValidationError):
        apply_model_oracles(case, spine, {"points": []}, _skeleton(), [], [])
    with pytest.raises(OracleMiningValidationError):
        apply_model_oracles(case, spine, {"oracles": "x"}, _skeleton(), [], [])
    with pytest.raises(OracleMiningValidationError):
        apply_model_oracles(case, spine, {"oracles": [1]}, _skeleton(), [], [])


def test_model_row_duplicating_spine_is_deduped_not_accepted():
    case = _case(tags=["FR-1"])
    skeleton = _skeleton()
    spine_text = skeleton.acceptance[0].text  # 确定性 AC 已挖
    payload = {"oracles": [
        {"type": "ui_assertion", "expectation": spine_text,
         "source_refs": [skeleton.features[0].urn]},
    ]}
    result = _apply(case, payload, skeleton)
    assert result.model_oracles == []
    assert result.deduped == 1
    assert result.rejected == []
    assert result.hallucination_rate == 0.0  # 去重不算幻觉


def test_evidence_and_related_case_urns_are_valid_model_refs():
    case = _case(oracle_missing=True)
    related = TestCase(urn="urn:testpilot:fs-cases:test_case:c#TC-A4",
                       external_id="TC-A4", title="t", steps=["s"])
    evidence = [_incident("必须保留接管证据"), _symbol()]
    payload = {"oracles": [
        {"type": "log_assertion", "expectation": "日志包含接管事件",
         "source_refs": [INCIDENT_URN]},
        {"type": "state_assertion", "expectation": "调度项可被反查",
         "source_refs": [SYMBOL_URN, related.urn]},
    ]}
    result = _apply(case, payload, skeleton=None, evidence=evidence, related=[related])
    assert len(result.model_oracles) == 2
    assert result.rejected == []


def test_recorded_fixture_poison_and_clean_rows():
    """录制夹具回归（worker 同键读取）：2 干净行接受、2 投毒行进台账。"""
    fixture = Path(__file__).parent / "fixtures" / "model" / "responses.json"
    data = json.loads(fixture.read_text(encoding="utf-8"))
    key = "oracle_mine:cases/synthetic-cases.md#TC-N2"
    case = _case(tags=["FR-1"], oracle_missing=True)
    skeleton = _skeleton()  # URN 与合成需求夹具同源，白名单天然对齐
    result = _apply(case, data[key]["structured"], skeleton)
    assert len(result.model_oracles) == 2
    assert all(o.confidence == 0.5 for o in result.model_oracles)
    assert {r.reason for r in result.rejected} == {"bad_type", "unknown_source"}
    assert result.hallucination_rate == 0.5  # 2 拒 / (2 受 + 2 拒)


# ── URN 内容寻址 ────────────────────────────────────────────────────────────

def test_suggestion_urn_stable_and_distinct():
    from testpilot_core.oracle_mining import MinedOracle

    def mined(otype, text):
        return MinedOracle(type=otype, expectation=text,
                           source_kind="model_proposed", provenance="model")

    a = suggestion_urn("TC-N2", mined(OracleType.STATE_ASSERTION, "状态为 stopped"))
    b = suggestion_urn("TC-N2", mined(OracleType.STATE_ASSERTION, "状态为 stopped"))
    c = suggestion_urn("TC-N2", mined(OracleType.UI_ASSERTION, "状态为 stopped"))
    d = suggestion_urn("TC-A4", mined(OracleType.STATE_ASSERTION, "状态为 stopped"))
    assert a == b and a != c and a != d  # 同用例同类型同文本才幂等


# ── L3-6 红证：AC join 只认句首标题 FR，正文提及不跨 AC 拖入（W2 回流） ─────

def test_l3_6_ac_join_prefers_primary_header_fr_over_body_mentions():
    skeleton = RequirementParseResult(
        doc_urn=DOC_URN, external_id=REQ_EXT,
        features=[_feature("FR-1", "功能一"), _feature("FR-2", "功能二")],
        rules=[],
        acceptance=[
            RequirementAcceptance(
                index=0,
                text="（FR-1）功能一验收，正文提及 FR-2 的联动细节",
                linked_fr_ids=["FR-1", "FR-2"],
                primary_fr_ids=["FR-1"],
                secondary_fr_ids=["FR-2"],
            ),
        ],
    )
    # 用例只打正文提及的 FR-2：标题级 join 不命中，AC 源一条都不得产出
    # （旧口径按 linked 并集会反向 join，W2-AC-4 同型红证）
    body_only = mine_deterministic(_case(tags=["FR-2"]), skeleton, [])
    assert body_only.mined == []
    # 用例打标题声明的 FR-1：join 正常命中
    primary_hit = mine_deterministic(_case(tags=["FR-1"]), skeleton, [])
    assert any(o.source_kind == "requirement_ac" for o in primary_hit.mined)


def test_l3_6_headerless_ac_falls_back_to_union_links():
    # primary 为空（无标题括号的历史文档/合成夹具）：join 回退 linked 并集
    skeleton = RequirementParseResult(
        doc_urn=DOC_URN, external_id=REQ_EXT,
        features=[_feature("FR-1", "功能一"), _feature("FR-2", "功能二")],
        rules=[],
        acceptance=[_ac(0, "正文提及 FR-2 的无标题 AC", ["FR-2"])],
    )
    result = mine_deterministic(_case(tags=["FR-2"]), skeleton, [])
    assert any(o.source_kind == "requirement_ac" for o in result.mined)


# ── 真实解析夹具对接（防纯手工构造自嗨） ───────────────────────────────────

def test_real_parsed_skeleton_feeds_ac_miner():
    fixture = Path(__file__).parent / "fixtures" / "requirements" / "synthetic-requirements.md"
    parsed = parse_requirement_markdown(
        fixture.read_text(encoding="utf-8"),
        doc_external_id="requirements/synthetic-requirements.md")
    if not parsed.acceptance or not any(ac.linked_fr_ids for ac in parsed.acceptance):
        pytest.skip("纯解析骨架 AC 未确定性链接时由 S2.2 链接后再测")
    linked_fr = next(ac.linked_fr_ids[0] for ac in parsed.acceptance if ac.linked_fr_ids)
    case = _case(tags=[linked_fr])
    result = mine_deterministic(case, parsed, [])
    assert any(o.source_kind == "requirement_ac"
               and o.confidence == 0.7 for o in result.mined)
