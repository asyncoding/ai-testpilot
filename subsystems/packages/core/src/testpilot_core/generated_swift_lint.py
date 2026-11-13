"""AgentGenerated Swift 静态红线扫描纯函数（docs/15 §9、docs/12 UI 分支规则，H5）。

零 IO、零三方：只吃文件相对路径 + 文本，产出结构化违规列表，全部判定可在
hostless 测试里红证（植入违规必红、回退转绿）。两条红线：

- **GS-001 固定 sleep 死等**：生成代码禁止 ``sleep(`` / ``usleep(`` /
  ``Thread.sleep``，转场一律 ``waitForExistence(timeout:)`` 轮询。固定时长
  在 CI 负载波动下要么假绿（等太短）要么白等，是 UI 自动化脆弱性首因；
- **GS-002/003 冷启动毁现场**：接力（relay）类用例附着已有进程必须
  ``app.activate()``，同一测试方法里 activate 与 launch 并存（GS-002）、
  或一个测试方法出现两次以上 ``.launch(``（GS-003）都判违规——冷启动会
  销毁系统接力场景，且让用例间状态累积，破坏干净启动契约。

作用域硬约束：**只扫路径含 ``AgentGenerated`` 段的文件**。语料仓存量测试
不受这套生成契约管辖，圈外路径直接拒绝（fail-closed），防止红线被误用于
人工代码引发大面积假红。注释与字符串（含多行字符串）先做遮罩再匹配，
"// 这里不许写 sleep(2)" 这类文档说明不构成违规。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

AGENT_GENERATED_MARKER = "AgentGenerated"

# 裸 sleep( / usleep(（词边界，避免误伤含 sleep 子串的标识符）；
# 负向后顾排除 ".sleep("——那是 Thread.sleep 的领地，由专用规则捕获，禁止双计
_BARE_SLEEP_RE = re.compile(r"(?<![\w.])u?sleep\s*\(")
_THREAD_SLEEP_RE = re.compile(r"\bThread\s*\.\s*sleep\b")
_FUNC_TEST_RE = re.compile(r"\bfunc\s+(test\w+)\s*\([^)]*\)")
_LAUNCH_RE = re.compile(r"\.launch\s*\(")
_ACTIVATE_RE = re.compile(r"\.activate\s*\(")


class GeneratedSwiftLintError(ValueError):
    """扫描器输入/作用域不合法（圈外路径等），与发现违规的返回值语义区分。"""


@dataclass(frozen=True)
class LintViolation:
    rule_id: str
    path: str
    line: int
    message: str
    snippet: str


def assert_agent_generated_path(rel_path: str) -> None:
    """作用域守卫：只有 AgentGenerated 目录内的 swift 文件受红线管辖。"""
    if AGENT_GENERATED_MARKER not in rel_path.split("/"):
        raise GeneratedSwiftLintError(
            f"静态红线只作用于 {AGENT_GENERATED_MARKER}/ 内的生成文件，"
            f"圈外路径拒绝扫描: {rel_path!r}")
    if not rel_path.endswith(".swift"):
        raise GeneratedSwiftLintError(f"只扫描 .swift 文件: {rel_path!r}")


def mask_comments_and_strings(text: str) -> str:
    """把注释与字符串内容替换为等长空白（保留换行），行列号不漂移。

    手写小状态机而非正则：Swift 的 ``//``、``/* */``、``"..."``、
    多行 ``\"\"\"...\"\"\"`` 会互相嵌套伪装（字符串里的 //、注释里的引号），
    单行正则必然漏判或误判。转义引号 ``\\"`` 在字符串态内跳过。
    """
    out: list[str] = []
    i, n = 0, len(text)
    state = "code"  # code | line_comment | block_comment | string | multiline
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if state == "code":
            if ch == "/" and nxt == "/":
                state, out, i = "line_comment", out + [" ", " "], i + 2
                continue
            if ch == "/" and nxt == "*":
                state, out, i = "block_comment", out + [" ", " "], i + 2
                continue
            if text[i:i + 3] == '"""':
                state, out, i = "multiline", out + [" ", " ", " "], i + 3
                continue
            if ch == '"':
                state, out, i = "string", out + [" "], i + 1
                continue
            out.append(ch)
            i += 1
        elif state == "line_comment":
            if ch == "\n":
                state = "code"
                out.append("\n")
            else:
                out.append(" ")
            i += 1
        elif state == "block_comment":
            if ch == "*" and nxt == "/":
                state, out, i = "code", out + [" ", " "], i + 2
                continue
            out.append("\n" if ch == "\n" else " ")
            i += 1
        elif state == "string":
            if ch == "\\" and nxt:
                # 转义对整体遮罩，\" 不会提前闭合字符串
                out.extend([" ", " "])
                i += 2
                continue
            if ch == '"':
                state = "code"
            out.append("\n" if ch == "\n" else " ")
            i += 1
        else:  # multiline
            if text[i:i + 3] == '"""':
                state, out, i = "code", out + [" ", " ", " "], i + 3
                continue
            out.append("\n" if ch == "\n" else " ")
            i += 1
    return "".join(out)


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _snippet(text: str, offset: int) -> str:
    line_start = text.rfind("\n", 0, offset) + 1
    line_end = text.find("\n", offset)
    if line_end == -1:
        line_end = len(text)
    return text[line_start:line_end].strip()[:160]


