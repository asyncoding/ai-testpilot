"""xctestrun 环境变量注入器（USB relay 转正组件，console script: testpilot-inject-xctestrun）。

把 TestPilotKit 反向监听三要素（TP_LISTEN_PORT/TP_SESSION/TP_TOKEN）注入
xcodebuild build-for-testing 产出的 .xctestrun，使 Runner 启动即带通道环境。
``testpilot-hybrid ws-run`` 编排器与本独立 CLI 共用同一套函数。

环境变量名是**线协议稳定名**：Swift TestPilotKit 按字面量读环境，模块可以改名，
这些键名刻意不随产品化改名（含 TP_TCA1_STRUCTURAL 这个历史专名，同此纪律）。

纪律：

1. **只写副本**：源 xctestrun 永不原地修改；副本必须落在源文件同目录——
   xctestrun 内全是 __TESTROOT__/__TESTHOST__ 相对路径，换目录即断链。
2. **形态自适应**：
   - FV1：顶层各 target 字典（跳过 __xctestrun_metadata__）下的
     EnvironmentVariables 与 TestingEnvironmentVariables，存在即注（真机两字典
     都注；模拟器仅注前者也能跑通，两字典都注的不回归由端到端测试钉住）；
   - FV2/旧形态：:TestConfigurations:i:TestTargets:j 下同名两字典。
   UITargetAppEnvironmentVariables 是 AUT 进程环境，刻意不注（SDK 跑在 Runner）。
3. **幂等**：每个键先 Delete（不存在则容忍）再 Add，重复执行结果字节一致。
4. **fail-loud**：缺文件/畸形 plist/同目录约束破坏/找不到注入点/PlistBuddy 实写失败
   一律抛 InjectionError，CLI 映射 exit 2。

检测与路径规划是纯函数 plan_injection（plistlib 读内存字典），实写走
/usr/libexec/PlistBuddy 子进程——与手工验证过的机制完全同源，且不整体重排
Xcode 产出的 plist 字节布局。非 macOS 宿主没有 PlistBuddy，实写用例整体 skip，
纯函数用例全平台 hostless 可跑。
"""

from __future__ import annotations

import argparse
import plistlib
import re
import shutil
import subprocess
import sys
from pathlib import Path
from xml.parsers.expat import ExpatError

PLIST_BUDDY = "/usr/libexec/PlistBuddy"
METADATA_KEY = "__xctestrun_metadata__"
# 真机 FV1 两个环境字典都要注；名字逐字对齐 Xcode 实际产出。
ENV_DICT_NAMES = ("EnvironmentVariables", "TestingEnvironmentVariables")
# 线协议默认值：Swift TestPilotKit 与 ws-run profile 按同一组默认对齐。
DEFAULT_ENV = {
    "TP_LISTEN_PORT": "9876",
    "TP_SESSION": "hsess-p0",
    "TP_TOKEN": "tok-p0",
}
# PlistBuddy 路径以 ':' 分段且无转义语法；环境值进 -c 字符串按空格分词。
# 线缆协议全部取值都是简单 token，越界即 fail-loud，不静默截断。
_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_VALUE_RE = re.compile(r"[A-Za-z0-9._\-]+")


class InjectionError(Exception):
    """注入前置条件不满足或实写失败（CLI 统一映射 exit 2）。"""


def _check_key(key: str) -> str:
    if not isinstance(key, str) or not _KEY_RE.fullmatch(key):
        raise InjectionError(f"环境变量名非法：{key!r}，要求匹配 {_KEY_RE.pattern}")
    return key


def _check_value(value: str) -> str:
    if not isinstance(value, str) or not _VALUE_RE.fullmatch(value):
        raise InjectionError(f"环境变量值非法：{value!r}，要求匹配 {_VALUE_RE.pattern}")
    return value


def _pb_path_segment(segment: str) -> str:
    # PlistBuddy 路径分隔符是 ':' 且无文档化转义；Xcode target 名不含 ':'，
    # 真遇到就 fail-loud，而不是拼出错误路径静默写错位置。
    if ":" in segment:
        raise InjectionError(f"plist 键名含 ':'，PlistBuddy 路径无法表达：{segment!r}")
    return segment


def plan_injection(plist: dict, env: dict) -> list[tuple[str, str]]:
    """纯函数：从内存 plist 字典规划全部 (PlistBuddy 键路径, 值) 写入项。

    支持 FV1 顶层 target 形态与 FV2 TestConfigurations 嵌套形态；两种形态
    同时存在时全部注入（互为冗余，不影响 Runner 取值）。
    """
    if not isinstance(plist, dict):
        raise InjectionError("xctestrun 根节点不是字典，无法识别形态")
    checked_env = {_check_key(k): _check_value(v) for k, v in env.items()}
    if not checked_env:
        raise InjectionError("待注入环境变量为空")

    dict_points: list[str] = []

    # FV1：顶层 target 字典（元数据键跳过）。
    for top_key, top_val in plist.items():
        if top_key == METADATA_KEY or not isinstance(top_val, dict):
            continue
        for name in ENV_DICT_NAMES:
            if isinstance(top_val.get(name), dict):
                dict_points.append(
                    f":{_pb_path_segment(top_key)}:{_pb_path_segment(name)}"
                )

    # FV2/旧形态：TestConfigurations[*].TestTargets[*]。
    configs = plist.get("TestConfigurations")
    if isinstance(configs, list):
        for ci, cfg in enumerate(configs):
            if not isinstance(cfg, dict):
                continue
            targets = cfg.get("TestTargets")
            if not isinstance(targets, list):
                continue
            for ti, target in enumerate(targets):
                if not isinstance(target, dict):
                    continue
                for name in ENV_DICT_NAMES:
                    if isinstance(target.get(name), dict):
                        dict_points.append(
                            f":TestConfigurations:{ci}:TestTargets:{ti}:"
                            f"{_pb_path_segment(name)}"
                        )

    if not dict_points:
        raise InjectionError(
            "找不到任何环境变量注入点（无 FV1 顶层 target 环境字典，也无 FV2 TestConfigurations）"
        )

    return [
        (f"{point}:{key}", value)
        for point in dict_points
        for key, value in checked_env.items()
    ]


