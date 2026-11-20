"""docs/01 §4.3 C2 知识分流红证（2026-11-16，hostless 纯函数）。

红证矩阵中 C2 负责的行：
- 词表纯函数：段首标记/机制夹注/机制句式命中，词表外文本不漂移，显式人审标签守卫；
- 解析器：TC-A1/TC-A1b 同形夹具知识 bullet 入 context_notes，真断言数量与类型不减少，
  嵌套子项继承父桶，跨多行机制夹注 build 期复核，只有背景条目的用例仍 oracle_missing；
- 兼容性：表格体恒空、老 raw_json 反序列化默认 []；
- 下游：context_notes 不进挖掘草稿、不参与 exploratory、不进 case-export-v1 白名单、
  审批队列载荷携出供 Web 只读展示。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from testpilot_core.approval import (
    SuggestionView,
    build_review_queue,
)
from testpilot_core.case_export import build_case_export
from testpilot_core.enums import OracleType
from testpilot_core.models import Oracle, TestCase
from testpilot_core.oracle_mining import MinedOracle, mine_deterministic
from testpilot_core.testpoint import ImpactEvidence
from testpilot_core.testcase import (
    classify_bullet_role,
    parse_case_markdown,
)

_NOW = datetime(2026, 11, 16, 9, 0, tzinfo=timezone.utc)
_DOC_URN = "urn:testpilot:fs-cases:document:cases/d.md"
_CASE_URN = "urn:testpilot:fs-cases:test_case:cases/d.md#TC-C2"


# ── 夹具 ────────────────────────────────────────────────────────────────────

def _narrative(lines: list[str], *, case_id: str = "TC-C2", steps: int = 3) -> str:
    step_lines = "\n".join(f"{i}. 操作步骤 {i}" for i in range(1, steps + 1))
    # 顶层行补 "- "；自带缩进的嵌套子项（"  1."/"  -"）原样保留走续行/嵌套分支
    bullets = "\n".join(
        line if line.startswith((" ", "\t")) else f"- {line}" for line in lines
    )
    return (
        "## 模块 M\n\n"
        f"### {case_id} 知识分流夹具（FR-1）P1\n\n"
        f"{step_lines}\n\n"
        "**预期**：\n"
        f"{bullets}\n"
    )


def _parse_one(text: str):
    result = parse_case_markdown(text, doc_external_id="cases/d.md")
    assert len(result.cases) == 1
    return result.cases[0], result.dead_letters


def _case(**over) -> TestCase:
    kwargs = dict(
        urn=_CASE_URN, external_id="TC-C2", title="知识分流夹具",
        module="M", priority="P1", steps=["步骤1", "步骤2", "步骤3"],
        oracles=[Oracle(type=OracleType.UI_ASSERTION, expectation="页面展示横幅")],
    )
    kwargs.update(over)
    return TestCase(**kwargs)


# ── 词表纯函数 ───────────────────────────────────────────────────────────────

def test_classify_prefix_markers_go_context():
    for text in [
        "**真机实锤的两条平台约束**：后台必须保持 .mixWithOthers",
        "平台约束：非混音会话必抛 !int",
        "实测记录（2026-09-16，iPhone 16 Pro）：修复前主钟接管失败",
        "历史：2026-09-16 无 gate 版实测一轮零空隙",
        "历史记录：2026 年旧流程 kill-backup 为 90s 间隔",
    ]:
        assert classify_bullet_role(text) == "context", text


def test_classify_meta_parenthesis_forms_go_context():
    # 活样本 TC-A1b：跨代码符号嵌套半角括号、句尾括号+冒号
    assert classify_bullet_role(
        "（2026-11-14 按代码现文案核对；机制见 InAppAlarmRinger.handleWillResignActive / "
        "setHaptics(active:false) / unmuteFromSystemStop(reason:)）："
    ) == "context"
    assert classify_bullet_role("（机制见 SystemStopGate 文档）") == "context"
    assert classify_bullet_role("（代码现文案核对于 2026-11-14）：") == "context"


def test_classify_mechanism_sentences_go_context():
    assert classify_bullet_role(
        "**iOS 26.4 不投递 LA `.ended`**（三轮零 ended、perform 零执行）"
    ) == "context"
    assert classify_bullet_role(
        ".alarmDidDismiss 分支仅为其他版本/出口保留"
    ) == "context"


def test_classify_wordlist_outside_stays_assertion_no_drift():
    # 保守口径红线：词表外文本一律 assertion，词表收紧/放宽不得漂移
    for text in [
        "日志（2026-11-14 按代码现文案核对；gate 机制 2026-09-19 引入）：T0 见 KEEPALIVE armed",
        "T0 见 KEEPALIVE armed（锁屏时已有待响闹钟）",
        "锁屏上不出现任何补救 UN 通知；",
        "App 循环铃声与降级震动在锁屏下立即恢复并无缝继续；",
        "历史告警记录页面应展示最近 10 条事件",  # 「历史」后无冒号/年份，是真断言
        "解锁进 App 直接是接管页，点完成后全停。",
    ]:
        assert classify_bullet_role(text) == "assertion", text


def test_classify_explicit_review_tag_always_assertion_even_with_context_words():
    # 人审回写显式标签是权威断言，任何知识词表不得覆盖
    tagged = "【UI 断言｜步骤2】真机实锤平台约束下横幅仍可见"
    assert classify_bullet_role(tagged) == "assertion"
    tagged2 = "【日志断言】iOS 26.4 不投递 LA .ended 时门控保持静音"
    assert classify_bullet_role(tagged2) == "assertion"


# ── 解析器：TC-A1 同形夹具（docs/01 §4.3 红证：2 条 context，7 条真断言不减少） ──

def test_tc_a1_shaped_fixture_splits_two_context_from_seven_assertions():
    case, dead = _parse_one(_narrative([
        "T0 到侧边键之间只有系统一路铃声（主观不得听到叠音）",                 # 1 manual
        "步骤 3 后系统全屏闹钟消失，循环铃声与震动无缝继续",                   # 2 ui + anchor
        "日志：T0 见 KEEPALIVE armed → RINGER gate unmuted",                 # 3 log
        "**真机实锤的两条平台约束**：",                                       # ctx 1
        "  1. 后台激活非混音会话必抛 !int，必须保持 .mixWithOthers",
        "  2. play() 可能返回 false，0.5s×14 次重试",
        "**iOS 26.4 不投递 LA `.ended`**（三轮零 ended）；分支仅为其他版本/出口保留",  # ctx 2
        "锁屏上不出现任何补救 UN 通知；",                                     # 4 ui
        "解锁进 App 直接是接管页，点完成后全停。",                            # 5 ui/manual
        "快照双拍确认本钟离开 .alerting 状态",                                # 6 state
        "铃声文件与系统闹钟同文件名，音量渐强到 1.0",                         # 7 state
    ]))
    assert dead == []
    assert len(case.context_notes) == 2
    assert "真机实锤" in case.context_notes[0]
    # 嵌套两条编号子项随父桶汇聚，不独立成断言也不丢失
    assert "mixWithOthers" in case.context_notes[0]
    assert "0.5s×14 次重试" in case.context_notes[0]
    assert "iOS 26.4 不投递" in case.context_notes[1]
    assert len(case.oracles) == 7
    # 真断言类型不减少、锚点不被知识分流干扰
    anchored = [o for o in case.oracles if o.step_id == 3]
    assert len(anchored) == 1
    assert {o.type for o in case.oracles} >= {
        OracleType.MANUAL, OracleType.UI_ASSERTION,
        OracleType.LOG_ASSERTION, OracleType.STATE_ASSERTION,
    }


def test_tc_a1b_multiline_meta_parenthesis_routed_at_build():
    # 活样本：机制夹注跨三个物理行，入口期不完整，build 期对拼接全文复核命中
    text = (
        "## 模块 M\n\n"
        "### TC-A1b 横幅闹钟接力（FR-2e）P0\n\n"
        "1. 排一个 1 分钟后的快速闹钟\n"
        "2. 横幅响铃时按侧边键锁定屏幕\n\n"
        "**预期**：（2026-11-14 按代码现文案核对；机制见\n"
        "`InAppAlarmRinger.handleWillResignActive` / `setHaptics(active:false)` /\n"
        "`unmuteFromSystemStop(reason:)`）：\n"
        "- 按下侧边键熄屏瞬间系统横幅声音被切断，但 .alerting 不翻转\n"
        "- App 循环铃声与降级震动在锁屏下立即恢复\n"
    )
    case, dead = _parse_one(text)
    assert dead == []
    assert len(case.context_notes) == 1
    assert "机制见" in case.context_notes[0]
    assert "unmuteFromSystemStop" in case.context_notes[0]
    assert len(case.oracles) == 2
    assert all("机制见" not in o.expectation for o in case.oracles)


def test_nested_dash_bullets_inherit_context_bucket():
    case, _ = _parse_one(_narrative([
        "**真机实锤的两条平台约束**：",
        "  - 子项甲：非混音会话必抛异常",
        "  - 子项乙：竞态下 play 返回 false",
        "锁屏上不出现补救通知",
    ]))
    assert len(case.context_notes) == 1
    assert "子项甲" in case.context_notes[0] and "子项乙" in case.context_notes[0]
    assert len(case.oracles) == 1


def test_context_only_bullets_keep_oracle_missing():
    # 知识材料不充当预期：只有背景条目的用例仍是缺预期，必须进模型补点队列
    case, _ = _parse_one(_narrative([
        "平台约束：后台必须保持混音会话",
        "历史：2026 年旧流程间隔为 90s",
    ]))
    assert case.oracles == []
    assert len(case.context_notes) == 2
    assert case.oracle_missing is True


def test_embedded_history_in_continuation_never_splits_parent_oracle():
    # 「历史」出现在续行散文里而非段首，不触发分流，整条仍是一条日志断言
    case, _ = _parse_one(_narrative([
        "日志：RINGER gate unmuted 后铃声继续。历史：2026-09-16 无 gate 版实测一轮零空隙",
    ]))
    assert case.context_notes == []
    assert len(case.oracles) == 1
    assert case.oracles[0].type == OracleType.LOG_ASSERTION


def test_table_form_context_notes_always_empty():
    text = (
        "## 模块 T\n\n"
        "| ID | 环境 | 步骤 | 预期 | 级 |\n"
        "|---|---|---|---|---|\n"
        "| TC-T1 | 🤖 | 建钟并等待 | 横幅展示 | P0 |\n"
    )
    result = parse_case_markdown(text, doc_external_id="cases/d.md")
    table_case = next(c for c in result.cases if c.external_id == "TC-T1")
    assert table_case.context_notes == []


# ── 零 DDL 兼容 ──────────────────────────────────────────────────────────────

def test_legacy_raw_json_deserializes_context_notes_default_empty():
    legacy = {
        "urn": _CASE_URN, "external_id": "TC-C2", "title": "老数据",
        "steps": ["s1"], "oracles": [],
    }
    case = TestCase.model_validate(legacy)
    assert case.context_notes == []
    # 新字段随 raw_json 无损往返
    roundtrip = TestCase.model_validate(json.loads(case.model_dump_json()))
    assert roundtrip.context_notes == []


# ── 下游：挖掘/exploratory/导出/审批载荷 ─────────────────────────────────────

def test_context_notes_never_become_suggestions_or_affect_exploratory():
    case = _case(
        oracles=[],
        context_notes=["真机实锤平台约束：非混音会话必抛 !int"],
        oracle_missing=True,
    )
    result = mine_deterministic(case, None, [], [])
    assert result.mined == []
    # 背景条目不伪装成可执行断言：无断言可挖 → 探索性
    assert result.exploratory is True

    case2 = _case(context_notes=["平台约束说明"])
    result2 = mine_deterministic(case2, None, [], [])
    assert len(result2.mined) == 1  # 真断言照常进草稿
    assert all("平台约束" not in m.expectation for m in result2.mined)


def test_v1_export_whitelist_never_leaks_context_notes():
    case = _case(context_notes=["iOS 26.4 不投递 LA .ended", "平台约束条目"])
    payload = build_case_export(
        profile="ios-local-poc-xcode", project="AlarmKitSpike",
        source_branch="main", requirements=[], test_points=[],
        cases=[case], now=_NOW,
    )
    exported = payload["cases"][0]
    assert "context_notes" not in exported
    assert all("context_notes" not in o for o in exported["oracles"])
    # 既有白名单字段不缩水
    assert set(exported.keys()) >= {
        "external_id", "steps", "preconditions", "oracles", "mined_oracle_suggestions",
    }


def test_review_queue_carries_context_notes_for_web_display():
    case = _case(context_notes=["真机实锤平台约束：混音会话不可中断"])
    suggestion = SuggestionView(
        urn="urn:testpilot:fs-cases:oracle_suggestion:TC-C2#abc1234567",
        case_urn=_CASE_URN, external_id="TC-C2", status="pending",
        oracle=MinedOracle(
            type=OracleType.UI_ASSERTION, expectation="页面展示横幅",
            source_kind="existing_assertion",
        ),
    )
    queue = build_review_queue([suggestion], [case])
    assert len(queue) == 1
    assert queue[0].context_notes == ["真机实锤平台约束：混音会话不可中断"]
    # 背景条目不混入待审草稿
    assert len(queue[0].suggestions) == 1
    assert queue[0].suggestions[0].oracle.expectation == "页面展示横幅"


def test_impact_evidence_incident_still_works_alongside_context_notes():
    # 回归守卫：事故义务句挖掘与知识分流互不干扰
    incident = ImpactEvidence(
        urn="urn:testpilot:fs-knowledge:chunk:incidents/postmortem.md#L10",
        kind="incident", title="复盘",
        text="后台播放必须保持混音会话；不得在锁屏时停止循环音。",
    )
    case = _case(context_notes=["平台约束：仅作背景说明"])
    result = mine_deterministic(case, None, [incident], [])
    expectations = " ".join(m.expectation for m in result.mined)
    assert "必须保持混音会话" in expectations
    assert "仅作背景说明" not in expectations