def _sleep_violations(rel_path: str, text: str,
                      masked: str) -> list[LintViolation]:
    violations: list[LintViolation] = []
    for match in _BARE_SLEEP_RE.finditer(masked):
        violations.append(LintViolation(
            "GS-001", rel_path, _line_of(masked, match.start()),
            "固定 sleep/usleep 死等：转场等待必须用 waitForExistence(timeout:) 轮询",
            _snippet(text, match.start())))
    for match in _THREAD_SLEEP_RE.finditer(masked):
        violations.append(LintViolation(
            "GS-001", rel_path, _line_of(masked, match.start()),
            "Thread.sleep 固定死等：转场等待必须用 waitForExistence(timeout:) 轮询",
            _snippet(text, match.start())))
    return violations


def _method_body_spans(masked: str) -> list[tuple[str, int, int]]:
    """大括号配平提取每个 test 方法体 (name, body_start, body_end)。

    输入已遮罩字符串/注释，大括号只反映真实语法结构；配不平的残缺文件
    返回能闭合的方法并让调用方按已解析内容判（红线宁早勿晚）。
    """
    spans: list[tuple[str, int, int]] = []
    for match in _FUNC_TEST_RE.finditer(masked):
        brace = masked.find("{", match.end())
        if brace == -1:
            continue
        depth = 0
        for idx in range(brace, len(masked)):
            if masked[idx] == "{":
                depth += 1
            elif masked[idx] == "}":
                depth -= 1
                if depth == 0:
                    spans.append((match.group(1), brace + 1, idx))
                    break
    return spans


def _launch_violations(rel_path: str, text: str,
                       masked: str) -> list[LintViolation]:
    violations: list[LintViolation] = []
    for name, start, end in _method_body_spans(masked):
        body = masked[start:end]
        launches = list(_LAUNCH_RE.finditer(body))
        activates = list(_ACTIVATE_RE.finditer(body))
        if activates and launches:
            offset = start + launches[0].start()
            violations.append(LintViolation(
                "GS-002", rel_path, _line_of(masked, offset),
                f"测试方法 {name} 同时使用 activate 与 launch：接力段只能 "
                "app.activate() 附着，launch() 冷启动会销毁系统接力场景",
                _snippet(text, offset)))
        if len(launches) > 1:
            offset = start + launches[1].start()
            violations.append(LintViolation(
                "GS-003", rel_path, _line_of(masked, offset),
                f"测试方法 {name} 出现 {len(launches)} 次 launch()："
                "每用例只允许一次冷启动（setUp/首次），重复启动累积状态",
                _snippet(text, offset)))
    return violations


def scan_generated_swift(rel_path: str, text: str) -> list[LintViolation]:
    """扫描单个 AgentGenerated Swift 文件，违规按行号排序返回。"""
    assert_agent_generated_path(rel_path)
    masked = mask_comments_and_strings(text)
    violations = _sleep_violations(rel_path, text, masked)
    violations.extend(_launch_violations(rel_path, text, masked))
    return sorted(violations, key=lambda v: (v.line, v.rule_id))


def scan_agent_generated_files(
        files: dict[str, str]) -> list[LintViolation]:
    """批量扫描（相对路径 → 文本）；任一文件圈外即 fail-closed 拒绝整批。"""
    violations: list[LintViolation] = []
    for rel_path in sorted(files):
        violations.extend(scan_generated_swift(rel_path, files[rel_path]))
    return violations
