#!/usr/bin/env python3
"""E6 双切块器边界对比（docs/08 FR-ING-002 AC3，离线部分）。

对 profile code_roots 内全部 Swift 文件，跑 core 词法器（lexical-swift-1）与
adapters tree-sitter 切块器（tree-sitter-swift-1），以 `xcrun swiftc -dump-ast`
的声明范围作为边界真值（SourceKit 系输出，docs/08 选型注记允许的 macOS 校准手段），
输出：
  - 符号召回（chunker 命中的非隐式 GT 声明占比，按 起始行+选择器名 连接）；
  - 边界精确率（命中符号的结束行是否与 GT 一致）；
  - 各自独有符号清单（词法盲区 / AST 多出项分开列）；
  - tree-sitter ERROR 恢复计数与 dump-ast 不可用文件。

检索侧指标（Recall@K/引用准确率）需先建 tree-sitter 双索引再走 eval_probe，
本脚本不触网、不调模型，报告落 out/eval/chunker-comparison.{json,md}（gitignore）。

用法（subsystems/ 下）：
  .venv/bin/python scripts/chunker_compare.py
  .venv/bin/python scripts/chunker_compare.py --profile config/profiles/ios-local-poc.yaml
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname

SUBSYSTEMS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SUBSYSTEMS / "packages" / "workers" / "src"))
sys.path.insert(0, str(SUBSYSTEMS / "packages" / "orchestrator" / "src"))

# dump-ast 节点头：(func_decl [implicit] ... range=[/path:2:3 - line:2:21] "bar(a:)" attr="x" ...
# 声明名是 range 之后第一个引号串；attr="..." 的引号前紧贴 '='，用负向后顾排除
_AST_HEADER_RE = re.compile(
    r"^\s*\((?P<kind>[a-z_]+_decl)\b.*?"
    r'range=\[[^:]+:(?P<sl>\d+):\d+(?:\s*-\s*line:(?P<el>\d+):(?P<ec>\d+))?\]'
    r"(?P<tail>.*)$"
)
_AST_NAME_RE = re.compile(r'(?<!=)"([^"]+)"')
_GT_KINDS = frozenset({
    "class_decl", "struct_decl", "enum_decl", "actor_decl",
    "protocol_decl", "extension_decl", "func_decl", "constructor_decl",
    "destructor_decl", "subscript_decl",
})
_REPORT_DIR = SUBSYSTEMS / "out" / "eval"


def parse_ast_ground_truth(ast_text: str) -> list[dict]:
    """解析 swiftc -dump-ast 输出为非隐式声明列表（起始行/结束行/选择器名）。"""
    out: list[dict] = []
    for line in ast_text.splitlines():
        m = _AST_HEADER_RE.match(line)
        if not m or m.group("kind") not in _GT_KINDS:
            continue
        # 编译器合成的隐式 init/deinit 不在源码里，切块器不该切，排除
        if "implicit" in line:
            continue
        sl = int(m.group("sl"))
        # 实测 swiftc range 末端指向闭合括号本身所在行列（如 '}' 在 127 行 col 1 →
        # line:127:1），故结束行直接取 el，不做 col==1 回退（回退会把 GT 整体上移一行）
        end_line = int(m.group("el")) if m.group("el") else sl
        name_match = _AST_NAME_RE.search(m.group("tail"))
        out.append({
            "kind": m.group("kind"),
            "start_line": sl,
            "end_line": end_line,
            "name": name_match.group(1) if name_match else "",
        })
    return out


def _anchor_key(anchor: str) -> str:
    """anchor → 可与 GT 名比对的选择器键：去 #L 去歧、去类型前缀。"""
    base = anchor.split("#L", 1)[0]
    return base.rsplit(".", 1)[-1] if "." in base else base


def _ios_sim_sdk() -> str:
    # 仓内 9 个文件整体包在 #if canImport(UIKit) 中：macOS target 下条件编译把
    # 正文整段跳过（AST 只剩 import），必须按 iOS 模拟器 target 解析才能拿到真值
    proc = subprocess.run(
        ["xcrun", "--show-sdk-path", "--sdk", "iphonesimulator"],
        capture_output=True, text=True, timeout=60, check=True)
    return proc.stdout.strip()


