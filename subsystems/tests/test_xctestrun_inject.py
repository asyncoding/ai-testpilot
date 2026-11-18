"""xctestrun_inject 转正后的 hermetic 测试（自 tca1-carryin 批次 29 条迁移）。

分两层：
- plan_injection 纯函数测试：plistlib 造 FV1（单/双环境字典）、FV2 嵌套夹具，
  零子进程，覆盖形态自适应、注入点缺失 fail-loud、名字/值校验；
- inject_file/main 端到端：真实 /usr/libexec/PlistBuddy 实写 tmp 副本再读回，
  覆盖同目录约束、幂等、源文件零改动、CLI 退出码。非 macOS 宿主自动 skip。
"""

from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

from testpilot_workers import xctestrun_inject as inj

PLIST_BUDDY = Path(inj.PLIST_BUDDY)
pytestmark = pytest.mark.skipif(
    not PLIST_BUDDY.exists(), reason="PlistBuddy 仅 macOS 自带，端到端用例在非 mac 宿主跳过"
)

ENV = {"TP_LISTEN_PORT": "9876", "TP_SESSION": "hsess-p0", "TP_TOKEN": "tok-p0"}


# ── 夹具构造 ────────────────────────────────────────────────────────────────


def fv1_plist(*, with_testing_env: bool = True, with_target_app_env: bool = True) -> dict:
    """仿 Xcode 27 实测 FV1：顶层 target 字典（模拟器当前产物同形）。"""
    target = {
        "IsUITestBundle": True,
        "EnvironmentVariables": {"TERM": "dumb", "OS_ACTIVITY_DT_MODE": "YES"},
    }
    if with_testing_env:
        target["TestingEnvironmentVariables"] = {"XCODE_SCHEME_NAME": "TPSpike"}
    if with_target_app_env:
        target["UITargetAppEnvironmentVariables"] = {"XCODE_SCHEME_NAME": "TPSpike"}
    return {
        "__xctestrun_metadata__": {"FormatVersion": 1},
        "TPSpikeUITests": target,
    }


def fv2_plist(*, n_targets: int = 1) -> dict:
    """旧形态：TestConfigurations 嵌套 TestTargets 数组。"""
    targets = []
    for i in range(n_targets):
        targets.append(
            {
                "BlueprintName": f"Target{i}",
                "EnvironmentVariables": {"TERM": "dumb"},
                "TestingEnvironmentVariables": {"XCODE_SCHEME_NAME": "X"},
            }
        )
    return {"TestConfigurations": [{"Name": "Config0", "TestTargets": targets}]}


def write_plist(path: Path, plist: dict) -> Path:
    path.write_bytes(plistlib.dumps(plist, fmt=plistlib.FMT_XML))
    return path


def paths_for(writes, dict_name: str) -> set[str]:
    return {p for p, _ in writes if f":{dict_name}:" in p}


# ── plan_injection 纯函数 ────────────────────────────────────────────────────


def test_fv1_dual_dict_injects_both_env_dicts():
    writes = inj.plan_injection(fv1_plist(), ENV)
    kv = dict(writes)
    # 两个环境字典都要注（真机硬要求；模拟器双注的不回归由端到端绿路实证）。
    for dict_name in ("EnvironmentVariables", "TestingEnvironmentVariables"):
        for key, value in ENV.items():
            assert kv[f":TPSpikeUITests:{dict_name}:{key}"] == value


def test_fv1_red_guard_testing_environment_must_be_covered():
    """红证护栏：存在 TestingEnvironmentVariables 时规划必须覆盖它。

    若有人把注入收窄回仅 EnvironmentVariables（早期手工做法），本用例立即变红。
    """
    writes = inj.plan_injection(fv1_plist(with_testing_env=True), ENV)
    assert paths_for(writes, "TestingEnvironmentVariables"), (
        "TestingEnvironmentVariables 存在却未规划注入，真机 Runner 会拿不到通道环境"
    )


def test_fv1_single_dict_only_available_point():
    plist = fv1_plist(with_testing_env=False)
    writes = inj.plan_injection(plist, ENV)
    assert paths_for(writes, "TestingEnvironmentVariables") == set()
    kv = dict(writes)
    for key, value in ENV.items():
        assert kv[f":TPSpikeUITests:EnvironmentVariables:{key}"] == value


