"""用例文档 lint 探针（docs/17 §5，M1，2026-11-16）。

纯函数、零 IO、零三方：吃文档原文 + ``parse_case_markdown`` 的结构化结果，
产出分级发现项与可量化聚合指标。定位是**先报告后阻断**（docs/17 §5.4）：
M1 阶段 CLI 只出报告，阈值经冻结夹具标定后再转门禁。

规则登记表（与 docs/17 §5.2 同号）：
- error：LINT-C-001 标记缺冒号 / 002 真缺预期 / 003 步骤编号断裂与解析死信；
- warning：004 知识材料未独立成段 / 005 超长断言 / 006 无标签人工断言 /
  007 表格体待迁移 / 008 缺前置段；
- info：009 锚点覆盖提示 / 010 实测记录要素与下线符号。
LINT-C-011（需求侧 EARS 句式）属二期，本模块不实现。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .enums import OracleType
# 复用解析器私有的行内清洗与词表，禁止 lint 侧另造一套导致口径漂移
from .testcase import (
    CaseParseResult,
    TestCase,
    _strip_inline,
    classify_bullet_role,
    classify_oracle,
    split_explicit_tag,
)

# 首版阈值为保守常量；M1 报告跑出 P95 分布后再 params 版本化（docs/17 §5.4 纪律）
MAX_ORACLE_CHARS = 240
MAX_ORACLE_LOG_TOKENS = 8
# 已下线/重定向符号：出现在实测记录段是合法历史冻结，段外出现提示保鲜
_RETIRED_SYMBOLS = ("Glass1",)
_EVIDENCE_LIMIT = 80

_BOLD_MARKER_RE = re.compile(r"^\*\*(预期|前置|步骤)\*\*")
_HAS_COLON_RE = re.compile(r"[：:]")
_H2_RE = re.compile(r"^##\s+")
_BACKTICK_TOKEN_RE = re.compile(r"`[^`\n]+`")
_FOUR_DIGIT_YEAR_RE = re.compile(r"\d{4}")
_BACKGROUND_MARKER_RE = re.compile(r"^\*\*背景")
_PRECOND_MARKER_RE = re.compile(r"^\*\*前置\*\*")
_RECORD_MARKER_RE = re.compile(r"^\*\*实测记录")
# 与 testcase.py 的 _EXPECT_MARKER_RE/_BULLET_RE 同形（lint 需要原文形态：
# 解析入库时反引号与显式标签已被剥离，005/006 只能在原文行上判定）
_EXPECT_SECTION_RE = re.compile(r"^\*\*预期\*\*[^：:]*[：:]\s*(?P<inline>.*)$")
_TOP_BULLET_RE = re.compile(r"^-\s+(?P<body>.+?)\s*$")
_INDENT_BULLET_RE = re.compile(r"^\s+-\s+(?P<body>.+?)\s*$")
_BOLD_LINE_RE = re.compile(r"^\*\*[^*]+\*\*")


def _expect_bullets(lines: list[str]) -> list[str]:
    """提取块内 ``**预期**：`` 段的顶格子弹原文（含反引号/显式标签）。

    其他加粗小节标题结束预期段；缩进子弹与普通续行按解析器同口径拼进父条
    （``append_nested``/``append_continuation`` 都是空格拼接），否则跨多行的
    日志链子弹会只按首行计长，005 漏报；行内形态 ``**预期**：xxx`` 同样收录。
    """
    bullets: list[str] = []
    in_expect = False
    for raw in lines:
        stripped = raw.strip()
        if _BOLD_LINE_RE.match(stripped):
            marker = _EXPECT_SECTION_RE.match(stripped)
            in_expect = marker is not None
            if marker and marker.group("inline").strip():
                bullets.append(marker.group("inline").strip())
            continue
        if not in_expect or not stripped:
            continue
        match = _TOP_BULLET_RE.match(stripped)
        if match:
            bullets.append(match.group("body"))
        elif bullets:
            # 缩进子弹/编号子项/散文续行：剥掉子弹符号后挂到当前父条
            nested = _INDENT_BULLET_RE.match(raw)
            bullets[-1] = f"{bullets[-1]} {nested.group('body') if nested else stripped}"
    return bullets


@dataclass(frozen=True)
class LintFinding:
    """一条 lint 发现；case_id 为空串表示文档级问题。"""

    rule_id: str
    severity: str  # error | warning | info
    case_id: str
    line: int | None
    message: str
    evidence: str = ""


@dataclass(frozen=True)
class LintReport:
    doc_external_id: str
    findings: list[LintFinding] = field(default_factory=list)
    metrics: dict[str, object] = field(default_factory=dict)

    @property
    def errors(self) -> list[LintFinding]:
        return [f for f in self.findings if f.severity == "error"]


# ── 原文块切片 ───────────────────────────────────────────────────────────────

def _case_blocks(text: str, cases: list[TestCase]) -> dict[str, tuple[int, int]]:
    """每个用例占的原文行区间 [start, end)（1-based，end 排他）。

    叙事体起点是 ``### TC`` 标题行（=source_line）；表格体起点是其数据行。
    区间结束于下一个用例起点/下一个 ``##`` 标题/文档末尾。
    """
    lines = text.splitlines()
    starts = sorted({case.source_line for case in cases if case.source_line})
    h2_lines = [idx + 1 for idx, raw in enumerate(lines) if _H2_RE.match(raw.strip())]
    boundaries = sorted(set(starts + h2_lines + [len(lines) + 1]))
    blocks: dict[str, tuple[int, int]] = {}
    for case in cases:
        if not case.source_line:
            continue
        later = [b for b in boundaries if b > case.source_line]
        end = later[0] if later else len(lines) + 1
        blocks[case.external_id] = (case.source_line, end)
    return blocks


def _block_lines(text: str, block: tuple[int, int]) -> list[str]:
    lines = text.splitlines()
    start, end = block
    return lines[start - 1:end - 1]


def _is_table_case(text: str, case: TestCase) -> bool:
    if not case.source_line:
        return False
    lines = text.splitlines()
    row = lines[case.source_line - 1].strip()
    return row.startswith("|")


def _percentile(sorted_values: list[int], pct: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    rank = max(1, round(pct * len(sorted_values)))
    return float(sorted_values[rank - 1])


# ── 规则实现 ─────────────────────────────────────────────────────────────────

def _rule_001_marker_colon(text: str, blocks: dict[str, tuple[int, int]]) -> list[LintFinding]:
    findings: list[LintFinding] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        owner = ""
        for case_id, (start, end) in blocks.items():
            if start <= lineno < end:
                owner = case_id
                break
        stripped = raw.strip()
        if _BOLD_MARKER_RE.match(stripped) and not _HAS_COLON_RE.search(stripped):
            findings.append(LintFinding(
                rule_id="LINT-C-001", severity="error", case_id=owner, line=lineno,
                message=f"加粗小节标记「{stripped[:20]}」缺冒号，解析器不会识别该段",
                evidence=stripped[:_EVIDENCE_LIMIT],
            ))
    return findings


def _rule_003_step_sequence(
    case: TestCase, block: list[str]
) -> list[LintFinding]:
    findings: list[LintFinding] = []
    seen: list[int] = []
    for raw in block:
        if raw[:1].isspace():
            continue  # 缩进编号是嵌套子项，不参与步骤序号连续性
        match = re.match(r"^(\d+)\.\s+", raw)
        if match:
            seen.append(int(match.group(1)))
    expected = list(range(1, len(seen) + 1))
    if seen and seen != expected:
        findings.append(LintFinding(
            rule_id="LINT-C-003", severity="error", case_id=case.external_id,
            line=case.source_line,
            message=f"步骤编号不连续/重复：实际 {seen}，期望 {expected}",
            evidence=" ".join(block)[:_EVIDENCE_LIMIT],
        ))
    return findings


def _rule_010_records(
    case: TestCase, block: list[str]
) -> list[LintFinding]:
    findings: list[LintFinding] = []
    in_record = False
    for raw in block:
        stripped = raw.strip()
        if _RECORD_MARKER_RE.match(stripped):
            in_record = True
            if not _FOUR_DIGIT_YEAR_RE.search(stripped):
                findings.append(LintFinding(
                    rule_id="LINT-C-010", severity="info",
                    case_id=case.external_id, line=case.source_line,
                    message="实测记录标记缺四位年份，历史证据无法定时",
                    evidence=stripped[:_EVIDENCE_LIMIT],
                ))
            continue
        if not in_record:
            for symbol in _RETIRED_SYMBOLS:
                if symbol in stripped:
                    findings.append(LintFinding(
                        rule_id="LINT-C-010", severity="info",
                        case_id=case.external_id, line=case.source_line,
                        message=f"段外引用已下线符号 {symbol}，确认是否应迁入实测记录或更新现值",
                        evidence=stripped[:_EVIDENCE_LIMIT],
                    ))
                    break
    return findings


def _lint_case(
    text: str, case: TestCase, block: tuple[int, int] | None
) -> list[LintFinding]:
    findings: list[LintFinding] = []
    lines = _block_lines(text, block) if block else []
    is_table = _is_table_case(text, case)

    if case.lifecycle != "cancelled":
        # LINT-C-002：有步骤却零断言（标记故障/真缺预期的最终信号）
        if case.steps and case.oracle_missing:
            findings.append(LintFinding(
                rule_id="LINT-C-002", severity="error", case_id=case.external_id,
                line=case.source_line,
                message="用例有步骤但解析不到任何断言（检查 **预期**： 段标记与冒号）",
            ))

        # LINT-C-004：背景材料仍混在预期段（独立背景段缺失时的 context_notes）
        has_background_section = any(
            _BACKGROUND_MARKER_RE.match(raw.strip()) for raw in lines
        )
        if case.context_notes and not has_background_section:
            findings.append(LintFinding(
                rule_id="LINT-C-004", severity="warning", case_id=case.external_id,
                line=case.source_line,
                message=(f"{len(case.context_notes)} 条背景材料由预期段词表兜底分流，"
                         "应迁入独立「**背景与平台约束**」段"),
                evidence=case.context_notes[0][:_EVIDENCE_LIMIT],
            ))

        # LINT-C-005/006 在原文子弹上判定：入库 expectation 已剥反引号与显式
        # 标签，模型侧无法区分「显式人工」与「猜测人工」，也数不到日志 token。
        # 表格体无预期段，体检由 007 统一管辖。
        if not is_table:
            for raw_bullet in _expect_bullets(lines):
                if classify_bullet_role(raw_bullet) == "context":
                    continue  # C2 背景材料归 004，不按断言语体检
                stored = _strip_inline(raw_bullet)
                token_count = len(_BACKTICK_TOKEN_RE.findall(raw_bullet))
                if len(stored) > MAX_ORACLE_CHARS or token_count > MAX_ORACLE_LOG_TOKENS:
                    findings.append(LintFinding(
                        rule_id="LINT-C-005", severity="warning",
                        case_id=case.external_id, line=case.source_line,
                        message=(f"超长断言（{len(stored)} 字/"
                                 f"{token_count} 个日志 token），应按检查点拆条"),
                        evidence=stored[:_EVIDENCE_LIMIT],
                    ))
                explicit_type, _, _ = split_explicit_tag(raw_bullet)
                if explicit_type is None and classify_oracle(stored) == OracleType.MANUAL:
                    findings.append(LintFinding(
                        rule_id="LINT-C-006", severity="warning",
                        case_id=case.external_id, line=case.source_line,
                        message="人工断言缺显式【类型】标签，自动化分流只能靠关键词猜测",
                        evidence=stored[:_EVIDENCE_LIMIT],
                    ))

        if is_table:
            # LINT-C-007：表格体迁移进度指标，迁移完成后降级 info
            findings.append(LintFinding(
                rule_id="LINT-C-007", severity="warning", case_id=case.external_id,
                line=case.source_line,
                message="表格体用例：步骤压扁为单格，无法锚定 step_id，待转叙事体五段骨架",
            ))
        elif not any(_PRECOND_MARKER_RE.match(raw.strip()) for raw in lines):
            # LINT-C-008：叙事体活跃用例缺前置段
            findings.append(LintFinding(
                rule_id="LINT-C-008", severity="warning", case_id=case.external_id,
                line=case.source_line,
                message="缺独立 **前置**： 段（环境/授权/钩子/数据准备应前置化）",
            ))

        # LINT-C-009：锚点覆盖提示（用例级断言合法，仅 info）；
        # 表格体已由 007 单列，不再重复提示
        if (not is_table and case.steps and case.oracles
                and all(o.step_id is None for o in case.oracles)):
            findings.append(LintFinding(
                rule_id="LINT-C-009", severity="info", case_id=case.external_id,
                line=case.source_line,
                message=f"{len(case.oracles)} 条断言全部为用例级（step_id=None），"
                        "归属明确时可显式写「步骤 N」锚点",
            ))

    if block is not None and not is_table:
        findings.extend(_rule_003_step_sequence(case, lines))
        findings.extend(_rule_010_records(case, lines))
    return findings


def _build_metrics(
    cases: list[TestCase],
    findings: list[LintFinding],
    *,
    dead_letter_count: int,
) -> dict[str, object]:
    active = [c for c in cases if c.lifecycle != "cancelled"]
    cancelled = len(cases) - len(active)
    table_ids = {f.case_id for f in findings if f.rule_id == "LINT-C-007"}
    narrative_active = [c for c in active if c.external_id not in table_ids]

    all_oracles = [o for c in active for o in c.oracles]
    all_context = [n for c in active for n in c.context_notes]
    # 004 发现项精确锁定「无独立背景段、靠词表兜底」的用例，其 context_notes
    # 即 docs/17 §5.3 口径的预期段混入知识材料数
    mixed_case_ids = {f.case_id for f in findings if f.rule_id == "LINT-C-004"}
    mixed_bullets = sum(
        len(c.context_notes) for c in active
        if c.external_id in mixed_case_ids
    )
    lengths = sorted(len(o.expectation) for o in all_oracles)
    error_case_ids = {f.case_id for f in findings if f.severity == "error" and f.case_id}
    anchored = sum(1 for o in all_oracles if o.step_id is not None)
    with_precond = sum(1 for c in narrative_active if c.preconditions)

    total_bullets = len(all_oracles) + len(all_context)
    return {
        "cases_total": len(cases),
        "cases_active": len(active),
        "cases_cancelled": cancelled,
        "cases_table_form": len(table_ids),
        "structure_pass_rate": (
            round(1 - len(error_case_ids) / len(active), 4) if active else 1.0
        ),
        "oracle_missing_rate": (
            round(sum(1 for c in active if c.oracle_missing) / len(active), 4)
            if active else 0.0
        ),
        "knowledge_mix_in_rate": (
            round(mixed_bullets / total_bullets, 4) if total_bullets else 0.0
        ),
        "oracle_len_p50": _percentile(lengths, 0.5),
        "oracle_len_p95": _percentile(lengths, 0.95),
        "overlong_oracles": sum(1 for f in findings if f.rule_id == "LINT-C-005"),
        "parse_dead_letters": dead_letter_count,
        "anchor_coverage": (
            round(anchored / len(all_oracles), 4) if all_oracles else 0.0
        ),
        "precondition_coverage": (
            round(with_precond / len(narrative_active), 4)
            if narrative_active else 0.0
        ),
        "finding_counts": {
            severity: sum(1 for f in findings if f.severity == severity)
            for severity in ("error", "warning", "info")
        },
    }


# ── 入口 ─────────────────────────────────────────────────────────────────────

def lint_case_doc(
    text: str, result: CaseParseResult, doc_external_id: str
) -> LintReport:
    """对一份用例文档跑全部结构/可判定性/锚点/保鲜规则；纯函数，不读文件。"""
    cases = result.cases
    blocks = _case_blocks(text, cases)
    findings: list[LintFinding] = _rule_001_marker_colon(text, blocks)

    for dead in result.dead_letters:
        findings.append(LintFinding(
            rule_id="LINT-C-003", severity="error",
            case_id=dead.case_id_hint, line=dead.line,
            message=f"解析死信：{dead.error}",
            evidence=dead.snippet[:_EVIDENCE_LIMIT],
        ))

    for case in cases:
        findings.extend(_lint_case(text, case, blocks.get(case.external_id)))

    return LintReport(
        doc_external_id=doc_external_id,
        findings=findings,
        metrics=_build_metrics(
            cases, findings, dead_letter_count=len(result.dead_letters)
        ),
    )


def aggregate_reports(reports: list[LintReport]) -> dict[str, object]:
    """多文档汇总（CLI 与测试共用）：计数相加、率类指标按用例数加权重算。"""
    all_cases = sum(int(r.metrics["cases_total"]) for r in reports)
    active_cases = sum(int(r.metrics["cases_active"]) for r in reports)
    error_cases = set()
    findings: list[LintFinding] = []
    for report in reports:
        findings.extend(report.findings)
        error_cases.update(
            f.case_id for f in report.findings
            if f.severity == "error" and f.case_id
        )
    return {
        "docs": len(reports),
        "cases_total": all_cases,
        "cases_active": active_cases,
        "structure_pass_rate": (
            round(1 - len(error_cases) / active_cases, 4) if active_cases else 1.0
        ),
        "finding_counts": {
            severity: sum(1 for f in findings if f.severity == severity)
            for severity in ("error", "warning", "info")
        },
        "rules_violated": sorted({f.rule_id for f in findings}),
    }
