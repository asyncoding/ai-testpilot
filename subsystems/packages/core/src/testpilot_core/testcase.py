"""用例 Markdown 解析纯函数（docs/08 FR-CON-003）。

零 IO、零三方：吃文本，吐结构化 ``TestCase`` 与解析死信。覆盖 V1 用例文档的两种形态：

- 叙事体：``## 模块`` → ``### TC-xx 标题（FR 引用）P0`` → 编号步骤 →
  ``**预期**：`` 子弹列表（可含嵌套）→ 可选 ``**实测记录**``；
- 表格体：含 ``ID`` 表头列的 Markdown 表格，列名不统一（步骤要点/环境+步骤/级…），
  按表头语义映射列位。

纪律：
- 缺预期显式 ``oracle_missing=True`` 但不丢弃（FR-CON-003）；
- 取消态（删除线/优先级=取消）保留为 ``lifecycle=cancelled``，不进漏报；
- 单条解析失败产出 ``CaseParseError`` 死信，整批继续（AC2）；
- 非用例表格（无 ID 列，如「自动化现状」）整体跳过；
- 代码围栏内的 ``#``/``|`` 不当 Markdown 结构处理。
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field

from .enums import OracleType
from .models import Oracle, TestCase
from .urn import make_urn

_CASE_SOURCE_SYSTEM = "fs-cases"

# 允许删除线包裹的取消态标题：### ~~TC-Xn ...~~
_TC_HEADING_RE = re.compile(r"^###\s+(?:~~)?(?P<id>TC-[0-9A-Za-z]+)\s*(?P<rest>.*?)\s*$")
_MODULE_HEADING_RE = re.compile(r"^##\s+(?P<title>.+?)\s*$")
# 只认顶格编号：预期段内缩进的 1./2. 是嵌套子项，走续行逻辑挂到当前 oracle
_STEP_RE = re.compile(r"^\d+\.\s+(?P<body>.+?)\s*$")
_BULLET_RE = re.compile(r"^(?P<indent>\s*)-\s+(?P<body>.+?)\s*$")
# 允许 **预期**（备注）： 形态——真档 TC-A7 在标记与冒号间夹了回归说明括号
_EXPECT_MARKER_RE = re.compile(r"^\*\*预期\*\*[^：:]*[：:]\s*(?P<inline>.*)$")
_RECORD_MARKER_RE = re.compile(r"^\*\*实测记录")
_PRIORITY_RE = re.compile(r"\b(P[0-3])\b")
_FR_REF_RE = re.compile(r"FR-[0-9A-Za-z]+")
_TABLE_SEP_RE = re.compile(r"^\s*\|?[\s:|-]+\|?\s*$")
_LOG_TOKEN_RE = re.compile(r"`[A-Z][A-Z0-9 _./=-]+`|日志|os_log|log\b", re.IGNORECASE)

# 表格表头语义：按优先级取第一个命中的列
_EXPECT_HEADERS = ("预期",)
_STEP_HEADERS = ("步骤要点", "步骤")
_PRIORITY_HEADERS = ("优先级", "级")
_ENV_HEADERS = ("环境",)


class CaseParseError(BaseModel):
    """单条用例解析失败的死信载荷（连接器落 dead_letters，UI 可查）。"""

    doc_external_id: str
    line: int | None = None
    case_id_hint: str = ""
    snippet: str = ""
    error: str


class CaseParseResult(BaseModel):
    cases: list[TestCase] = Field(default_factory=list)
    dead_letters: list[CaseParseError] = Field(default_factory=list)


# ── 行内工具 ────────────────────────────────────────────────────────────────

def _strip_inline(text: str) -> str:
    """去掉行内 Markdown 装饰（粗体/删除线/反引号），折叠空白。"""
    text = re.sub(r"~~(.+?)~~", r"\1", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = text.replace("`", "")
    return re.sub(r"\s+", " ", text).strip()


def _is_cancelled(text: str) -> bool:
    return "~~" in text or "取消" in text


def classify_oracle(text: str) -> OracleType:
    """把一条预期文本确定性归入四类 oracle（FR-TD-003 的分类口径前置到解析期）。

    优先级刻意保守：日志信号最强（含反引号大写日志 token 或「日志」字样）；
    主观感受必须人工见证 → manual；再分 UI 可见性与状态/数据；拿不准归 manual，
    绝不硬判成可自动断言的类型。
    """
    if _LOG_TOKEN_RE.search(text):
        return OracleType.LOG_ASSERTION
    if re.search(r"主观|手感|听到|听/|人工|亲自", text):
        return OracleType.MANUAL
    if re.search(r"页面|屏|横幅|弹窗|按钮|展示|显示|可见|出现|UI|入口|菜单|文案", text):
        return OracleType.UI_ASSERTION
    if re.search(r"状态|账本|快照|字段|数据|记录|排程|取消|落库|UUID|计数|分桶|开关|权限", text):
        return OracleType.STATE_ASSERTION
    return OracleType.MANUAL


# S2.5 人审回写的显式类型标签：Markdown 预期子弹以「【UI 断言】…」开头时，
# 类型由标签权威给定（不再走关键词猜测），保证回写文档被重新摄入时类型无损
# round-trip；无标签的历史文档行为完全不变，仍走 classify_oracle。
EXPLICIT_TYPE_LABELS: dict[OracleType, str] = {
    OracleType.UI_ASSERTION: "UI 断言",
    OracleType.LOG_ASSERTION: "日志断言",
    OracleType.STATE_ASSERTION: "状态断言",
    OracleType.MANUAL: "人工确认",
}
_EXPLICIT_TAG_RE = re.compile(r"^【(?P<label>[^】]+)】\s*(?P<rest>.*)$")


def split_explicit_type(text: str) -> tuple[OracleType | None, str]:
    """拆分显式类型标签；无标签/未知标签返回 (None, 原文)，调用方回退分类器。"""
    match = _EXPLICIT_TAG_RE.match(text.strip())
    if not match:
        return None, text
    label = match.group("label").strip()
    for oracle_type, name in EXPLICIT_TYPE_LABELS.items():
        if label == name:
            return oracle_type, match.group("rest").strip()
    return None, text


# ── 叙事体解析 ──────────────────────────────────────────────────────────────

class _NarrativeBuilder:
    """单个 ``### TC`` 块的累积器；oracle 段支持嵌套子弹挂到同一条预期。"""

    def __init__(self, case_id: str, heading_rest: str, line: int) -> None:
        self.case_id = case_id
        self.line = line
        self.cancelled = _is_cancelled(heading_rest) or _is_cancelled(case_id)
        priority_match = _PRIORITY_RE.search(heading_rest)
        self.priority = priority_match.group(1) if priority_match else ""
        self.title = _PRIORITY_RE.sub("", _strip_inline(heading_rest)).strip()
        self.tags = _FR_REF_RE.findall(heading_rest)
        self.steps: list[str] = []
        self.oracle_texts: list[str] = []
        self.mode: Literal["body", "oracle", "record"] = "body"

    def append_continuation(self, line: str) -> None:
        """非标记的续行：按当前段落挂到最后一个步骤或预期。"""
        text = line.strip()
        if not text:
            return
        if self.mode == "oracle" and self.oracle_texts:
            self.oracle_texts[-1] = f"{self.oracle_texts[-1]} {_strip_inline(text)}"
        elif self.mode == "body" and self.steps:
            self.steps[-1] = f"{self.steps[-1]} {_strip_inline(text)}"

    def build(self, *, module: str, doc_external_id: str, source_system: str,
              version: str | None) -> tuple[TestCase | None, CaseParseError | None]:
        if not self.steps and not self.oracle_texts:
            return None, CaseParseError(
                doc_external_id=doc_external_id, line=self.line,
                case_id_hint=self.case_id, snippet=self.title,
                error="用例块既无步骤也无预期，无法结构化",
            )
        oracles = []
        for text in self.oracle_texts:
            # 显式标签优先（S2.5 人审回写文档）；无标签回退关键词分类，历史行为不变
            explicit_type, expectation = split_explicit_type(text)
            oracles.append(
                Oracle(
                    type=explicit_type or classify_oracle(text),
                    expectation=expectation,
                    source_refs=[make_urn(source_system, "document", doc_external_id)],
                    confidence=0.5,  # 文档解析只做确定性归类，置信度留给 S2.4 oracle 挖掘复评
                )
            )
        case = TestCase(
            urn=make_urn(source_system, "test_case", f"{doc_external_id}#{self.case_id}"),
            external_id=_strip_inline(self.case_id),
            title=self.title or _strip_inline(self.case_id),
            steps=[_strip_inline(s) for s in self.steps],
            oracles=oracles,
            oracle_missing=bool(self.steps) and not self.oracle_texts,
            tags=self.tags,
            version=version,
            module=module,
            priority=self.priority,
            lifecycle="cancelled" if self.cancelled else "active",
            source_line=self.line,
        )
        return case, None