def run_dump_ast(path: Path, sdk: str) -> tuple[str | None, str]:
    proc = subprocess.run(
        ["xcrun", "swiftc", "-target", "arm64-apple-ios17.0-simulator",
         "-sdk", sdk, "-dump-ast", str(path)],
        capture_output=True, text=True, timeout=120,
    )
    # 模块缺失等语义错误不影响语法 AST：有输出就用，stderr 留给报告计数
    return (proc.stdout or None), proc.stderr


def _join_gt(decl: dict, chunks: list) -> tuple:
    """两趟连接：先 (选择器名, 精确起始行)，再同名 ±5 行容差。

    切块器把属性（@MainActor/@main）与文档注释行计入块起点，GT range 从关键字
    行起算，二者系统性相差 1~数行；容差命中不算漏报，单列为起始行偏移并记 delta。
    返回 (chunk|None, 命中模式, delta)。
    """
    exact = {(_anchor_key(c.anchor), c.start_line): c for c in chunks}
    hit = exact.get((decl["name"], decl["start_line"]))
    if hit is not None:
        return hit, "exact", 0
    same_name = [c for c in chunks if _anchor_key(c.anchor) == decl["name"]]
    for c in same_name:
        if abs(c.start_line - decl["start_line"]) <= 5:
            return c, "offset", c.start_line - decl["start_line"]
    # 第三趟：同位置（±5 行）存在异名块 → 非真漏，是命名口径差（core _selector 吸入
    # 返回元组标签、dotted 扩展只取首段等，两切块器共享，非边界变量）
    near = [c for c in chunks if abs(c.start_line - decl["start_line"]) <= 5]
    if near:
        return near[0], "artifact", None
    return None, "miss", None


def compare_file(path: Path, text: str, lexical, tree_chunker, sdk: str) -> dict:
    lex_chunks = lexical(text)
    ts_chunks = tree_chunker.chunk(text)
    ast_text, stderr = run_dump_ast(path, sdk)
    gt = parse_ast_ground_truth(ast_text) if ast_text else []

    per_file = {
        "file": str(path),
        "lexical_symbols": len(lex_chunks),
        "tree_sitter_symbols": len(ts_chunks),
        "ground_truth_decls": len(gt),
        "ast_available": bool(gt),
        "ast_stderr_head": stderr.splitlines()[0][:160] if stderr else "",
        "error_node_count": tree_chunker.parse_info.error_node_count
        if tree_chunker.parse_info else 0,
        "lexical_only": [],
        "tree_sitter_only": [],
        "gt_misses": {"lexical": [], "tree_sitter": []},
        "naming_artifacts": {"lexical": [], "tree_sitter": []},
        "start_offsets": {"lexical": [], "tree_sitter": []},
        "boundary_mismatches": {"lexical": [], "tree_sitter": []},
        "matched": {"lexical": 0, "tree_sitter": 0},
        "offset_matched": {"lexical": 0, "tree_sitter": 0},
        "artifact_count": {"lexical": 0, "tree_sitter": 0},
    }

    lex_anchor_set = {c.anchor for c in lex_chunks}
    ts_anchor_set = {c.anchor for c in ts_chunks}
    per_file["lexical_only"] = sorted(lex_anchor_set - ts_anchor_set)
    per_file["tree_sitter_only"] = sorted(ts_anchor_set - lex_anchor_set)

    for decl in gt:
        for side, chunks, bucket in (
            ("lexical", lex_chunks, "lexical"),
            ("tree_sitter", ts_chunks, "tree_sitter"),
        ):
            chunk, mode, delta = _join_gt(decl, chunks)
            if chunk is None:
                per_file["gt_misses"][bucket].append(decl)
                continue
            if mode == "artifact":
                per_file["artifact_count"][side] += 1
                per_file["naming_artifacts"][bucket].append({
                    "gt_name": decl["name"], "start_line": decl["start_line"],
                    "chunk_anchor": chunk.anchor})
                continue
            if mode == "offset":
                per_file["offset_matched"][side] += 1
                per_file["start_offsets"][bucket].append({
                    "name": decl["name"], "gt_start_line": decl["start_line"],
                    "chunker_start_line": decl["start_line"] + delta, "delta": delta})
            per_file["matched"][side] += 1
            if chunk.end_line != decl["end_line"]:
                per_file["boundary_mismatches"][bucket].append({
                    "name": decl["name"], "start_line": decl["start_line"],
                    "gt_end_line": decl["end_line"],
                    "chunker_end_line": chunk.end_line,
                })
    return per_file


