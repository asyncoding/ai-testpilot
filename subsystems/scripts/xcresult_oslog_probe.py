#!/usr/bin/env python3
"""S3.3 xcresult 制品采集与 os_log 查询端到端探针（FR-EX-005/FR-EX-006，A21）。

与单测分工：test_xcresult_* / test_os_log_* 用 tmp 钉死每条分支；本探针走
**生产装配路径**（load_profile → build_container；XcresultToolResultParser /
OsLogQueryAdapter 的进程出口全部换成假执行器），复用 tests/fixtures 下的自造
夹具，验证：零 xcresulttool、零 simctl、零 ffmpeg、零端口、零网络、零真机状态。

故事线（10 项）：
  ① core 用例树解析：夹具三态展平、秒→毫秒、failureText 首行证据；
  ② 畸形夹具 fail-closed：未知 nodeType → SchemaValidationError；
  ③ 附件清单分型 + URN 回链纯函数（截图/录屏 → SCREENSHOT/VIDEO）；
  ④ 适配器全链：get tests → get attachments → export → localfs 内容寻址，
     失败用例 artifact_urns 两连且可解析，raw_report_ref 为 URN，未覆盖层显式；
  ⑤ 适配器响亮失败：xcresulttool 非零退出 / 导出缺文件 → AdapterUnavailableError；
  ⑥ os_log argv 逐字对账：显式 udid（无 booted）、--style json、query 可复现；
  ⑦ os_log 适配器跑夹具 NDJSON：窗内 3 / 窗外 2 / 坏行 1，echo argv 与实跑同源；
  ⑧ 坏行样片封顶但总数不封顶；倒挂窗口零进程调用；
  ⑨ ffmpeg 抽帧 argv 对齐 docs/03 原型，fps 白名单越界即拒；
  ⑩ 工厂装配：xcode runner 注入真实 parser 且共享制品端口；artifact=stub 拒绝；
     os-log 显式 udid 装配、缺 udid/未知 type 拒绝。

报告落 out/eval/xcresult-oslog-probe.{json,md}（out/ 已 gitignore）。任一断言不符 exit 1。

用法（在 subsystems/ 下）：
  .venv/bin/python scripts/xcresult_oslog_probe.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages" / "workers" / "src"))

from testpilot_adapters.localfs_artifact import LocalFsArtifactAdapter  # noqa: E402
from testpilot_adapters.os_log import OsLogQueryAdapter  # noqa: E402
from testpilot_adapters.process_executor import CommandResult  # noqa: E402
from testpilot_adapters.xcresult_tool import XcresultToolResultParser  # noqa: E402
from testpilot_core.config_schema import AdapterRef  # noqa: E402
from testpilot_core.enums import ArtifactType, CaseStatus  # noqa: E402
from testpilot_core.errors import (  # noqa: E402
    AdapterUnavailableError,
    SchemaValidationError,
)
from testpilot_core.ffmpeg_commands import build_frame_extract_argv  # noqa: E402
from testpilot_core.models import TestExecutionRequest  # noqa: E402
from testpilot_core.oslog_commands import build_os_log_query  # noqa: E402
from testpilot_core.xcresult_parse import (  # noqa: E402
    link_attachment_urns,
    parse_attachments,
    parse_test_nodes,
)
from testpilot_orchestrator.config_loader import load_profile  # noqa: E402
from testpilot_orchestrator.factory import build_container  # noqa: E402

SUBSYSTEMS = Path(__file__).resolve().parents[1]
PROFILE = SUBSYSTEMS / "config" / "profiles" / "offline-demo.yaml"
FIX_XC = SUBSYSTEMS / "tests" / "fixtures" / "xcresult"
FIX_OSLOG = SUBSYSTEMS / "tests" / "fixtures" / "oslog"
EVAL_DIR = SUBSYSTEMS / "out" / "eval"
_UDID = "5411787C-8AFF-4EBF-9000-EB5EFD1457D5"
_FAIL_REF = (
    "AlarmKitSpikeTests.xctest/AlarmKitSpikeTests/"
    "testKillBackupFallsBackTo30s()"
)
_START = "2026-11-10T10:00:00"
_END = "2026-11-10T10:20:00"
_PREDICATE = 'subsystem == "com.thames.alarmkitspike"'


class FakeXcresultExecutor:
    """按 argv 分发夹具 JSON；export 时在 --output-path 物化两个自造附件。"""

    def __init__(self, tests_text: str, attachments_text: str, *,
                 tests_rc: int = 0, export_rc: int = 0,
                 skip_files: bool = False) -> None:
        self._tests = tests_text
        self._attachments = attachments_text
        self._rc = {"tests": tests_rc, "export": export_rc}
        self._skip_files = skip_files
        self.calls: list[list[str]] = []

    async def run(self, argv: list[str], *, cwd: str | None = None) -> CommandResult:
        self.calls.append(argv)
        if argv[2:5] == ["get", "test-results", "tests"]:
            return CommandResult(self._rc["tests"], self._tests, "", list(argv))
        if argv[2:5] == ["get", "test-results", "attachments"]:
            return CommandResult(0, self._attachments, "", list(argv))
        if argv[2:4] == ["export", "attachments"]:
            if self._rc["export"] == 0 and not self._skip_files:
                out = Path(argv[argv.index("--output-path") + 1])
                (out / "Screenshot-Failure-1.png").write_bytes(b"\x89PNG synthetic")
                (out / "FailureRecording.mp4").write_bytes(b"\x00mp4 synthetic")
            return CommandResult(
                self._rc["export"], "",
                "" if self._rc["export"] == 0 else "export boom", list(argv))
        raise AssertionError(f"未预期调用: {argv}")


class FakeOslogExecutor:
    def __init__(self, stdout: str, *, rc: int = 0) -> None:
        self._stdout = stdout
        self._rc = rc
        self.calls: list[list[str]] = []

    async def run(self, argv: list[str], *, cwd: str | None = None) -> CommandResult:
        self.calls.append(argv)
        return CommandResult(self._rc, self._stdout, "", list(argv))


def _request() -> TestExecutionRequest:
    return TestExecutionRequest(
        request_id="req-a21", idempotency_key="idem-a21", project="ios-demo",
        selector={"test_ids": ["AlarmKitSpikeTests"]},
        commit_sha="9f8e7d6c", build_id="xcode-a21",
        environment={"capabilities": ["ios.simulator"],
                     "labels": {"udid": _UDID}},
    )


async def _main_async() -> tuple[list[dict[str, str]], list[str], Path]:
    checks: list[dict[str, str]] = []
    failures: list[str] = []
    work = Path(tempfile.mkdtemp(prefix="tp-a21-"))

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "passed": "✅" if ok else "❌", "detail": detail})
        if not ok:
            failures.append(name)

    tests_text = (FIX_XC / "test-results.json").read_text(encoding="utf-8")
    attachments_text = (FIX_XC / "attachments.json").read_text(encoding="utf-8")

    # ① core 用例树解析
    cases = parse_test_nodes(tests_text)
    ok_1 = (
        [c.status for c in cases]
        == [CaseStatus.PASSED, CaseStatus.SKIPPED, CaseStatus.FAILED]
        and cases[2].duration_ms == 410
        and cases[2].message.startswith("XCTAssertEqual failed")
        and cases[2].external_id == _FAIL_REF
    )
    check("① 夹具用例树三态展平/秒→毫秒/failureText 首行证据", ok_1,
          f"cases={len(cases)} failed_ms={cases[2].duration_ms} "
          f"msg={cases[2].message[:40]!r}")

    # ② 畸形 fail-closed
    malformed = (FIX_XC / "malformed-test-results.json").read_text(encoding="utf-8")
    try:
        parse_test_nodes(malformed)
        loud_2 = False
    except SchemaValidationError:
        loud_2 = True
    check("② 未知 nodeType 夹具 fail-closed", loud_2,
          "SchemaValidationError 命中" if loud_2 else "畸形输入被静默接受")

    # ③ 附件分型 + URN 回链
    grouped = parse_attachments(attachments_text)
    atts = grouped[_FAIL_REF]
    from testpilot_core.models import CaseResult

    case_results = [
        CaseResult(external_case_id=_FAIL_REF, status=CaseStatus.FAILED)
    ]
    linked = link_attachment_urns(
        case_results, {_FAIL_REF: ["urn:testpilot:localfs-artifact:screenshot:a",
                                   "urn:testpilot:localfs-artifact:video:b"]})
    ok_3 = (
        [a.artifact_type for a in atts]
        == [ArtifactType.SCREENSHOT, ArtifactType.VIDEO]
        and len(linked[0].artifact_urns) == 2
        and case_results[0].artifact_urns == []  # 输入不被改
    )
    check("③ 附件 mime 分型与 URN 回链纯函数", ok_3,
          f"types={[a.artifact_type.value for a in atts]} "
          f"urns={len(linked[0].artifact_urns)}")

    # ④ 适配器全链
    store = LocalFsArtifactAdapter(work / "artifacts")
    executor4 = FakeXcresultExecutor(tests_text, attachments_text)
    parser = XcresultToolResultParser(
        store, work_dir=work / "xcwork", executor=executor4)
    run = await parser(
        request=_request(), result_bundle_path=work / "r.xcresult",
        env_id=_UDID, returncode=1)
    by_id = {c.external_case_id: c for c in run.case_results}
    urns = by_id[_FAIL_REF].artifact_urns
    resolved = [await store.resolve_uri(u) for u in urns]
    types = sorted(a.type for a in run.artifacts)
    ok_4 = (
        [c[2] for c in executor4.calls] == ["get", "get", "export"]
        and len(urns) == 2 and all(r.startswith("file:") for r in resolved)
        and types == [ArtifactType.SCREENSHOT, ArtifactType.VIDEO,
                      ArtifactType.XCRESULT]
        and run.raw_report_ref and run.raw_report_ref.startswith("urn:")
        and run.uncovered_layers == ["background_audio", "codesigning", "ios.device"]
    )
    check("④ xcresulttool 三命令→localfs URN 回链失败用例（AC1）", ok_4,
          f"urns={len(urns)} artifacts={[t.value for t in types]} "
          f"raw_ref={'urn' if run.raw_report_ref else 'missing'}")

    # ⑤ 响亮失败两形态
    loud_5a = False
    try:
        await XcresultToolResultParser(
            LocalFsArtifactAdapter(work / "art5a"),
            work_dir=work / "w5a",
            executor=FakeXcresultExecutor(tests_text, attachments_text, tests_rc=64),
        )(request=_request(), result_bundle_path=work / "x", env_id=_UDID,
          returncode=1)
    except AdapterUnavailableError:
        loud_5a = True
    loud_5b = False
    try:
        await XcresultToolResultParser(
            LocalFsArtifactAdapter(work / "art5b"),
            work_dir=work / "w5b",
            executor=FakeXcresultExecutor(
                tests_text, attachments_text, skip_files=True),
        )(request=_request(), result_bundle_path=work / "x", env_id=_UDID,
          returncode=1)
    except AdapterUnavailableError:
        loud_5b = True
    check("⑤ 非零退出/导出缺文件均 AdapterUnavailableError",
          loud_5a and loud_5b, f"rc_loud={loud_5a} missing_file_loud={loud_5b}")

    # ⑥ os_log argv 对账 + 可复现
    q = build_os_log_query(
        udid=_UDID, predicate=_PREDICATE, start_time=_START, end_time=_END)
    ok_6 = (
        q.argv == [
            "xcrun", "simctl", "spawn", _UDID, "log", "show",
            "--start", _START, "--end", _END,
            "--predicate", _PREDICATE, "--style", "json",
        ]
        and "booted" not in q.argv
        and q.echo()["argv"] == q.argv
    )
    check("⑥ os_log argv 逐字对账（显式 udid/无 booted）+ echo 可复现", ok_6,
          f"argv_len={len(q.argv)} echo_match={q.echo()['argv'] == q.argv}")

    # ⑦ 适配器跑夹具 NDJSON
    ndjson = (FIX_OSLOG / "window.ndjson").read_text(encoding="utf-8")
    oslog_exec = FakeOslogExecutor(ndjson)
    adapter = OsLogQueryAdapter(_UDID, executor=oslog_exec)
    result = await adapter.query(
        run_id="run-a21", predicate=_PREDICATE,
        start_time=_START, end_time=_END)
    ok_7 = (
        len(result["entries"]) == 3
        and result["dropped_out_of_window"] == 2
        and result["bad_lines"] == 1
        and result["query"]["argv"] == oslog_exec.calls[0]
    )
    check("⑦ NDJSON 窗内 3/窗外 2/坏行 1，echo 与实跑 argv 同源（AC1/AC2）",
          ok_7,
          f"kept={len(result['entries'])} dropped={result['dropped_out_of_window']} "
          f"bad={result['bad_lines']}")

    # ⑧ 坏行封顶 + 倒挂零调用
    four_bad = "\n".join(
        ["{x", "123", json.dumps({"no_timestamp": 1}),
         json.dumps({"timestamp": "nope"})])
    exec8 = FakeOslogExecutor(four_bad)
    r8 = await OsLogQueryAdapter(_UDID, executor=exec8).query(
        run_id="r", predicate=_PREDICATE, start_time=_START, end_time=_END)
    inverted_exec = FakeOslogExecutor(ndjson)
    try:
        await OsLogQueryAdapter(_UDID, executor=inverted_exec).query(
            run_id="r", predicate=_PREDICATE,
            start_time=_END, end_time=_START)
        inverted_loud = False
    except SchemaValidationError:
        inverted_loud = True
    ok_8 = (
        r8["bad_lines"] == 4 and len(r8["bad_line_samples"]) == 3
        and inverted_loud and inverted_exec.calls == []
    )
    check("⑧ 坏行样片封顶 3/总数 4；倒挂窗口零进程 fail-closed", ok_8,
          f"bad_total={r8['bad_lines']} samples={len(r8['bad_line_samples'])} "
          f"inverted_loud={inverted_loud} calls={len(inverted_exec.calls)}")

    # ⑨ ffmpeg 白名单
    argv9 = build_frame_extract_argv(
        video_path="store/artifacts/FailureRecording.mp4",
        frames_dir="store/frames/run-a21")
    rejected = []
    for bad in (0, 31, 2.0, True):
        try:
            build_frame_extract_argv(video_path="v.mp4", frames_dir="d", fps=bad)
        except ValueError:
            rejected.append(bad)
    ok_9 = (
        argv9 == ["ffmpeg", "-y", "-i",
                  "store/artifacts/FailureRecording.mp4", "-vf", "fps=2",
                  "store/frames/run-a21/frame_%03d.png"]
        and len(rejected) == 4
    )
    check("⑨ ffmpeg 抽帧 argv 对齐 docs/03 + fps 白名单（1..30 整数）", ok_9,
          f"argv={argv9} rejected={rejected}")

    # ⑩ 工厂装配
    config = load_profile(PROFILE)
    config.paths["data_dir"] = str(work / "data")
    config.paths["fixtures_dir"] = str(SUBSYSTEMS / "tests" / "fixtures")
    config.adapters.runner = AdapterRef(type="xcode", settings={
        "workspace": "AlarmKitSpike/AlarmKitSpike.xcworkspace",
        "scheme": "AlarmKitSpike",
        "result_bundle_dir": str(work / "bundles"),
    })
    config.adapters.environment = AdapterRef(type="ios-simulator", settings={
        "devices": [{"udid": _UDID, "name": "iPhone 17",
                     "capabilities": ["ios.simulator"]}]})
    config.adapters.logs = AdapterRef(
        type="os-log", settings={"udid": _UDID})
    container = build_container(config, SUBSYSTEMS)
    parser_ok = isinstance(
        container.get("runner")._parser, XcresultToolResultParser)  # noqa: SLF001
    shared_store = (
        container.get("runner")._parser._store is container.get("artifact")  # noqa: SLF001
    )
    logs_ok = isinstance(container.get("logs"), OsLogQueryAdapter)
    container.get("store").close()

    config_bad = load_profile(PROFILE)
    config_bad.paths["data_dir"] = str(work / "data-bad")
    config_bad.paths["fixtures_dir"] = str(SUBSYSTEMS / "tests" / "fixtures")
    config_bad.adapters.runner = AdapterRef(type="xcode", settings={
        "workspace": "w", "scheme": "s"})
    config_bad.adapters.artifact = AdapterRef(type="stub", settings={})
    try:
        build_container(config_bad, SUBSYSTEMS)
        no_artifact_loud = False
    except ValueError:
        no_artifact_loud = True

    config_logs = load_profile(PROFILE)
    config_logs.paths["data_dir"] = str(work / "data-logs")
    config_logs.paths["fixtures_dir"] = str(SUBSYSTEMS / "tests" / "fixtures")
    config_logs.adapters.logs = AdapterRef(type="os-log", settings={})
    try:
        build_container(config_logs, SUBSYSTEMS)
        missing_udid_loud = False
    except ValueError:
        missing_udid_loud = True
    ok_10 = (
        parser_ok and shared_store and logs_ok
        and no_artifact_loud and missing_udid_loud
    )
    check("⑩ 工厂：真实 parser 注入+共享制品端口；os-log udid 装配；两坏配置拒绝",
          ok_10,
          f"parser={parser_ok} shared={shared_store} logs={logs_ok} "
          f"stub_artifact_loud={no_artifact_loud} missing_udid_loud={missing_udid_loud}")

    return checks, failures, work


def main() -> int:
    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    checks, failures, work = asyncio.run(_main_async())

    report_doc = {
        "probe": "xcresult_oslog_probe",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "workdir": str(work),
        "summary": {
            "passed": sum(1 for c in checks if c["passed"] == "✅"),
            "failed": len(failures),
            "total": len(checks),
        },
        "checks": checks,
    }
    (EVAL_DIR / "xcresult-oslog-probe.json").write_text(
        json.dumps(report_doc, ensure_ascii=False, indent=2), encoding="utf-8")
    md_lines = [
        "# S3.3 xcresult 制品与 os_log 探针报告"
        "（xcresult_oslog_probe / FR-EX-005/006）",
        "",
        f"生成时间：{report_doc['generated_at']}",
        f"工作目录：`{work}`",
        "",
        "| # | 检查 | 结果 | 证据 |",
        "|---|---|---|---|",
    ]
    md_lines += [
        f"| {i} | {c['name']} | {c['passed']} | {c['detail']} |"
        for i, c in enumerate(checks, 1)
    ]
    (EVAL_DIR / "xcresult-oslog-probe.md").write_text(
        "\n".join(md_lines) + "\n", encoding="utf-8")

    for c in checks:
        print(f"{c['passed']} {c['name']} — {c['detail']}")
    print(f"报告：{EVAL_DIR / 'xcresult-oslog-probe.json'}")
    if failures:
        print("❌ xcresult_oslog_probe 存在失败检查", file=sys.stderr)
        return 1
    print("✅ xcresult_oslog_probe 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