def test_fv1_never_touches_uitarget_app_env():
    # UITargetAppEnvironmentVariables 是 AUT 进程环境，SDK 在 Runner，刻意不注。
    writes = inj.plan_injection(fv1_plist(), ENV)
    assert all(":UITargetAppEnvironmentVariables:" not in p for p, _ in writes)


def test_fv1_metadata_key_skipped():
    writes = inj.plan_injection(fv1_plist(), ENV)
    assert all(not p.startswith("::__xctestrun_metadata__") for p, _ in writes)


def test_fv2_nested_indexed_paths():
    writes = inj.plan_injection(fv2_plist(n_targets=2), ENV)
    kv = dict(writes)
    for ti in range(2):
        for key, value in ENV.items():
            assert kv[f":TestConfigurations:0:TestTargets:{ti}:EnvironmentVariables:{key}"] == value
            assert kv[f":TestConfigurations:0:TestTargets:{ti}:TestingEnvironmentVariables:{key}"] == value


def test_no_injection_point_fails_loud():
    with pytest.raises(inj.InjectionError, match="注入点"):
        inj.plan_injection({"__xctestrun_metadata__": {"FormatVersion": 1}}, ENV)


def test_root_non_dict_fails_loud():
    with pytest.raises(inj.InjectionError):
        inj.plan_injection([], ENV)  # type: ignore[arg-type]


def test_empty_env_fails_loud():
    with pytest.raises(inj.InjectionError, match="为空"):
        inj.plan_injection(fv1_plist(), {})


@pytest.mark.parametrize("bad_key", ["1TP", "TP-X", "", "a.b"])
def test_bad_env_key_fails_loud(bad_key):
    with pytest.raises(inj.InjectionError, match="变量名非法"):
        inj.plan_injection(fv1_plist(), {bad_key: "v"})


@pytest.mark.parametrize("bad_value", ["has space", "", "中文", "a/b"])
def test_bad_env_value_fails_loud(bad_value):
    with pytest.raises(inj.InjectionError, match="变量值非法"):
        inj.plan_injection(fv1_plist(), {"TP_X": bad_value})


def test_colon_in_target_name_fails_loud():
    plist = fv1_plist()
    plist["Weird:Target"] = {"EnvironmentVariables": {}}
    with pytest.raises(inj.InjectionError, match="PlistBuddy 路径无法表达"):
        inj.plan_injection(plist, ENV)


# ── inject_file 端到端（真实 PlistBuddy 实写） ───────────────────────────────


def test_inject_file_fv1_roundtrip_and_source_untouched(tmp_path):
    src = write_plist(tmp_path / "TPSpike.xctestrun", fv1_plist())
    original_bytes = src.read_bytes()
    out = tmp_path / "injected.xctestrun"

    writes = inj.inject_file(src, out, ENV)
    assert len(writes) == 6  # 2 字典 × 3 键

    result = plistlib.loads(out.read_bytes())
    target = result["TPSpikeUITests"]
    for dict_name in ("EnvironmentVariables", "TestingEnvironmentVariables"):
        for key, value in ENV.items():
            assert target[dict_name][key] == value
    # 既有键不被破坏，AUT 环境字典不被污染。
    assert target["EnvironmentVariables"]["TERM"] == "dumb"
    assert "TP_TOKEN" not in target["UITargetAppEnvironmentVariables"]
    # 源文件字节零改动。
    assert src.read_bytes() == original_bytes