def _parse_narrative(lines: list[tuple[int, str]], *, doc_external_id: str,
                     source_system: str, version: str | None,
                     dead: list[CaseParseError]) -> list[TestCase]:
    """叙事体扫描；表格块由调用方在进入本函数前剔除，这里只处理标题/列表行。"""
    cases: list[TestCase] = []
    module = ""
    builder: _NarrativeBuilder | None = None
    in_fence = False

    def flush() -> None:
        nonlocal builder
        if builder is None:
            return
        case, error = builder.build(
            module=module, doc_external_id=doc_external_id,
            source_system=source_system, version=version)
        if case is not None:
            cases.append(case)
        if error is not None:
            dead.append(error)
        builder = None

    for lineno, raw in lines:
        line = raw.rstrip("\n")
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if not stripped:
            continue
        if stripped.startswith("####"):
            continue  # 四级标题是用例块内的小节，不切换状态
        module_match = _MODULE_HEADING_RE.match(stripped)
        if module_match:  # 正则已限定恰好两个 #，#/### 不会误入
            flush()
            module = _strip_inline(module_match.group("title"))
            continue
        heading = _TC_HEADING_RE.match(stripped)
        if heading:
            flush()
            builder = _NarrativeBuilder(heading.group("id"), heading.group("rest"), lineno)
            continue
        if builder is None:
            continue
        expect = _EXPECT_MARKER_RE.match(stripped)
        if expect:
            builder.mode = "oracle"
            inline = expect.group("inline").strip()
            if inline:
                builder.oracle_texts.append(_strip_inline(inline))
            continue
        if _RECORD_MARKER_RE.match(stripped):
            builder.mode = "record"
            continue
        if stripped.startswith("**") and stripped.endswith("**"):
            # 其他加粗小节标题（如「平台约束」）：结束预期收集但不丢已收集内容
            builder.mode = "body"
            continue
        # 顶格编号才是步骤（stripped 已去缩进，等价于原文列 0 起始）
        step = _STEP_RE.match(stripped)
        if step and not line[0].isspace() and builder.mode != "oracle":
            builder.steps.append(step.group("body").strip())
            continue
        bullet = _BULLET_RE.match(line)
        if bullet and builder.mode == "oracle":
            # 顶层子弹开新条；缩进子弹（嵌套）挂前一条
            if len(bullet.group("indent")) > 0 and builder.oracle_texts:
                builder.oracle_texts[-1] = (
                    f"{builder.oracle_texts[-1]} {_strip_inline(bullet.group('body'))}")
            else:
                builder.oracle_texts.append(_strip_inline(bullet.group("body")))
            continue
        builder.append_continuation(line)
    flush()
    return cases