def load_xctestrun(path: Path) -> dict:
    if not path.is_file():
        raise InjectionError(f"xctestrun 不存在：{path}")
    try:
        # Python 3.14 实测：截断 XML 直接抛 ExpatError，旧版包成 InvalidFileException，
        # 两类都纳入 fail-loud；ValueError/TypeError 覆盖二进制 plist 错版等畸形。
        plist = plistlib.loads(path.read_bytes())
    except (plistlib.InvalidFileException, ExpatError, ValueError, TypeError) as exc:
        raise InjectionError(f"xctestrun plist 畸形，无法解析：{path}（{exc}）") from exc
    if not isinstance(plist, dict):
        raise InjectionError(f"xctestrun 根节点不是字典：{path}")
    return plist


def _run_plist_buddy(args: list[str], *, tolerate_missing: bool) -> None:
    proc = subprocess.run(args, capture_output=True, text=True)
    if proc.returncode != 0:
        # PlistBuddy 实测文案："Delete: Entry, ":...", Does Not Exist"（rc=1）。
        if tolerate_missing and "Does Not Exist" in proc.stderr:
            return
        raise InjectionError(
            f"PlistBuddy 实写失败 rc={proc.returncode}：{' '.join(args)}\nstderr={proc.stderr.strip()}"
        )


def inject_file(input_path: str | Path, output_path: str | Path, env: dict) -> list[tuple[str, str]]:
    """复制源 xctestrun 到同目录副本并注入 env，返回全部写入项。源文件零改动。"""
    inp = Path(input_path)
    out = Path(output_path)
    if not inp.is_file():
        raise InjectionError(f"xctestrun 不存在：{inp}")
    if out.parent.resolve() != inp.parent.resolve():
        raise InjectionError(
            f"输出副本必须与源文件同目录（__TESTROOT__ 相对路径约束）："
            f"源目录 {inp.parent}，输出目录 {out.parent}"
        )
    if not Path(PLIST_BUDDY).exists():
        # 显式分支：非 macOS 宿主没有 PlistBuddy，fail-loud 而非悄悄换实现。
        raise InjectionError(f"PlistBuddy 不存在，当前宿主无法实写：{PLIST_BUDDY}")

    plist = load_xctestrun(inp)
    writes = plan_injection(plist, env)

    shutil.copyfile(inp, out)
    for key_path, value in writes:
        _run_plist_buddy(
            [PLIST_BUDDY, "-c", f"Delete {key_path}", str(out)],
            tolerate_missing=True,
        )
        _run_plist_buddy(
            [PLIST_BUDDY, "-c", f"Add {key_path} string {value}", str(out)],
            tolerate_missing=False,
        )
    return writes


def _parse_extra_env(items: list[str]) -> dict:
    extra: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise InjectionError(f"--env 参数必须是 KEY=VALUE：{item!r}")
        key, value = item.split("=", 1)
        extra[_check_key(key)] = _check_value(value)
    return extra


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="xctestrun TestPilot 通道环境注入器")
    p.add_argument("--input", required=True, help="源 .xctestrun（永不原地修改）")
    p.add_argument(
        "--output",
        help="注入副本路径，必须与源同目录；默认 <源stem>.injected.xctestrun",
    )
    p.add_argument("--port", default=DEFAULT_ENV["TP_LISTEN_PORT"], help="TP_LISTEN_PORT，默认 9876")
    p.add_argument("--session", default=DEFAULT_ENV["TP_SESSION"], help="TP_SESSION，默认 hsess-p0")
    p.add_argument("--token", default=DEFAULT_ENV["TP_TOKEN"], help="TP_TOKEN，默认 tok-p0")
    p.add_argument(
        "--env",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="附加环境变量，可重复；如 --env TP_TCA1_STRUCTURAL=1（同名覆盖内置默认）",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    inp = Path(args.input)
    out = Path(args.output) if args.output else inp.with_name(f"{inp.stem}.injected.xctestrun")
    try:
        env = {
            "TP_LISTEN_PORT": _check_value(str(args.port)),
            "TP_SESSION": _check_value(str(args.session)),
            "TP_TOKEN": _check_value(str(args.token)),
        }
        env.update(_parse_extra_env(args.env))
        writes = inject_file(inp, out, env)
    except InjectionError as exc:
        print(f"[inject-xctestrun] 失败：{exc}", file=sys.stderr)
        return 2

    points = sorted({path.rsplit(":", 1)[0] for path, _ in writes})
    print(f"[inject-xctestrun] 成功：{inp} -> {out}")
    print(f"[inject-xctestrun] 注入点 {len(points)} 个：")
    for point in points:
        print(f"  {point}")
    print(f"[inject-xctestrun] 键值 {len(writes)} 项：{', '.join(env)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