def test_inject_file_idempotent_and_overwrites_stale(tmp_path):
    src = write_plist(tmp_path / "TPSpike.xctestrun", fv1_plist())
    out = tmp_path / "injected.xctestrun"

    inj.inject_file(src, out, ENV)
    first = out.read_bytes()
    # 第二次执行：Delete-then-Add 幂等，产物字节一致。
    inj.inject_file(src, out, ENV)
    assert out.read_bytes() == first

    # 先以 9877 注出副本（模拟换端口前的旧产物），再用默认 ENV 对同路径重注：
    # copyfile 先清掉旧副本、Delete-then-Add 保证不残留旧值。
    old_env = {**ENV, "TP_LISTEN_PORT": "9877"}
    inj.inject_file(src, out, old_env)
    assert plistlib.loads(out.read_bytes())["TPSpikeUITests"]["EnvironmentVariables"][
        "TP_LISTEN_PORT"
    ] == "9877"
    inj.inject_file(src, out, ENV)
    refreshed = plistlib.loads(out.read_bytes())
    assert refreshed["TPSpikeUITests"]["EnvironmentVariables"]["TP_LISTEN_PORT"] == "9876"
    # 两个字典同步刷新，不允许一个新一个旧。
    assert refreshed["TPSpikeUITests"]["TestingEnvironmentVariables"]["TP_LISTEN_PORT"] == "9876"


def test_inject_file_fv2_roundtrip(tmp_path):
    src = write_plist(tmp_path / "old.xctestrun", fv2_plist())
    out = tmp_path / "old.injected.xctestrun"
    inj.inject_file(src, out, ENV)
    result = plistlib.loads(out.read_bytes())
    target = result["TestConfigurations"][0]["TestTargets"][0]
    assert target["EnvironmentVariables"]["TP_SESSION"] == "hsess-p0"
    assert target["TestingEnvironmentVariables"]["TP_TOKEN"] == "tok-p0"


def test_inject_missing_file_exit2(tmp_path):
    with pytest.raises(inj.InjectionError, match="不存在"):
        inj.inject_file(tmp_path / "nope.xctestrun", tmp_path / "out.xctestrun", ENV)


def test_inject_malformed_plist_exit2(tmp_path):
    src = tmp_path / "bad.xctestrun"
    src.write_text("<?xml version='1.0'?><plist><dict>")  # 截断的畸形 plist
    with pytest.raises(inj.InjectionError, match="畸形"):
        inj.inject_file(src, tmp_path / "out.xctestrun", ENV)


def test_inject_output_must_share_source_directory(tmp_path):
    src = write_plist(tmp_path / "a.xctestrun", fv1_plist())
    other = tmp_path / "subdir"
    other.mkdir()
    with pytest.raises(inj.InjectionError, match="同目录"):
        inj.inject_file(src, other / "out.xctestrun", ENV)


# ── CLI main ─────────────────────────────────────────────────────────────────


def test_cli_full_success_with_extra_env(tmp_path, capsys):
    src = write_plist(tmp_path / "TPSpike.xctestrun", fv1_plist())
    out = tmp_path / "cli-out.xctestrun"
    rc = inj.main(
        [
            "--input", str(src),
            "--output", str(out),
            "--port", "9877",
            "--session", "hsess-x",
            "--token", "tok-x",
            "--env", "TP_TCA1_STRUCTURAL=1",
        ]
    )
    assert rc == 0
    target = plistlib.loads(out.read_bytes())["TPSpikeUITests"]
    assert target["EnvironmentVariables"]["TP_LISTEN_PORT"] == "9877"
    assert target["EnvironmentVariables"]["TP_TCA1_STRUCTURAL"] == "1"
    assert target["TestingEnvironmentVariables"]["TP_TOKEN"] == "tok-x"
    assert "注入点 2 个" in capsys.readouterr().out


def test_cli_default_output_name(tmp_path):
    src = write_plist(tmp_path / "TPSpike.xctestrun", fv1_plist())
    rc = inj.main(["--input", str(src)])
    assert rc == 0
    assert (tmp_path / "TPSpike.injected.xctestrun").is_file()


def test_cli_missing_input_rc2(tmp_path):
    rc = inj.main(["--input", str(tmp_path / "nope.xctestrun")])
    assert rc == 2


def test_cli_bad_env_shape_rc2(tmp_path):
    src = write_plist(tmp_path / "a.xctestrun", fv1_plist())
    rc = inj.main(["--input", str(src), "--env", "NO_EQUALS_SIGN"])
    assert rc == 2


def test_cli_bad_value_rc2(tmp_path):
    src = write_plist(tmp_path / "a.xctestrun", fv1_plist())
    rc = inj.main(["--input", str(src), "--port", "98 76"])
    assert rc == 2