def aggregate(files: list[dict]) -> dict:
    def sum_(key):
        return sum(f[key] for f in files)

    usable = [f for f in files if f["ast_available"]]
    agg = {
        "files_total": len(files),
        "files_with_ast_truth": len(usable),
        "lexical_symbols": sum_("lexical_symbols"),
        "tree_sitter_symbols": sum_("tree_sitter_symbols"),
        "ground_truth_decls": sum_("ground_truth_decls"),
        "tree_sitter_error_nodes": sum_("error_node_count"),
        "lexical_only_total": sum(len(f["lexical_only"]) for f in files),
        "tree_sitter_only_total": sum(len(f["tree_sitter_only"]) for f in files),
    }
    for side in ("lexical", "tree_sitter"):
        matched = sum(f["matched"][side] for f in usable)
        offset_matched = sum(f["offset_matched"][side] for f in usable)
        gt_total = sum(f["ground_truth_decls"] for f in usable)
        boundary_miss = sum(len(f["boundary_mismatches"][side]) for f in usable)
        agg[f"{side}_gt_recall"] = round(matched / gt_total, 4) if gt_total else None
        agg[f"{side}_boundary_exact"] = (
            round((matched - boundary_miss) / matched, 4) if matched else None)
        agg[f"{side}_gt_misses"] = sum(len(f["gt_misses"][side]) for f in usable)
        agg[f"{side}_naming_artifacts"] = sum(f["artifact_count"][side] for f in usable)
        agg[f"{side}_boundary_mismatches"] = boundary_miss
        agg[f"{side}_offset_matched"] = offset_matched
        deltas = [o["delta"] for f in usable for o in f["start_offsets"][side]]
        agg[f"{side}_offset_delta_dist"] = dict(sorted(Counter(deltas).items()))
    return agg