# ── 表格体解析 ──────────────────────────────────────────────────────────────

def _split_row(line: str) -> list[str]:
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split("|")]


def _normalize_header(cell: str) -> str:
    return _strip_inline(cell).replace("：", ":")


def _parse_table_block(header_line: str, rows: list[tuple[int, str]], *, module: str,
                       doc_external_id: str, source_system: str,
                       version: str | None, dead: list[CaseParseError]) -> list[TestCase]:
    headers = [_normalize_header(c) for c in _split_row(header_line)]
    if "ID" not in headers:
        return []  # 非用例表（自动化现状/缺陷定级等），整体跳过
    id_col = headers.index("ID")
    expect_col = next((i for i, h in enumerate(headers)
                       if any(k in h for k in _EXPECT_HEADERS)), None)
    step_col = next((i for i, h in enumerate(headers)
                     if any(k in h for k in _STEP_HEADERS)), None)
    pri_col = next((i for i, h in enumerate(headers)
                    if any(k in h for k in _PRIORITY_HEADERS)), None)
    env_col = next((i for i, h in enumerate(headers)
                    if any(k in h for k in _ENV_HEADERS)), None)
    width = len(headers)
    cases: list[TestCase] = []

    for lineno, raw in rows:
        cells = _split_row(raw)
        # ID 先提取：即使列数损坏，死信也要自报身份（FR-CON-003 AC2 UI 可查）
        raw_id = cells[id_col].strip() if id_col < len(cells) else ""
        case_id = _strip_inline(raw_id)
        if len(cells) != width:
            dead.append(CaseParseError(
                doc_external_id=doc_external_id, line=lineno,
                case_id_hint=case_id, snippet=raw.strip()[:120],
                error=f"表格列数 {len(cells)} 与表头 {width} 不一致",
            ))
            continue
        if not case_id or not case_id.startswith("TC-"):
            dead.append(CaseParseError(
                doc_external_id=doc_external_id, line=lineno,
                case_id_hint=case_id, snippet=raw.strip()[:120],
                error="ID 列缺失或非 TC- 前缀",
            ))
            continue
        priority = ""
        cancelled = _is_cancelled(raw_id)
        if pri_col is not None and pri_col < len(cells):
            pri_cell = _strip_inline(cells[pri_col])
            if "取消" in pri_cell:
                cancelled = True
            elif pri_match := _PRIORITY_RE.search(pri_cell):
                priority = pri_match.group(1)
        step_text = _strip_inline(cells[step_col]) if step_col is not None and step_col < len(cells) else ""
        expect_text = _strip_inline(cells[expect_col]) if expect_col is not None and expect_col < len(cells) else ""
        tags: list[str] = []
        if env_col is not None and env_col < len(cells) and cells[env_col].strip():
            tags.append(f"env:{cells[env_col].strip()}")
        oracles: list[Oracle] = []
        if expect_text:
            oracles.append(Oracle(
                type=classify_oracle(expect_text),
                expectation=expect_text,
                source_refs=[make_urn(source_system, "document", doc_external_id)],
                confidence=0.5,
            ))
        cases.append(TestCase(
            urn=make_urn(source_system, "test_case", f"{doc_external_id}#{case_id}"),
            external_id=case_id,
            title=step_text or case_id,
            steps=[step_text] if step_text else [],
            oracles=oracles,
            oracle_missing=bool(step_text) and not expect_text,
            tags=tags,
            version=version,
            module=module,
            priority=priority,
            lifecycle="cancelled" if cancelled else "active",
            source_line=lineno,
        ))
    return cases


