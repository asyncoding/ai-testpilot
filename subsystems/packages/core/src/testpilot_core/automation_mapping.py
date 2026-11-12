"""automation-map-v1 自动化身份映射纯函数（docs/12 §3.3，切片 E3）。

零 IO、零三方（仅 pydantic + 标准库）：吃 ``{仓库相对路径: Swift 文本}`` 与
活跃用例 ID 集，产出「TC 用例 ↔ Swift 测试方法」绑定、未链接测试、未映射
用例与扫描警告，供 worker 落盘 automation-map-v1。

口径（docs/12 §3.2/§3.3）：

- 标记只认行首空白后独占一行的 ``// testpilot-case: <TC-ID>``；标记挂在它
  下方最近的 ``func test...`` 方法上，遇 class 声明清空悬挂标记；
- 正则只识别封闭方法签名（``func test\\w+\\(``），不引 Swift 解析器；
- target 按目录映射：路径含 ``UITests`` → UI target，含 ``Tests`` → 单元 target，
  其余目录的 swift 文件不视为测试文件；
- 未知 TC 标记 / 悬空 manual 绑定 / 非法 only-testing 形态 → ``AutomationMapError``
  fail-closed（worker 映射 exit 2，不产出有效绑定）；
- 同一 TC 出现在多个方法是 docs/12 §3.3 允许的一对多，但产生警告（worker exit 3）
  催人审；exploratory 用例不进自动化集，出现标记同样警告并剔除绑定。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel, Field

from .case_export import DEFAULT_TEST_TARGET, DEFAULT_UI_TEST_TARGET

AUTOMATION_MAP_SCHEMA_VERSION = "automation-map-v1"

# 唯一在 Xcode workspace 内的工程根（AlarmKitSpike.xcworkspace）。仓里根 Tests/
# 属于 2026-08 已作废的 CalendarStrongReminder SPM 包（根 project.yml 同为作废
# 规格），其测试不被 workspace 编译，OID 传给 xcodebuild 会 suite-not-found。
# E5 实测：旧口径把根 Tests/CalendarStrongReminderTests 的 12 个方法误纳进映射。
PROJECT_ROOT_SEGMENT = "AlarmKitSpike"

_CLASS_RE = re.compile(r"\bclass\s+(?P<name>[A-Za-z_]\w*)\b")
_FUNC_RE = re.compile(r"\bfunc\s+(?P<name>test[A-Za-z0-9_]\w*)\s*\(")
_MARKER_RE = re.compile(r"^\s*//\s*testpilot-case:\s*(?P<tc>[A-Za-z0-9_-]+)\s*$")
_MARKER_HINT_RE = re.compile(r"testpilot-case\s*:", re.IGNORECASE)
_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z_]\w*/test[A-Za-z0-9_]\w*$")


class AutomationMapError(ValueError):
    """扫描硬错误（未知 TC/悬空绑定/非法标识符），worker 映射 exit 2。"""


class Binding(BaseModel):
    case_external_id: str
    only_testing_id: str
    source_file: str
    line: int
    origin: str  # "marker" | "manual"
    reviewed_by: str = ""
    reviewed_at: str | None = None


class UnlinkedTest(BaseModel):
    only_testing_id: str
    source_file: str
    line: int


class ManualBindingInput(BaseModel):
    """人审对无标记存量测试的手工绑定（编辑 map 或 CLI --bind 输入）。"""

    case_external_id: str
    only_testing_id: str
    reviewed_by: str
    reviewed_at: str


class ScanResult(BaseModel):
    bindings: list[Binding] = Field(default_factory=list)
    unlinked_swift_tests: list[UnlinkedTest] = Field(default_factory=list)
    unmapped_active_cases: list[str] = Field(default_factory=list)
    scan_warnings: list[str] = Field(default_factory=list)

    def mapped_case_ids(self) -> set[str]:
        return {b.case_external_id for b in self.bindings}


def target_for_path(
    path: str, *,
    unit_target: str = DEFAULT_TEST_TARGET,
    ui_target: str = DEFAULT_UI_TEST_TARGET,
    project_root: str = PROJECT_ROOT_SEGMENT,
) -> str | None:
    """目录 → Xcode target；非 workspace 工程内的路径返回 None（调用方跳过）。

    两道 containment：①路径必须含工程根段（AlarmKitSpike），挡掉仓里根 Tests/
    的作废 SPM 包；②工程根内再按 Tests/UITests 目录段分 target。
    """
    parts = path.replace("\\", "/").split("/")
    if project_root not in parts:
        return None
    if any(part == "UITests" or part.endswith("UITests") for part in parts):
        return ui_target
    if any(part == "Tests" or part.endswith("Tests") for part in parts):
        return unit_target
    return None


def _only_testing_id(target: str, class_name: str, method_name: str) -> str:
    return f"{target}/{class_name}/{method_name}"


def scan_swift_tests(
    files: Mapping[str, str],
    active_case_ids: Sequence[str],
    *,
    exploratory_ids: Sequence[str] = (),
    unit_target: str = DEFAULT_TEST_TARGET,
    ui_target: str = DEFAULT_UI_TEST_TARGET,
    manual_bindings: Sequence[ManualBindingInput] = (),
) -> ScanResult:
    """扫描 Swift 文件字典，构建 marker 绑定并校验人工绑定（纯函数）。"""
    active_set = set(active_case_ids)
    exploratory_set = set(exploratory_ids)
    errors: list[str] = []
    warnings: list[str] = []

    # 全量方法台账：only_testing_id → (file, line)，manual 绑定存在性校验依据，
    # 也是最终计算 unlinked（全量方法 − 已有效绑定方法）的全集。
    method_index: dict[str, tuple[str, int]] = {}
    marker_pairs: list[tuple[str, str, int, str]] = []  # (tc, oid, line, file)

    for path in sorted(files):
        target = target_for_path(path, unit_target=unit_target, ui_target=ui_target)
        if target is None:
            continue
        current_class = ""
        pending_markers: list[str] = []
        for lineno, line in enumerate(files[path].splitlines(), start=1):
            class_match = _CLASS_RE.search(line)
            if class_match:
                current_class = class_match.group("name")
                pending_markers = []  # 标记只能挂方法，挂 class 的悬空标记作废
                continue
            marker_match = _MARKER_RE.match(line)
            if marker_match:
                pending_markers.append(marker_match.group("tc"))
                continue
            if _MARKER_HINT_RE.search(line) and not marker_match:
                errors.append(f"{path}:{lineno} 畸形 testpilot-case 标记: {line.strip()}")
                continue
            func_match = _FUNC_RE.search(line)
            if not func_match or not current_class:
                continue
            method_name = func_match.group("name")
            oid = _only_testing_id(target, current_class, method_name)
            method_index[oid] = (path, lineno)
            for tc in pending_markers:
                marker_pairs.append((tc, oid, lineno, path))
            pending_markers = []

    bindings: list[Binding] = []
    marker_locations: dict[str, list[str]] = {}

    # ── marker 绑定：未知 TC 硬错误；exploratory 剔除+警告；一对多警告 ──
    for tc, oid, line, path in marker_pairs:
        if tc not in active_set:
            errors.append(
                f"{path}:{line} 标记引用了不存在或非活跃的用例 {tc}（oid={oid}）")
            continue
        if tc in exploratory_set:
            warnings.append(
                f"探索性用例 {tc} 不应生成自动化绑定，已剔除: {path}:{line}")
            continue
        bindings.append(Binding(
            case_external_id=tc, only_testing_id=oid,
            source_file=path, line=line, origin="marker"))
        marker_locations.setdefault(tc, []).append(f"{path}:{line}")

    for tc, locations in sorted(marker_locations.items()):
        if len(locations) > 1:
            warnings.append(f"重复标记: {tc} 同时出现于 {', '.join(locations)}")

    # ── manual 绑定：标识符形态 + 方法真实存在 + TC 活跃/非探索性，全量校验 ──
    # 去重键含 origin：重扫时旧 map manual 与 CLI 新输入可能指向同一方法。
    seen_binding_keys = {
        (b.case_external_id, b.only_testing_id, b.origin) for b in bindings
    }
    for manual in manual_bindings:
        if not _ID_RE.match(manual.only_testing_id):
            errors.append(
                f"manual 绑定标识符非法（应为 Target/Class/method）: "
                f"{manual.case_external_id} -> {manual.only_testing_id}")
            continue
        if manual.only_testing_id not in method_index:
            errors.append(
                f"manual 绑定指向已不存在的 Swift 方法: "
                f"{manual.case_external_id} -> {manual.only_testing_id}")
            continue
        if manual.case_external_id not in active_set:
            errors.append(
                f"manual 绑定引用了不存在或非活跃的用例: {manual.case_external_id}")
            continue
        if manual.case_external_id in exploratory_set:
            warnings.append(
                f"探索性用例 {manual.case_external_id} 的 manual 绑定已剔除")
            continue
        dedup_key = (manual.case_external_id, manual.only_testing_id, "manual")
        if dedup_key in seen_binding_keys:
            continue
        seen_binding_keys.add(dedup_key)
        path, line = method_index[manual.only_testing_id]
        bindings.append(Binding(
            case_external_id=manual.case_external_id,
            only_testing_id=manual.only_testing_id,
            source_file=path, line=line, origin="manual",
            reviewed_by=manual.reviewed_by, reviewed_at=manual.reviewed_at))

    if errors:
        raise AutomationMapError("; ".join(errors))

    # 稳定排序：同输入同输出，map hash 可复现
    bindings.sort(key=lambda b: (b.case_external_id, b.only_testing_id))
    # unlinked = 全量测试方法 − 已有有效绑定的方法（marker 被剔除/manual 接管
    # 的方法都自动归入正确一侧，不在扫描期提前分流）。
    bound_oids = {b.only_testing_id for b in bindings}
    unlinked = [
        UnlinkedTest(only_testing_id=oid, source_file=path, line=line)
        for oid, (path, line) in sorted(method_index.items())
        if oid not in bound_oids
    ]
    mapped = {b.case_external_id for b in bindings}
    unmapped = sorted(active_set - mapped - exploratory_set)

    return ScanResult(
        bindings=bindings,
        unlinked_swift_tests=unlinked,
        unmapped_active_cases=unmapped,
        scan_warnings=sorted(warnings),
    )


def build_automation_map(
    result: ScanResult, *,
    repo_path: str, branch: str, head_commit: str,
    generated_at: str,
) -> dict[str, Any]:
    """ScanResult + 仓库坐标 → automation-map-v1 载荷 dict（含内容 hash）。"""
    payload: dict[str, Any] = {
        "map_schema_version": AUTOMATION_MAP_SCHEMA_VERSION,
        "generated_at": generated_at,
        "repo_path": repo_path,
        "branch": branch,
        "head_commit": head_commit,
        "bindings": [b.model_dump() for b in result.bindings],
        "unlinked_swift_tests": [u.model_dump() for u in result.unlinked_swift_tests],
        "unmapped_active_cases": list(result.unmapped_active_cases),
        "scan_warnings": list(result.scan_warnings),
    }
    payload["map_hash"] = automation_map_hash(payload)
    return payload


def automation_map_hash(payload: Mapping[str, Any]) -> str:
    """内容 hash：剔除 generated_at/map_hash（head_commit 保留——换 HEAD 必须变 hash）。"""
    normalized = {
        k: v for k, v in payload.items()
        if k not in ("generated_at", "map_hash")
    }
    blob = json.dumps(
        normalized, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()