def render_markdown(agg: dict, files: list[dict]) -> str:
    lines = [
        "# E6 双切块器边界对比（FR-ING-002 AC3）",
        "",
        f"- 扫描文件：{agg['files_total']}（其中 {agg['files_with_ast_truth']} 个拿到 "
        "swiftc -dump-ast 真值；模块缺失只报语义错误不影响语法树）",
        f"- GT 非隐式声明：{agg['ground_truth_decls']}；"
        f"词法器符号 {agg['lexical_symbols']} / tree-sitter 符号 {agg['tree_sitter_symbols']}",
        f"- tree-sitter ERROR 恢复节点总数：{agg['tree_sitter_error_nodes']}",
        "",
        "## 对照真值的指标",
        "",
        "连接口径：先按（选择器名, 精确起始行）连接；块起点含属性/文档注释而 GT 从关键字",
        "行起算的系统性偏移，用同名 ±5 行容差二次连接（单列为起始行偏移，不计漏报）。",
        "",
        "| 指标 | 词法器 lexical-swift-1 | tree-sitter-swift-1 |",
        "|---|---|---|",
        f"| GT 符号召回（精确+容差） | {agg['lexical_gt_recall']} | {agg['tree_sitter_gt_recall']} |",
        f"| 其中容差命中（起始行偏移） | {agg['lexical_offset_matched']} | "
        f"{agg['tree_sitter_offset_matched']} |",
        f"| 起始行 delta 分布 | {agg['lexical_offset_delta_dist']} | "
        f"{agg['tree_sitter_offset_delta_dist']} |",
        f"| 边界精确率（结束行一致） | {agg['lexical_boundary_exact']} | "
        f"{agg['tree_sitter_boundary_exact']} |",
        f"| 命名口径差（同位置异名块，非漏报） | {agg['lexical_naming_artifacts']} | "
        f"{agg['tree_sitter_naming_artifacts']} |",
        f"| 真漏报数（同位置 ±5 行无任何块） | {agg['lexical_gt_misses']} | "
        f"{agg['tree_sitter_gt_misses']} |",
        f"| 结束行不一致数 | {agg['lexical_boundary_mismatches']} | "
        f"{agg['tree_sitter_boundary_mismatches']} |",
        "",
        f"对账：GT {agg['ground_truth_decls']} = 命中（精确+容差）+ 命名口径差 + 真漏报；"
        f"tree-sitter ERROR 节点 {agg['tree_sitter_error_nodes']} 个。",
        "",
        f"独有符号：词法器 {agg['lexical_only_total']} / tree-sitter {agg['tree_sitter_only_total']}",
        "",
        "## 词法器真漏报（同位置 ±5 行无任何块，按文件）",
        "",
    ]
    for f in files:
        misses = [f"{d['name']}@L{d['start_line']}" for d in f["gt_misses"]["lexical"]]
        if misses:
            lines.append(f"- `{Path(f['file']).name}`：{', '.join(misses)}")
    lines += ["", "## tree-sitter 真漏报（按文件）", ""]
    for f in files:
        misses = [f"{d['name']}@L{d['start_line']}" for d in f["gt_misses"]["tree_sitter"]]
        if misses:
            lines.append(f"- `{Path(f['file']).name}`：{', '.join(misses)}")
    lines += ["", "## 命名口径差明细（GT 名 → 实际 anchor；两切块器共享，非边界变量）", ""]
    seen = set()
    for f in files:
        for a in f["naming_artifacts"]["tree_sitter"]:
            key = (Path(f["file"]).name, a["gt_name"], a["chunk_anchor"])
            if key in seen:
                continue
            seen.add(key)
            lines.append(f"- `{Path(f['file']).name}`：GT `{a['gt_name']}` → "
                         f"anchor `{a['chunk_anchor']}`（L{a['start_line']}）")
    lines += ["", "## tree-sitter 独有符号（词法盲区候选）", ""]
    for f in files:
        if f["tree_sitter_only"]:
            lines.append(f"- `{Path(f['file']).name}`：{', '.join(f['tree_sitter_only'])}")
    lines += ["", "## 词法器独有符号（AST 多出/口径差异候选）", ""]
    for f in files:
        if f["lexical_only"]:
            lines.append(f"- `{Path(f['file']).name}`：{', '.join(f['lexical_only'])}")
    no_ast = [Path(f["file"]).name for f in files if not f["ast_available"]]
    if no_ast:
        lines += ["", f"## dump-ast 不可用文件（{len(no_ast)}，未计入 GT 指标）", ""]
        lines += [f"- {n}" for n in no_ast]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="E6 双切块器边界对比")
    parser.add_argument(
        "--profile",
        default=str(SUBSYSTEMS / "config" / "profiles" / "ios-local-poc.yaml"))
    args = parser.parse_args()

    from testpilot_adapters.tree_sitter_chunker import TreeSwiftChunker
    from testpilot_core.chunking import chunk_swift
    from testpilot_orchestrator.config_loader import load_profile
    from testpilot_orchestrator.factory import build_container

    config = load_profile(Path(args.profile))
    config.paths["data_dir"] = str(SUBSYSTEMS / "out" / "chunker-compare-tmp")
    container = build_container(config, SUBSYSTEMS)
    try:
        docs_adapter = container.get("docs")
        scanned, _ = docs_adapter.scan_safely()
        swift_docs = [d for d in scanned if d.metadata_.get("kind_hint") == "swift"]
        if not swift_docs:
            print("❌ 未扫描到 Swift 文件，检查 profile code_roots 与 REPO_PATH")
            return 1
        tree_chunker = TreeSwiftChunker()
        sdk = _ios_sim_sdk()
        files = []
        for doc in swift_docs:
            text = docs_adapter.read_content(doc.urn)
            # fs 适配器 uri 带 file: scheme，直传 swiftc 打不开，先转本地路径
            uri = Path(url2pathname(urlparse(doc.uri).path)
                       if doc.uri.startswith("file:") else doc.uri)
            files.append(compare_file(uri, text, chunk_swift, tree_chunker, sdk))
    finally:
        container.get("store").close()

    agg = aggregate(files)
    _REPORT_DIR.mkdir(parents=True, exist_ok=True)
    json_path = _REPORT_DIR / "chunker-comparison.json"
    md_path = _REPORT_DIR / "chunker-comparison.md"
    json_path.write_text(json.dumps(
        {"aggregate": agg, "files": files}, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(agg, files), encoding="utf-8")
    print(json.dumps(agg, ensure_ascii=False, indent=2))
    print(f"\n报告：{md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