# ── 入口 ────────────────────────────────────────────────────────────────────

def parse_case_markdown(
    text: str,
    *,
    doc_external_id: str,
    source_system: str = _CASE_SOURCE_SYSTEM,
    version: str | None = None,
) -> CaseParseResult:
    """解析一份用例 Markdown；纯函数，不读文件、不写库。

    ``doc_external_id`` 由连接器给（根别名/相对路径），参与 URN 拼接，
    同文档重复解析产生相同 URN（重跑幂等的基础）。
    """
    indexed = list(enumerate(text.splitlines(), start=1))
    dead: list[CaseParseError] = []
    cases: list[TestCase] = []

    # 先标出代码围栏行：围栏内的 # 标题、| 表格都是正文，不参与结构解析
    fenced: set[int] = set()
    inside = False
    for lineno, raw in indexed:
        if raw.strip().startswith("```"):
            inside = not inside
            fenced.add(lineno)
            continue
        if inside:
            fenced.add(lineno)

    # 第一遍：识别表格块（连续 | 行，第二行是分隔行），把块内行从叙事流中摘出
    table_spans: list[tuple[int, int]] = []
    i = 0
    while i < len(indexed):
        lineno, raw = indexed[i]
        if lineno not in fenced and raw.strip().startswith("|") and i + 1 < len(indexed) \
                and _TABLE_SEP_RE.match(indexed[i + 1][1] or ""):
            start = i
            i += 2  # 跳过表头与分隔行
            block_rows: list[tuple[int, str]] = []
            while i < len(indexed) and indexed[i][1].strip().startswith("|"):
                block_rows.append(indexed[i])
                i += 1
            table_spans.append((start, i))
            # 模块归属取表格上方最近一个 ## 标题（跳过围栏行）
            module = ""
            for back in range(start - 1, -1, -1):
                back_lineno, back_raw = indexed[back]
                if back_lineno in fenced:
                    continue
                m = _MODULE_HEADING_RE.match(back_raw.strip())
                if m:
                    module = _strip_inline(m.group("title"))
                    break
            cases.extend(_parse_table_block(
                indexed[start][1], block_rows, module=module,
                doc_external_id=doc_external_id, source_system=source_system,
                version=version, dead=dead,
            ))
        else:
            i += 1

    table_line_set = {ln for start, end in table_spans for ln, _ in indexed[start:end]}
    narrative_lines = [
        item for item in indexed
        if item[0] not in table_line_set and item[0] not in fenced
    ]
    cases.extend(_parse_narrative(
        narrative_lines, doc_external_id=doc_external_id,
        source_system=source_system, version=version, dead=dead,
    ))
    return CaseParseResult(cases=cases, dead_letters=dead)
