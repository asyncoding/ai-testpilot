"""需求 Markdown 骨架抽取纯函数（docs/08 FR-TD-001）。

零 IO、零三方：吃文本，吐 ``RequirementSkeleton``（spec 形状）+ 富结构结果与缺口。
覆盖 V1 需求文档的实际形态：

- ``## 5. 功能需求`` 下若干 ``### x.y 模块`` 小节，每个小节一张
  ``| ID | 需求 | 优先级 |`` 表（FR-1 / FR-2a 子编号 / 删除线取消行 / 缺优先级）；
- ``## 6. 非功能需求`` 的 ``| ID | 需求 |`` 两列表（NFR-1…）；
- ``## 7. 验收标准`` 顶格编号列表（条目可跨多行，可能显式提及 FR ID）；
- 风险/元信息等无 FR/NFR ID 的表整体跳过；代码围栏内的标题/表格不是结构。

显式标缺不编造（FR-TD-001 的第一纪律）：

- 文档没有验收标准节 → ``ac_section_missing`` 缺口 + 每个功能点一条 present=False；
- 有验收节但某功能点没有任何 AC 覆盖（确定性显式 ID 提及，或模型链接）→
  该功能点一条 present=False，原文一条不丢、模型一个字不替；
- 模型只允许产出「源文档存在的 AC 序号 + FR ID」链接，未知 ID/越界序号整包
  fail-closed（``SkeletonValidationError``），与问答引用白名单同一纪律。
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field

from .models import AcceptanceCriterion, RequirementSkeleton
from .urn import make_urn

_REQ_SOURCE_SYSTEM = "fs-reqs"

# L3-5：前导负向字母回顾，排除 NFR-1 内的 FR-1 子串碰撞（W2 红证：AC-7 标题
# （FR-7、NFR-1）被旧正则链出假 FR-1，并经 join 实际扇出到无关用例单元）。
_FR_ID_RE = re.compile(r"(?<![A-Za-z])FR-\d+[a-z]?")
_NFR_ID_RE = re.compile(r"NFR-\d+")
_FR_CELL_RE = re.compile(r"^(?:~~)?(?P<id>FR-\d+[a-z]?)(?:~~)?$")
_NFR_CELL_RE = re.compile(r"^(?P<id>NFR-\d+)$")
_PRIORITY_RE = re.compile(r"\b(P[0-3])\b")
_H2_RE = re.compile(r"^##\s+(?P<title>.+?)\s*$")
_H3_RE = re.compile(r"^###\s+(?P<title>.+?)\s*$")
_AC_ITEM_RE = re.compile(r"^\d+\.\s+(?P<body>.+?)\s*$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?[\s:|-]+\|?\s*$")

LinkingSource = Literal["deterministic", "model", "degraded"]
GapKind = Literal[
    "ac_section_missing",
    "ac_missing_for_feature",
    "priority_missing",
    "malformed_row",
    "cancelled_feature",
]


class SkeletonValidationError(ValueError):
    """模型链接载荷未通过源文档白名单校验（未知 FR ID/越界 AC 序号/形状非法）。

    刻意继承 ValueError：调用方按降级路径处理，绝不接受幻觉引用。
    """


class RequirementParseError(BaseModel):
    """单行/单表解析失败的死信载荷（连接器落 dead_letters，UI 可查）。"""

    doc_external_id: str
    line: int | None = None
    subject_hint: str = ""
    snippet: str = ""
    error: str


class RequirementFeature(BaseModel):
    urn: str
    fr_id: str
    text: str
    priority: Literal["P0", "P1", "P2", "P3", ""] = ""
    module: str = ""
    lifecycle: Literal["active", "cancelled"] = "active"
    source_line: int | None = None


class RequirementRule(BaseModel):
    urn: str
    rule_id: str
    text: str
    source_line: int | None = None


class RequirementAcceptance(BaseModel):
    """验收标准富结构：序号 0-based 供模型链接引用，linked 只收白名单内 FR ID。

    L3-6 起把显式 FR 引用分两级（W2 红证：AC-6 正文「FR-10 降级除外」被当标题
    ID 入链，导致无关用例反向 join）：

    - ``primary_fr_ids``：AC 句首括号（如「（FR-3、FR-3a）」）声明的 FR，是这条
      验收口径的法定归属；无句首括号的文档该列表为空。
    - ``secondary_fr_ids``：正文行内提及的 FR，可追溯但不享有标题级 join 权重。
    - ``linked_fr_ids`` 保持「主+次并集」旧口径不变，覆盖率/gap 对账/脊柱产点
      全部沿用，消费侧需要标题级口径时自行读 primary（见 oracle_mining 源①）。
    """

    index: int
    text: str
    source_line: int | None = None
    linked_fr_ids: list[str] = Field(default_factory=list)
    primary_fr_ids: list[str] = Field(default_factory=list)
    secondary_fr_ids: list[str] = Field(default_factory=list)


class RequirementGap(BaseModel):
    kind: GapKind
    subject: str
    detail: str


class RequirementParseResult(BaseModel):
    doc_urn: str
    external_id: str
    features: list[RequirementFeature] = Field(default_factory=list)
    rules: list[RequirementRule] = Field(default_factory=list)
    acceptance: list[RequirementAcceptance] = Field(default_factory=list)
    gaps: list[RequirementGap] = Field(default_factory=list)
    dead_letters: list[RequirementParseError] = Field(default_factory=list)
    modules: list[str] = Field(default_factory=list)
    linking_source: LinkingSource = "deterministic"
    version: str | None = None

    @property
    def active_features(self) -> list[RequirementFeature]:
        return [f for f in self.features if f.lifecycle == "active"]

    def to_skeleton(self) -> RequirementSkeleton:
        """投影成 docs/08 FR-TD-001 约定的四段骨架（富结构留在本结果里供 S2.3）。"""
        criteria = [
            AcceptanceCriterion(text=ac.text, present=True) for ac in self.acceptance
        ]
        covered = {fid for ac in self.acceptance for fid in ac.linked_fr_ids}
        for feature in self.active_features:
            if feature.fr_id not in covered:
                if not self.acceptance:
                    detail = "文档无验收标准节，验收口径整体缺失"
                else:
                    detail = "未在文档「验收标准」节找到对应口径"
                criteria.append(AcceptanceCriterion(
                    text=f"{feature.fr_id} {detail}", present=False,
                ))
        return RequirementSkeleton(
            source_urn=self.doc_urn,
            features=[f"{f.fr_id} {f.text}" for f in self.active_features],
            rules=[f"{r.rule_id} {r.text}" for r in self.rules],
            acceptance_criteria=criteria,
            modules=list(self.modules),
        )


# ── 行内工具 ────────────────────────────────────────────────────────────────

def _strip_inline(text: str) -> str:
    text = re.sub(r"~~(.+?)~~", r"\1", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = text.replace("`", "")
    return re.sub(r"\s+", " ", text).strip()


def _split_row(line: str) -> list[str]:
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split("|")]


def _fenced_lines(indexed: list[tuple[int, str]]) -> set[int]:
    fenced: set[int] = set()
    inside = False
    for lineno, raw in indexed:
        if raw.strip().startswith("```"):
            inside = not inside
            fenced.add(lineno)
            continue
        if inside:
            fenced.add(lineno)
    return fenced


def _section_kind(title: str) -> Literal["fr", "nfr", "ac", "other"]:
    # 「非功能需求」包含「功能需求」四字，必须先判非功能
    if "非功能" in title:
        return "nfr"
    if "验收" in title:
        return "ac"
    if "功能需求" in title or re.search(r"\bFR\b", title):
        return "fr"
    return "other"


# ── 表格解析 ────────────────────────────────────────────────────────────────

def _parse_requirement_table(
    header_line: str,
    rows: list[tuple[int, str]],
    *,
    kind: Literal["fr", "nfr"],
    module: str,
    doc_external_id: str,
    source_system: str,
    version: str | None,
    features: list[RequirementFeature],
    rules: list[RequirementRule],
    gaps: list[RequirementGap],
    dead: list[RequirementParseError],
) -> None:
    headers = [_strip_inline(c) for c in _split_row(header_line)]
    if "ID" not in headers or not any("需求" in h for h in headers):
        # FR/NFR 节里的畸形表（无 ID/需求列）：整表死信一次，不逐行噪声
        dead.append(RequirementParseError(
            doc_external_id=doc_external_id,
            line=rows[0][0] if rows else None,
            snippet=header_line.strip()[:120],
            error=f"{kind.upper()} 节表格缺少 ID/需求 列，已整表跳过",
        ))
        gaps.append(RequirementGap(
            kind="malformed_row", subject=module or kind,
            detail="FR/NFR 节存在无法解析的表格（缺 ID/需求 列）",
        ))
        return
    id_col = headers.index("ID")
    req_col = next(i for i, h in enumerate(headers) if "需求" in h)
    pri_col = next(
        (i for i, h in enumerate(headers) if ("优先级" in h or h.strip() == "级")),
        None,
    )
    width = len(headers)
    id_pattern = _FR_CELL_RE if kind == "fr" else _NFR_CELL_RE

    for lineno, raw in rows:
        cells = _split_row(raw)
        raw_id = cells[id_col].strip() if id_col < len(cells) else ""
        hint = _strip_inline(raw_id)
        if len(cells) != width:
            dead.append(RequirementParseError(
                doc_external_id=doc_external_id, line=lineno,
                subject_hint=hint, snippet=raw.strip()[:120],
                error=f"表格列数 {len(cells)} 与表头 {width} 不一致",
            ))
            gaps.append(RequirementGap(
                kind="malformed_row", subject=hint or f"L{lineno}",
                detail=f"表格列数 {len(cells)} 与表头 {width} 不一致",
            ))
            continue
        id_match = id_pattern.match(_strip_inline(raw_id))
        if not id_match:
            dead.append(RequirementParseError(
                doc_external_id=doc_external_id, line=lineno,
                subject_hint=hint, snippet=raw.strip()[:120],
                error=f"ID 列不符合 {kind.upper()} 编号约定",
            ))
            gaps.append(RequirementGap(
                kind="malformed_row", subject=hint or f"L{lineno}",
                detail="ID 列不符合 FR-/NFR- 编号约定",
            ))
            continue
        item_id = id_match.group("id")
        text = _strip_inline(cells[req_col]) if req_col < len(cells) else ""
        if kind == "nfr":
            rules.append(RequirementRule(
                urn=make_urn(source_system, "requirement_rule",
                            f"{doc_external_id}#{item_id}"),
                rule_id=item_id, text=text, source_line=lineno,
            ))
            continue
        cancelled = "~~" in raw_id or "取消" in _strip_inline(raw_id)
        priority = ""
        if pri_col is not None and pri_col < len(cells):
            pri_cell = _strip_inline(cells[pri_col])
            if "取消" in pri_cell:
                cancelled = True
            elif pri_match := _PRIORITY_RE.search(pri_cell):
                priority = pri_match.group(1)
        feature = RequirementFeature(
            urn=make_urn(source_system, "requirement",
                        f"{doc_external_id}#{item_id}"),
            fr_id=item_id, text=text,
            priority="" if cancelled else priority,
            module=module,
            lifecycle="cancelled" if cancelled else "active",
            source_line=lineno,
        )
        features.append(feature)
        if cancelled:
            gaps.append(RequirementGap(
                kind="cancelled_feature", subject=item_id,
                detail=f"需求已取消（删除线/优先级=取消）：{text}",
            ))
        elif not priority:
            gaps.append(RequirementGap(
                kind="priority_missing", subject=item_id,
                detail="活跃需求缺优先级，测试点分级将无据可依",
            ))


# ── 验收标准解析 ────────────────────────────────────────────────────────────

def _parse_acceptance(
    lines: list[tuple[int, str]],
) -> list[RequirementAcceptance]:
    items: list[RequirementAcceptance] = []
    for lineno, raw in lines:
        stripped = raw.strip()
        if not stripped or stripped.startswith("|") or stripped.startswith("#"):
            continue
        # 只认顶格编号：缩进的编号/子弹是当前条目的续行（同用例解析器纪律）
        item = _AC_ITEM_RE.match(stripped)
        if item and not raw[0].isspace():
            items.append(RequirementAcceptance(
                index=len(items), text=_strip_inline(item.group("body")),
                source_line=lineno,
            ))
        elif items:
            items[-1].text = f"{items[-1].text} {_strip_inline(stripped)}"
    return items


# L3-6：AC 句首括号里的 FR 声明（允许全角/半角括号与、，,/ 分隔）
_AC_HEADER_RE = re.compile(r"^\s*[（(](?P<zone>[^）)]*)[）)]")


def _split_ac_fr_refs(text: str) -> tuple[list[str], list[str]]:
    """把 AC 文本里的 FR 引用拆成「句首标题 ID」与「正文提及 ID」两级。

    句首括号（如 ``（FR-3、FR-3a）``）是该条验收的法定归属；其余位置出现的
    FR ID 降为正文提及。NFR-x 不经 :data:`_FR_ID_RE` 产出 FR（L3-5 词边界），
    因此标题里的 NFR 不会污染任一级。
    """
    header = _AC_HEADER_RE.match(text)
    header_ids: list[str] = []
    if header:
        header_ids = list(dict.fromkeys(_FR_ID_RE.findall(header.group("zone"))))
    all_ids = list(dict.fromkeys(_FR_ID_RE.findall(text)))
    secondary = [fid for fid in all_ids if fid not in header_ids]
    return header_ids, secondary


# ── 入口：确定性解析 ────────────────────────────────────────────────────────

def parse_requirement_markdown(
    text: str,
    *,
    doc_external_id: str,
    source_system: str = _REQ_SOURCE_SYSTEM,
    version: str | None = None,
) -> RequirementParseResult:
    """解析一份需求 Markdown；纯函数，不读文件、不写库、不调模型。

    同文档重复解析产生相同 URN（重跑幂等的基础）；验收覆盖只按 AC 文本中
    显式提及的 FR ID 建立确定性链接，语义链接走 :func:`apply_linking`。
    """
    indexed = list(enumerate(text.splitlines(), start=1))
    fenced = _fenced_lines(indexed)
    doc_urn = make_urn(source_system, "requirement_doc", doc_external_id)

    features: list[RequirementFeature] = []
    rules: list[RequirementRule] = []
    gaps: list[RequirementGap] = []
    dead: list[RequirementParseError] = []
    modules: list[str] = []
    acceptance: list[RequirementAcceptance] = []

    section: Literal["fr", "nfr", "ac", "other"] = "other"
    module = ""
    i = 0
    while i < len(indexed):
        lineno, raw = indexed[i]
        if lineno in fenced:
            i += 1
            continue
        stripped = raw.strip()
        if not stripped:
            i += 1
            continue
        if h2 := _H2_RE.match(stripped):
            section = _section_kind(_strip_inline(h2.group("title")))
            module = ""
            i += 1
            continue
        if h3 := _H3_RE.match(stripped):
            if section == "fr":
                module = _strip_inline(h3.group("title"))
                if module not in modules:
                    modules.append(module)
            i += 1
            continue
        # 表格块：表头行 + 分隔行 + 连续 | 行
        if stripped.startswith("|") and i + 1 < len(indexed) \
                and _TABLE_SEP_RE.match(indexed[i + 1][1] or ""):
            header_line = raw
            start = i
            i += 2
            block_rows: list[tuple[int, str]] = []
            while i < len(indexed) and indexed[i][1].strip().startswith("|"):
                if indexed[i][0] not in fenced:
                    block_rows.append(indexed[i])
                i += 1
            if section in ("fr", "nfr"):
                _parse_requirement_table(
                    header_line, block_rows, kind=section, module=module,
                    doc_external_id=doc_external_id, source_system=source_system,
                    version=version, features=features, rules=rules,
                    gaps=gaps, dead=dead,
                )
            # 其他节（风险/元信息）的表整体跳过，不算死信
            continue
        if section == "ac":
            # 收集到下一个 H2 为止；先攒行，统一走编号状态机
            ac_lines: list[tuple[int, str]] = []
            while i < len(indexed):
                next_lineno, next_raw = indexed[i]
                if next_lineno in fenced:
                    i += 1
                    continue
                next_stripped = next_raw.strip()
                if _H2_RE.match(next_stripped):
                    break
                ac_lines.append(indexed[i])
                i += 1
            acceptance = _parse_acceptance(ac_lines)
            continue
        i += 1

    # 确定性链接：AC 文本显式提及且文档确实存在的活跃 FR ID；L3-6 起按句首
    # 标题/正文提及分主、次两级，linked_fr_ids 仍是主+次并集（旧口径不变）
    active_ids = {f.fr_id for f in features if f.lifecycle == "active"}
    for ac in acceptance:
        header_ids, body_ids = _split_ac_fr_refs(ac.text)
        primary = [fid for fid in header_ids if fid in active_ids]
        secondary = [fid for fid in body_ids if fid in active_ids]
        ac.primary_fr_ids = primary
        ac.secondary_fr_ids = secondary
        ac.linked_fr_ids = [*primary, *secondary]

    if not acceptance:
        gaps.append(RequirementGap(
            kind="ac_section_missing", subject=doc_external_id,
            detail="文档缺少「验收标准」节，全部功能点验收口径缺失",
        ))
    covered = {fid for ac in acceptance for fid in ac.linked_fr_ids}
    for feature in features:
        if feature.lifecycle == "active" and feature.fr_id not in covered:
            gaps.append(RequirementGap(
                kind="ac_missing_for_feature", subject=feature.fr_id,
                detail="未在验收标准中找到对应口径（确定性显式 ID 链接未命中）",
            ))

    return RequirementParseResult(
        doc_urn=doc_urn, external_id=doc_external_id,
        features=features, rules=rules, acceptance=acceptance,
        gaps=gaps, dead_letters=dead, modules=modules, version=version,
    )


# ── 模型链接：prompt / schema / 白名单校验 ─────────────────────────────────

def build_linking_messages(
    result: RequirementParseResult,
) -> list[dict[str, str]]:
    """组装 AC↔FR 链接 prompt；只让模型做「配对」，禁止生成任何 prose。"""
    feature_lines = [
        f"- {f.fr_id}（{f.priority or '无优先级'}，{f.module or '未分模块'}）：{f.text}"
        for f in result.active_features
    ]
    ac_lines = [
        f"{ac.index}. {ac.text}" for ac in result.acceptance
    ]
    user = (
        "下面是一份需求文档抽取结果。请为每条验收标准（ac_index）链接它所验收的"
        "功能点 FR ID。\n\n【功能点】\n" + "\n".join(feature_lines)
        + "\n\n【验收标准】\n" + "\n".join(ac_lines)
        + "\n\n规则：只能引用上面列出的 FR ID 与 ac_index，禁止发明新 ID；"
        "一条 AC 可链接多个 FR；没有把握的 FR 不要链接（缺失会被显式标缺，不许凑数）。"
    )
    return [
        {"role": "system", "content": (
            "你是测试设计助手的需求链接节点，只输出 JSON 链接关系，"
            "不输出解释、不生成验收文本。"
        )},
        {"role": "user", "content": user},
    ]


def linking_json_schema() -> dict[str, object]:
    """Ollama ``format=`` 可直接消费的 JSON Schema（structured 信封同问答链路）。"""
    return {
        "type": "object",
        "properties": {
            "links": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "ac_index": {"type": "integer"},
                        "fr_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "required": ["ac_index", "fr_ids"],
                },
            },
        },
        "required": ["links"],
    }


def apply_linking(
    result: RequirementParseResult,
    payload: dict[str, object],
) -> RequirementParseResult:
    """把模型链接载荷合并进解析结果；白名单校验失败整包拒绝（不部分接受）。

    模型只能选择源文档里存在的 AC 序号与活跃 FR ID；任何越界/未知项都按
    幻觉处理，抛 :class:`SkeletonValidationError` 由调用方走降级路径。
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("links"), list):
        raise SkeletonValidationError("链接载荷缺少 links 数组")
    active_ids = {f.fr_id for f in result.active_features}
    ac_count = len(result.acceptance)
    merged: dict[int, set[str]] = {
        ac.index: set(ac.linked_fr_ids) for ac in result.acceptance
    }
    for raw_link in payload["links"]:
        if not isinstance(raw_link, dict):
            raise SkeletonValidationError(f"链接项不是对象: {raw_link!r}")
        ac_index = raw_link.get("ac_index")
        fr_ids = raw_link.get("fr_ids")
        if not isinstance(ac_index, int) or isinstance(ac_index, bool) \
                or not (0 <= ac_index < ac_count):
            raise SkeletonValidationError(f"ac_index 越界或非整数: {ac_index!r}")
        if not isinstance(fr_ids, list) or not all(isinstance(x, str) for x in fr_ids):
            raise SkeletonValidationError(f"fr_ids 必须是字符串数组: {fr_ids!r}")
        unknown = [fid for fid in fr_ids if fid not in active_ids]
        if unknown:
            raise SkeletonValidationError(f"模型引用了文档不存在的 FR ID: {unknown}")
        merged[ac_index].update(fr_ids)

    new_acceptance = [
        ac.model_copy(update={"linked_fr_ids": sorted(merged[ac.index])})
        for ac in result.acceptance
    ]
    covered = {fid for ac in new_acceptance for fid in ac.linked_fr_ids}
    structural_gaps = [
        g for g in result.gaps if g.kind != "ac_missing_for_feature"
    ]
    for feature in result.active_features:
        if feature.fr_id not in covered:
            structural_gaps.append(RequirementGap(
                kind="ac_missing_for_feature", subject=feature.fr_id,
                detail="未在验收标准中找到对应口径（确定性+模型链接均未命中）",
            ))
    return result.model_copy(update={
        "acceptance": new_acceptance,
        "gaps": structural_gaps,
        "linking_source": "model",
    })
