#!/usr/bin/env python3
"""S1.3 真仓 live-verify 取证探针（docs/08 FR-CON-001 / FR-QA-002 / NFR-006）。

不是 CI 门禁：依赖本机真仓（TESTPILOT_REPO_PATH）与已入库的 ios profile，
verify_s0.sh 不调用它；CI/离线场景由 tmp 真仓单测与 recorded 契约测试覆盖。

四项检查，任一失败以退出码 1 返回：
  ① runSnooze 符号块 URN 验 HEAD → verified=True, body 未变（reason=unchanged）；
  ② 同一 URN 验指定旧 commit（默认 09b473b：kill-backup 90s→30s 调参提交）
     → body_changed=True，当场抓出"索引旧于代码"，且不依赖重新入库（FR-QA-002 AC1）。
     注：该区间真正变动的是 killBackupInterval 常量与 runSnooze/runNoteInAppFire
     注释（git diff 实证）；schedulePrimary 方法体本身未变，故选 runSnooze 而非它；
  ③ get_change_set(旧 commit, HEAD) → changed_files 非空，且含 MissionCoordinator
     modified 符号变更（FR-CON-001：base/head→ChangeSet，不读 commit message）；
  ④ 故障注入：RepoRef.repo_path 指向不存在目录 → 调用不抛穿、结果 degraded=True，
     且 verification_events 新增一行 degraded 留痕（NFR-006 + FR-QA-002 AC2）。

注意：live-verify 以 git HEAD（或指定 commit）blob 为准，不读工作区，
因此工作区未提交改动不影响判定；但入库（fs 扫描）读的是工作区——
正式取证前工作区应干净，否则"索引内容"本身可能对应未提交版本（README 已注明）。

用法（在 subsystems/ 下）：
  TESTPILOT_REPO_PATH=~/Documents/Code/app-calendar-alarm \\
  TESTPILOT_PROFILE=config/profiles/ios-local-poc.yaml \\
  .venv/bin/python scripts/live_verify_probe.py
  # 可选：--base <旧commit短sha>  --symbol-urn <URN片段，默认 schedulePrimary>
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages" / "workers" / "src"))

from testpilot_adapters.git_repo import GitCodeRepository  # noqa: E402
from testpilot_adapters.live_verify import LiveVerifier  # noqa: E402
from testpilot_core.models import RepoRef  # noqa: E402
from testpilot_workers._bootstrap import bootstrap  # noqa: E402

DEFAULT_BASE = "09b473b"  # feat(icon)；其后 7936295 把 killBackupInterval 90s→30s
# schedulePrimary 方法体在该区间未变（git diff 实证），runSnooze 切块含调参注释会翻转
DEFAULT_SYMBOL_URN_MATCH = "%#MissionCoordinator.runSnooze(alarmID:minutes:)%"


def _row_count(store, where: str, params: tuple) -> int:
    row = store.conn.execute(
        f"SELECT count(*) AS n FROM verification_events WHERE {where}", params
    ).fetchone()
    return int(row["n"])


async def amain() -> int:
    parser = argparse.ArgumentParser(description="S1.3 真仓 live-verify 取证探针")
    parser.add_argument("--base", default=DEFAULT_BASE, help="对照用旧 commit（短 sha 即可）")
    parser.add_argument("--symbol-urn-match", default=DEFAULT_SYMBOL_URN_MATCH,
                        help="目标符号块 URN 的 LIKE 片段")
    args = parser.parse_args()

    failures: list[str] = []

    def check(name: str, ok: bool, evidence: str) -> None:
        print(f"\n{'─' * 78}\n{'✅' if ok else '❌'} {name}\n{evidence}")
        if not ok:
            failures.append(name)

    container = bootstrap("live-verify-probe")
    try:
        store = container.get("store")
        scm = container.get("scm")
        repo = container.get("repo")
        search = container.get("search")
        if not isinstance(scm, GitCodeRepository):
            print(
                "当前 profile 未装配真实 git scm（需要 ios-local-poc.yaml + TESTPILOT_REPO_PATH）。",
                file=sys.stderr,
            )
            return 2
        if not hasattr(search, "live_verify"):
            print("当前 search 适配器未接线 live_verify。", file=sys.stderr)
            return 2

        # 定位已入库的目标符号块 URN（不硬编码完整 URN：#L 行号提示可能漂移）
        rows = store.conn.execute(
            "SELECT urn FROM chunks WHERE urn LIKE ? AND kind='code_symbol' LIMIT 1",
            (args.symbol_urn_match,),
        ).fetchall()
        if not rows:
            print(f"库中找不到匹配 {args.symbol_urn_match!r} 的符号块；请先 --full 入库。",
                  file=sys.stderr)
            return 2
        urn = rows[0]["urn"]

        head = await scm.head_sha(repo)
        print(f"repo_path = {repo.repo_path}")
        print(f"HEAD      = {head}")
        print(f"base      = {args.base}")
        print(f"URN       = {urn}")

        # ── ① HEAD 当场核验：索引与 HEAD 一致 ──────────────────────────────
        r_head = await search.live_verify(urn, "")
        ok1 = (not r_head["degraded"] and r_head["verified"] is True
               and r_head["body_changed"] is False and r_head["exists"] is True
               and r_head["reason"] == "unchanged")
        check("① HEAD live-verify → verified=True / body 未变", ok1,
              f"exists={r_head['exists']} verified={r_head['verified']} "
              f"body_changed={r_head['body_changed']} degraded={r_head['degraded']} "
              f"reason={r_head['reason']} head_commit={r_head['head_commit'][:10]} "
              f"latency_ms={r_head['latency_ms']}")

        # ── ② 同一 URN 验旧 commit：不重灌即可抓出 body_changed ────────────
        r_old = await search.live_verify(urn, args.base)
        ok2 = (not r_old["degraded"] and r_old["exists"] is True
               and r_old["body_changed"] is True and r_old["verified"] is False)
        check(f"② 旧 commit {args.base} → body_changed=True（stale-index 当场可抓）", ok2,
              f"exists={r_old['exists']} verified={r_old['verified']} "
              f"body_changed={r_old['body_changed']} degraded={r_old['degraded']} "
              f"reason={r_old['reason']} requested_commit={r_old['requested_commit'][:10]}")

        # ── ③ base/head → ChangeSet（文件 + 符号增删改，不读 message）──────
        cs = await scm.get_change_set(repo, args.base, head)
        modified_symbols = [
            s.symbol_id for s in cs.symbol_changes if s.change_type.value == "modified"
        ]
        ok3 = bool(cs.changed_files) and "MissionCoordinator" in modified_symbols
        kinds: dict[str, int] = {}
        for s in cs.symbol_changes:
            kinds[s.change_type.value] = kinds.get(s.change_type.value, 0) + 1
        check(f"③ ChangeSet({args.base}..{head[:7]}) 含 MissionCoordinator modified", ok3,
              f"changed_files={len(cs.changed_files)} 个（示例："
              f"{', '.join(cs.changed_files[:3])}）；"
              f"符号变更 {kinds}；MissionCoordinator 在 modified 列表："
              f"{'MissionCoordinator' in modified_symbols}")

        # ── ④ 故障注入：repo_path 不存在 → 降级不抛穿且留痕 ────────────────
        missing_dir = Path(tempfile.gettempdir()) / "testpilot-live-verify-probe-no-such-repo"
        bad_repo = RepoRef(
            project=repo.project, repo_path=str(missing_dir),
            default_branch=repo.default_branch, tech_stack=repo.tech_stack,
        )
        bad_scm = GitCodeRepository(
            bad_repo,
            git_path=getattr(scm, "git_path", "git"),
            timeout_s=getattr(scm, "timeout_s", 15.0),
        )
        bad_verifier = LiveVerifier(
            bad_scm, store, bad_repo,
            tracer=container.get("tracer"), project=repo.project,
        )
        before = _row_count(store, "urn=? AND degraded=1", (urn,))
        error_raised = False
        try:
            r_bad = await bad_verifier.verify(urn, "")
        except Exception as exc:  # noqa: BLE001 — 探针要把"抛穿"本身记为失败
            error_raised = True
            r_bad = None
            print(f"  抛出未降级异常: {type(exc).__name__}: {exc}")
        after = _row_count(store, "urn=? AND degraded=1", (urn,))
        persisted = after == before + 1
        ok4 = (not error_raised and r_bad is not None and r_bad.degraded is True
               and r_bad.exists is None and r_bad.body_changed is None and persisted)
        detail = (f"degraded={r_bad.degraded if r_bad else 'n/a'} "
                  f"reason={r_bad.reason if r_bad else 'n/a'} "
                  f"exists={r_bad.exists if r_bad else 'n/a'} "
                  f"error={(r_bad.error[:80] if r_bad else '')!r}") if r_bad else ""
        check("④ 不可达仓库故障注入 → degraded 不抛穿 + verification_events 留痕", ok4,
              f"{detail}\n  verification_events(degraded) 行数 {before} → {after}")

    finally:
        container.get("store").close()

    print(f"\n{'=' * 78}")
    if failures:
        print(f"失败 {len(failures)} 项：{failures}")
        return 1
    print("四项取证全部通过（① HEAD 一致 / ② 旧 commit 抓出 body_changed / "
          "③ ChangeSet 文件+符号 / ④ 故障降级留痕）")
    return 0


def main() -> None:
    sys.exit(asyncio.run(amain()))


if __name__ == "__main__":
    main()
