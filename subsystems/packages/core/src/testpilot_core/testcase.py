"""用例 Markdown 解析纯函数（docs/08 FR-CON-003）。

零 IO、零三方：吃文本，吐结构化 ``TestCase`` 与解析死信。覆盖 V1 用例文档的两种形态：

- 叙事体：``## 模块`` → ``### TC-xx 标题（FR 引用）P0`` → 可选 ``**前置**：`` →
  编号步骤 → ``**预期**：`` 子弹列表（可含嵌套）→ 可选独立 ``**背景与平台约束**``
  段（入 context_notes）→ 可选 ``**实测记录**``（docs/17 先导批起支持前置/背景段，
  无这两段的历史文档行为完全不变）；
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
# docs/17 先导批：前置段标记，同样允许标记与冒号间夹说明括号
_PRECOND_MARKER_RE = re.compile(r"^\*\*前置\*\*[^：:]*[：:]\s*(?P<inline>.*)$")
# docs/17 先导批：独立背景段「**背景与平台约束**（备注）：」，冒号可选（纯标题分段也合法），
# 背景段子弹统一进 context_notes 桶，与预期段内 C2 词表分流合流
_CONTEXT_SECTION_MARKER_RE = re.compile(
    r"^\*\*背景[^*]*\*\*[^：:]*[：:]?\s*(?P<inline>.*)$"
)
# 回写渲染与规范化模板都输出 **步骤**： 段标记；显式识别为 body 分段，
# 否则该行会被前置段续行逻辑误拼进最后一条前置（红证：回写往返夹具）
_STEP_SECTION_MARKER_RE = re.compile(r"^\*\*步骤\*\*[^：:]*[：:]")
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
    # L3-4 扩词（W2 红证）：产品语「提示」是可自动化的界面信号，旧词表漏认
    # 导致 AC-1 类验收整条兜底 manual。
    if re.search(r"页面|屏|横幅|弹窗|按钮|展示|显示|可见|出现|UI|入口|菜单|文案|提示", text):
        return OracleType.UI_ASSERTION
    # L3-4 扩词（W2 红证）：恢复/次数/重答/换题/注册/题数/难度 等产品语表达的是
    # 可断言的状态与数据事实，旧词表只认「计数/记录」等工程语，AC-3/AC-6 因此
    # 被误判 manual 并把整个用例单元打成探索性。
    if re.search(
        r"状态|账本|快照|字段|数据|记录|排程|取消|落库|UUID|计数|分桶|开关|权限"
        r"|恢复|次数|重答|答错|答对|换题|注册|题数|道数|难度",
        text,
    ):
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
# docs/01 回写标签内的锚点段：【UI 断言｜步骤3】，全角 ｜ 为主、半角 | 兼容
_TAG_ANCHOR_SPLIT_RE = re.compile(
    r"^(?P<label>.+?)\s*[｜|]\s*步骤\s*(?P<n>[0-9]+)\s*$"
)
# docs/01 正文显式步骤引用：只认阿拉伯数字；中文数字与事件描述（T0/解锁后/侧边键后）
# 没有「步骤/Step」前缀，天然不匹配——事件不是稳定序号，硬映射就是猜测
_STEP_ANCHOR_RE = re.compile(r"(?:步骤|\bStep)\s*([0-9]+)", re.IGNORECASE)


def split_explicit_tag(text: str) -> tuple[OracleType | None, int | None, str]:
    """拆分显式标签 ``【类型】`` / ``【类型｜步骤N】``。

    无标签或标签未知时返回 ``(None, None, 原文)``，调用方回退关键词分类器；
    标签类型已知但不带锚点时 step_id 为 None（用例级写法，旧字节风格）。
    """
    match = _EXPLICIT_TAG_RE.match(text.strip())
    if not match:
        return None, None, text
    raw_label = match.group("label").strip()
    rest = match.group("rest").strip()
    anchor_match = _TAG_ANCHOR_SPLIT_RE.match(raw_label)
    label = anchor_match.group("label").strip() if anchor_match else raw_label
    for oracle_type, name in EXPLICIT_TYPE_LABELS.items():
        if label == name:
            step_id = int(anchor_match.group("n")) if anchor_match else None
            return oracle_type, step_id, rest
    return None, None, text


def split_explicit_type(text: str) -> tuple[OracleType | None, str]:
    """拆分显式类型标签（不含锚点，兼容旧调用方）；无标签/未知标签返回 (None, 原文)。"""
    oracle_type, _, rest = split_explicit_tag(text)
    return oracle_type, rest


def extract_step_anchor(text: str) -> tuple[int | None, str]:
    """从断言正文提取显式步骤引用（docs/01 §4.2 保守口径）。

    返回 ``(锚点序号或 None, 歧义说明)``：唯一 N 才锚定；同条断言出现多个
    不同 N 属歧义，不锚并交调用方记死信；无引用返回 (None, "")。
    """
    numbers = {int(m.group(1)) for m in _STEP_ANCHOR_RE.finditer(text)}
    if not numbers:
        return None, ""
    if len(numbers) > 1:
        ordered = sorted(numbers)
        return None, "同一断言引用多个步骤: " + ", ".join(str(n) for n in ordered)
    return numbers.pop(), ""


# ── C2 知识分流（docs/01 §4.3，2026-11-16）─────────────────────────────────
# 词表刻意只收强结构信号，默认 assertion——宁可漏分流不可误杀可判定断言；
# 增减词表必须由 tests/test_context_notes.py 的红证夹具驱动。
BulletRole = Literal["assertion", "context"]
# 段首标记：平台约束/真机实锤/实测记录/历史（历史要求冒号或年份紧随，防「历史数据应…」
# 这类真断言误分流）
_CONTEXT_PREFIX_RE = re.compile(
    r"^(?:平台约束|真机实锤|实测记录|历史(?:记录)?(?:[:：]|\s*\d{4}))"
)
# 句首括号机制夹注：活样本「（2026-11-14 按代码现文案核对；机制见 X.y(…)）：」，
# 正文允许嵌套半角括号（setHaptics(active:false) 等代码符号），故要求三端齐备：
# 句首括号 + 关键词 + 句尾括号（可选冒号）。刻意要求句首就是括号——
# 「日志（…按代码现文案核对…」以日志开头且不以括号收尾，仍是真断言。
_CONTEXT_META_PAREN_RE = re.compile(
    r"^[（(].*(?:机制见|按代码现文案核对|代码现文案).*[）)]\s*[:：]?\s*$"
)
# 纯机制说明句式：活样本「iOS 26.4 不投递 LA .ended」「仅为其他版本/出口保留」
_CONTEXT_MECHANISM_RE = re.compile(
    r"iOS\s*\d+(?:\.\d+)*\s*不投递|仅为其他版本|出口保留"
)


def classify_bullet_role(text: str) -> BulletRole:
    """叙事体预期 bullet 的断言/背景知识分流（docs/01 §4.3 C2）。

    保守口径：默认 assertion，仅命中确定性结构词表才归 context；
    人审回写显式标签 ``【类型…】`` 是权威断言，任何词表不得覆盖。
    注意本函数面向**拼接完整**的 bullet 文本；入口期（首物理行可能未闭合）
    只用 ``_is_context_entry`` 的段首标记，句形规则在 build 期复核。
    """
    stripped = _strip_inline(text)
    explicit_type, _, _ = split_explicit_tag(stripped)
    if explicit_type is not None:
        return "assertion"
    if _CONTEXT_PREFIX_RE.match(stripped):
        return "context"
    if _CONTEXT_META_PAREN_RE.match(stripped):
        return "context"
    if _CONTEXT_MECHANISM_RE.search(stripped):
        return "context"
    return "assertion"


def _is_context_entry(text: str) -> bool:
    """入口期分流：只认段首标记（首物理行即完整的强信号）。

    机制夹注/句式可能跨多个物理行（活样本 TC-A1b 三行才闭合括号），
    入口文本不完整，句形类规则统一留到 build() 对拼接全文复核，避免漏分流。
    """
    stripped = _strip_inline(text)
    explicit_type, _, _ = split_explicit_tag(stripped)
    if explicit_type is not None:
        return False
    return bool(_CONTEXT_PREFIX_RE.match(stripped))


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
        # docs/01：与 oracle_texts 平行的步骤锚点表。None=该条无显式引用（用例级）；
        # 嵌套子弹/续行只拼文本、不改锚点，天然实现「嵌套继承顶层 bullet 锚点」。
        self.oracle_anchors: list[int | None] = []
        # 与锚点表平行的歧义说明（多步骤引用）；越界判定在 build 期（需步骤总数）
        self.oracle_anchor_notes: list[str] = []
        # C2：知识型材料分桶（平台约束/机制指针/历史记录），与断言桶互斥；
        # _last_bucket 决定嵌套子弹与续行的归属，保证子项随父 bullet 角色。
        # docs/17 先导批：独立「**背景与平台约束**」段的子弹也进同一桶。
        self.context_texts: list[str] = []
        self._last_bucket: Literal["oracle", "context"] | None = None
        # docs/17 先导批：「**前置**：」段累积器；模型/审批/回写渲染早已支持
        # preconditions，这里补齐 parser 抽取，老文档无该段时恒为空列表。
        self.preconditions: list[str] = []
        self.mode: Literal[
            "body", "precondition", "oracle", "context_section", "record"
        ] = "body"

    def append_bullet(self, text: str) -> None:
        """收录一条顶层预期 bullet：C2 先分流知识型材料，断言桶再定步骤锚点。

        入口只按段首标记分流（首行即完整的强信号）；跨多行才完整的机制夹注
        留 build 期复核，使嵌套子项先随父桶汇聚、最终角色由完整文本裁定。
        标签锚点（人审回写）优先于正文引用；嵌套子弹与续行走 append_nested/
        append_continuation，不经过本方法，因此无法引入或覆盖父 bullet 的锚点与角色。
        """
        if _is_context_entry(text):
            self.context_texts.append(text)
            self._last_bucket = "context"
            return
        _, tag_step_id, _ = split_explicit_tag(text)
        if tag_step_id is not None:
            anchor, note = tag_step_id, ""
        else:
            anchor, note = extract_step_anchor(text)
        self.oracle_texts.append(text)
        self.oracle_anchors.append(anchor)
        self.oracle_anchor_notes.append(note)
        self._last_bucket = "oracle"

    def append_precondition(self, text: str) -> None:
        """前置段顶层子弹开新条；嵌套/续行由 append_nested/append_continuation 拼接。"""
        self.preconditions.append(text)

    def append_context_section(self, text: str) -> None:
        """独立背景段顶层子弹：进 C2 同一背景桶，嵌套子项随父汇聚。"""
        self.context_texts.append(text)
        self._last_bucket = "context"

    def append_nested(self, text: str) -> None:
        """缩进子弹：拼到当前段最后一条，角色与锚点都继承父 bullet。"""
        if self.mode == "precondition":
            bucket_texts = self.preconditions
        else:
            bucket_texts = (
                self.context_texts if self._last_bucket == "context" else self.oracle_texts
            )
        if bucket_texts:
            bucket_texts[-1] = f"{bucket_texts[-1]} {_strip_inline(text)}"

    def append_continuation(self, line: str) -> None:
        """非标记的续行：按当前段落挂到最后一个步骤/前置/当前桶预期。"""
        text = line.strip()
        if not text:
            return
        if self.mode == "precondition" and self.preconditions:
            self.preconditions[-1] = f"{self.preconditions[-1]} {_strip_inline(text)}"
        elif self.mode in ("oracle", "context_section") and (
            self.oracle_texts or self.context_texts
        ):
            bucket = (
                self.context_texts
                if self._last_bucket == "context"
                else self.oracle_texts
            )
            if bucket:
                bucket[-1] = f"{bucket[-1]} {_strip_inline(text)}"
        elif self.mode == "body" and self.steps:
            self.steps[-1] = f"{self.steps[-1]} {_strip_inline(text)}"

    def build(self, *, module: str, doc_external_id: str, source_system: str,
              version: str | None
              ) -> tuple[TestCase | None, list[CaseParseError]]:
        if not self.steps and not self.oracle_texts:
            return None, [CaseParseError(
                doc_external_id=doc_external_id, line=self.line,
                case_id_hint=self.case_id, snippet=self.title,
                error="用例块既无步骤也无预期，无法结构化",
            )]
        oracles = []
        anchor_errors: list[CaseParseError] = []
        step_count = len(self.steps)
        # C2 两阶段分流：对拼接完整的断言文本复核句形类词表（机制夹注跨多行时
        # 入口期无法判定），命中者移入背景桶，其锚点与锚点死信一并撤销——
        # 背景材料不参与步骤关联，也不产出断言。
        final_texts: list[str] = []
        final_anchors: list[int | None] = []
        final_notes: list[str] = []
        for text, raw_anchor, note in zip(
            self.oracle_texts, self.oracle_anchors, self.oracle_anchor_notes
        ):
            if classify_bullet_role(text) == "context":
                self.context_texts.append(text)
                continue
            final_texts.append(text)
            final_anchors.append(raw_anchor)
            final_notes.append(note)
        for text, raw_anchor, note in zip(final_texts, final_anchors, final_notes):
            # 显式标签优先（S2.5 人审回写文档）；无标签回退关键词分类，历史行为不变
            explicit_type, _, expectation = split_explicit_tag(text)
            step_id: int | None = None
            if note:
                # 歧义不静默：降级用例级并留死信，整批继续（docs/01 §4.2 + AC2）
                anchor_errors.append(CaseParseError(
                    doc_external_id=doc_external_id, line=self.line,
                    case_id_hint=self.case_id, snippet=text[:120], error=note,
                ))
            elif raw_anchor is not None:
                if 1 <= raw_anchor <= step_count:
                    step_id = raw_anchor
                else:
                    # 越界 fail-loud：用户拍板的死信策略，step_id 置 None 不阻断整批
                    anchor_errors.append(CaseParseError(
                        doc_external_id=doc_external_id, line=self.line,
                        case_id_hint=self.case_id, snippet=text[:120],
                        error=(f"步骤锚点 {raw_anchor} 越界（用例共 {step_count} 个步骤），"
                               "降级为用例级断言"),
                    ))
            oracles.append(
                Oracle(
                    type=explicit_type or classify_oracle(text),
                    expectation=expectation,
                    source_refs=[make_urn(source_system, "document", doc_external_id)],
                    confidence=0.5,  # 文档解析只做确定性归类，置信度留给 S2.4 oracle 挖掘复评
                    step_id=step_id,
                )
            )
        case = TestCase(
            urn=make_urn(source_system, "test_case", f"{doc_external_id}#{self.case_id}"),
            external_id=_strip_inline(self.case_id),
            title=self.title or _strip_inline(self.case_id),
            preconditions=[_strip_inline(p) for p in self.preconditions],
            steps=[_strip_inline(s) for s in self.steps],
            oracles=oracles,
            # C2：知识材料不充当预期，只有背景条目的用例仍是 oracle_missing
            context_notes=list(self.context_texts),
            oracle_missing=bool(self.steps) and not final_texts,
            tags=self.tags,
            version=version,
            module=module,
            priority=self.priority,
            lifecycle="cancelled" if self.cancelled else "active",
            source_line=self.line,
        )
        return case, anchor_errors


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
        case, errors = builder.build(
            module=module, doc_external_id=doc_external_id,
            source_system=source_system, version=version)
        if case is not None:
            cases.append(case)
        dead.extend(errors)
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
        precond = _PRECOND_MARKER_RE.match(stripped)
        if precond:
            builder.mode = "precondition"
            inline = precond.group("inline").strip()
            if inline:
                builder.append_precondition(_strip_inline(inline))
            continue
        expect = _EXPECT_MARKER_RE.match(stripped)
        if expect:
            builder.mode = "oracle"
            inline = expect.group("inline").strip()
            if inline:
                builder.append_bullet(_strip_inline(inline))
            continue
        # docs/17：独立背景段必须在通用加粗小节分支前识别（标题行带括号备注，
        # 不以 ** 收尾，本也落不进通用分支，此处显式处理语义更清晰）
        context_section = _CONTEXT_SECTION_MARKER_RE.match(stripped)
        if context_section:
            builder.mode = "context_section"
            inline = context_section.group("inline").strip()
            if inline:
                builder.append_context_section(_strip_inline(inline))
            continue
        if _STEP_SECTION_MARKER_RE.match(stripped):
            # 显式步骤段标记：结束前置/背景分段，编号行由既有步骤分支收录
            builder.mode = "body"
            continue
        if _RECORD_MARKER_RE.match(stripped):
            builder.mode = "record"
            continue
        if stripped.startswith("**") and stripped.endswith("**"):
            # 其他加粗小节标题（如「平台约束」）：结束预期收集但不丢已收集内容
            builder.mode = "body"
            continue
        # 顶格编号才是步骤（stripped 已去缩进，等价于原文列 0 起始）；
        # 前置/背景段遇顶格编号说明该段结束、步骤开始，切回 body 再收录
        step = _STEP_RE.match(stripped)
        if step and not line[0].isspace() and builder.mode != "oracle":
            if builder.mode in ("precondition", "context_section"):
                builder.mode = "body"
            builder.steps.append(step.group("body").strip())
            continue
        bullet = _BULLET_RE.match(line)
        if bullet:
            indented = len(bullet.group("indent")) > 0
            if builder.mode == "precondition":
                if indented and builder.preconditions:
                    builder.append_nested(bullet.group("body"))
                else:
                    builder.append_precondition(_strip_inline(bullet.group("body")))
                continue
            if builder.mode == "context_section":
                # 背景段与预期段共用 context 桶，嵌套子项随父汇聚
                if indented and (builder.oracle_texts or builder.context_texts):
                    builder.append_nested(bullet.group("body"))
                else:
                    builder.append_context_section(_strip_inline(bullet.group("body")))
                continue
            if builder.mode == "oracle":
                # 顶层子弹开新条（入口处 C2 分流 + 定锚点）；缩进子弹（嵌套）挂当前桶
                # 最后一条并继承其角色与锚点
                if indented and (builder.oracle_texts or builder.context_texts):
                    builder.append_nested(bullet.group("body"))
                else:
                    builder.append_bullet(_strip_inline(bullet.group("body")))
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
            # docs/01 §4.2 规则 4：表格体单预期格无步骤结构，step_id 恒 None；
            # C2：表格体无知识/断言双段结构，context_notes 恒空（默认值，不显式传）
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
