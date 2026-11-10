#!/usr/bin/env python3
"""S2.6a L3 规则资产版本化端到端探针（docs/08 FR-KNO-002）。

与单测的分工：test_asset_* 用 tmp 仓钉死每条分支；本探针走**生产装配路径**
（load_profile → build_container → workers.asset_service → SQLite rule_assets），
在临时工作目录跑完整故事，不碰 store/offline-demo、不碰语料仓、不起端口、不访问网络。

故事线（确定性，无模型调用）：
  ① 空资产清单；
  ② 登记 prompt v1 → v2，版本号单调、内容 hash 随内容变化；
  ③ 同内容重放幂等（inserted=false，版本链不增生）；
  ④ kind 串档 fail-closed（AssetValidationError，旧版本零污染）；
  ⑤ diff v1→v2 字段级 + 字符串叶子 unified diff；
  ⑥ diff 不存在版本 → NotFoundError（HTTP 层映射 404）；
  ⑦ 灌 1 条人审 modify 偏好样本 → 回流 few_shot 资产，报告键齐全；
  ⑧ 回流资产进 latest 清单，entry 可溯源 sample_id 且带 content_seal；
  ⑨ 同批样本二次回流幂等（hash 不变，无新版本）；
  ⑩ 不可变证据：rule_assets 表行数=版本链长度，重开库后版本链无损；
  ⑪ 空样本回流被拒（不制造空 few_shot 资产）。

报告落 out/eval/asset-probe.{json,md}（out/ 已 gitignore）。任一断言不符 exit 1。

用法（在 subsystems/ 下）：
  .venv/bin/python scripts/asset_probe.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages" / "workers" / "src"))

from testpilot_adapters.store import bootstrap_store  # noqa: E402
from testpilot_core.errors import NotFoundError  # noqa: E402
from testpilot_core.models import PreferenceSample  # noqa: E402
from testpilot_core.rule_assets import AssetValidationError  # noqa: E402
from testpilot_orchestrator.config_loader import load_profile  # noqa: E402
from testpilot_orchestrator.factory import build_container  # noqa: E402
from testpilot_workers.asset_service import (  # noqa: E402
    DEFAULT_FEWSHOT_ASSET_ID,
    get_asset_diff,
    promote_preferences_to_fewshot,
    register_asset_revision,
)

SUBSYSTEMS = Path(__file__).resolve().parents[1]
PROFILE = SUBSYSTEMS / "config" / "profiles" / "offline-demo.yaml"
EVAL_DIR = SUBSYSTEMS / "out" / "eval"
_PROMPT_ID = "tp.prompt:probe"
_NOW = datetime(2026, 11, 10, 3, 30, tzinfo=timezone.utc)


def main() -> int:
    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="asset-probe-", dir=EVAL_DIR))
    checks: list[dict[str, str]] = []
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "passed": "✅" if ok else "❌", "detail": detail})
        if not ok:
            failures.append(f"{name}: {detail}")

    config = load_profile(PROFILE)
    config.paths["data_dir"] = str(work / "data")
    container = build_container(config, SUBSYSTEMS)
    store = container.get("store")
    data_dir = container.data_dir
    main_store_closed = False

    try:
        # ① 空清单
        check("① 空资产清单", store.list_latest_assets() == [],
              f"assets={len(store.list_latest_assets())}")

        # ② 登记 v1 → v2
        r1 = register_asset_revision(
            container, asset_id=_PROMPT_ID, kind="prompt", name="探针 prompt",
            content={"template": "你是测试助手", "meta": {"owner": "li"}}, author="li")
        r2 = register_asset_revision(
            container, asset_id=_PROMPT_ID, kind="prompt", name="探针 prompt",
            content={"template": "你是资深测试助手", "meta": {"owner": "wang"}}, author="li")
        chain = store.list_asset_revisions(_PROMPT_ID)
        check("② 版本单调递增且 hash 随内容变化",
              r1["inserted"] and r2["inserted"]
              and [r.version for r in chain] == [1, 2]
              and chain[0].content_hash != chain[1].content_hash,
              f"versions={[r.version for r in chain]}")

        # ③ 同内容幂等
        replay = register_asset_revision(
            container, asset_id=_PROMPT_ID, kind="prompt", name="探针 prompt",
            content={"template": "你是资深测试助手", "meta": {"owner": "wang"}}, author="li")
        check("③ 同内容重放幂等不增生",
              replay["inserted"] is False and replay["revision"].version == 2
              and len(store.list_asset_revisions(_PROMPT_ID)) == 2,
              f"inserted={replay['inserted']} version={replay['revision'].version}")

        # ④ kind 串档 fail-closed
        kind_drift_blocked = False
        try:
            register_asset_revision(
                container, asset_id=_PROMPT_ID, kind="golden", name="探针 prompt",
                content={"template": "串档"})
        except AssetValidationError:
            kind_drift_blocked = True
        check("④ kind 不可变 fail-closed 且旧版本零污染",
              kind_drift_blocked
              and [r.version for r in store.list_asset_revisions(_PROMPT_ID)] == [1, 2],
              f"blocked={kind_drift_blocked}")

        # ⑤ diff
        diff = get_asset_diff(
            container, asset_id=_PROMPT_ID, from_version=1, to_version=2)
        template_change = next(c for c in diff.changed if c.path == "template")
        check("⑤ diff 字段级+行级留痕",
              {c.path for c in diff.changed} == {"template", "meta.owner"}
              and any(line.startswith("-你是测试助手") for line in template_change.unified_diff)
              and any(line.startswith("+你是资深测试助手") for line in template_change.unified_diff),
              f"paths={[c.path for c in diff.changed]}")

        # ⑥ 不存在版本
        not_found = False
        try:
            get_asset_diff(container, asset_id=_PROMPT_ID, from_version=1, to_version=9)
        except NotFoundError:
            not_found = True
        check("⑥ 缺失版本 NotFound（API 映射 404）", not_found, f"raised={not_found}")

        # ⑦ 偏好样本回流
        store.save_preference_sample(PreferenceSample(
            sample_id="probe-s-1",
            case_urn="urn:testpilot:fs-cases:test_case:c.md#TC-1",
            suggestion_urn="urn:testpilot:fs-cases:oracle_suggestion:probe-s-1",
            reviewer="qa-zhang", reason="预期需含响铃持续",
            original={"type": "state_assertion", "expectation": "页面展示提醒卡片"},
            revised={"type": "ui_assertion", "expectation": "前台全屏提醒并持续响铃"},
            diff={"expectation": {"from": "页面展示提醒卡片", "to": "前台全屏提醒并持续响铃"}},
            created_at=_NOW,
        ))
        report = promote_preferences_to_fewshot(container, author="li", note="探针回流")
        check("⑦ 偏好回流 few_shot 报告键齐全",
              report["asset_id"] == DEFAULT_FEWSHOT_ASSET_ID
              and report["version"] == 1 and report["inserted"] is True
              and report["samples_consumed"] == 1
              and report["source_sample_ids"] == ["probe-s-1"]
              and len(report["content_hash"]) == 64,
              json.dumps(report, ensure_ascii=False))

        # ⑧ latest 清单 + seal + 溯源
        latest = {r.asset_id: r for r in store.list_latest_assets()}
        fewshot = latest[DEFAULT_FEWSHOT_ASSET_ID]
        entry = fewshot.content["entries"][0]
        check("⑧ 回流资产可见、seal 与来源溯源完整",
              set(latest) == {_PROMPT_ID, DEFAULT_FEWSHOT_ASSET_ID}
              and fewshot.kind == "few_shot"
              and bool(fewshot.content["content_seal"])
              and entry["sample_id"] == "probe-s-1"
              and entry["diff"]["expectation"]["to"].startswith("前台全屏"),
              f"assets={sorted(latest)} seal={bool(fewshot.content['content_seal'])}")

        # ⑨ 二次回流幂等
        again = promote_preferences_to_fewshot(container, author="li", note="探针回流")
        check("⑨ 同批样本二次回流幂等",
              again["inserted"] is False and again["version"] == 1,
              f"inserted={again['inserted']} version={again['version']}")

        # ⑩ 不可变 + 重开无损
        table_rows = store.conn.execute(
            "SELECT COUNT(*) AS n FROM rule_assets").fetchone()["n"]
        chain_len = sum(
            len(store.list_asset_revisions(a)) for a in (_PROMPT_ID, DEFAULT_FEWSHOT_ASSET_ID))
        store.close()
        main_store_closed = True
        reopened = bootstrap_store(data_dir)
        try:
            reopened_chain = [r.version for r in reopened.list_asset_revisions(_PROMPT_ID)]
            reopened_seal = reopened.get_asset_revision(
                DEFAULT_FEWSHOT_ASSET_ID, 1).content["content_seal"]
        finally:
            reopened.close()
        check("⑩ 只 INSERT 不可变且重开版本链无损",
              table_rows == chain_len == 3 and reopened_chain == [1, 2]
              and reopened_seal == fewshot.content["content_seal"],
              f"rows={table_rows} chain={chain_len} reopened={reopened_chain}")

        # ⑪ 空样本回流拒入（新容器，无偏好样本）
        config2 = load_profile(PROFILE)
        config2.paths["data_dir"] = str(work / "data-empty")
        empty_container = build_container(config2, SUBSYSTEMS)
        empty_rejected = False
        try:
            promote_preferences_to_fewshot(empty_container)
        except AssetValidationError:
            empty_rejected = True
        finally:
            empty_container.get("store").close()
        check("⑪ 空样本回流拒绝不造空资产", empty_rejected, f"rejected={empty_rejected}")
    finally:
        if not main_store_closed:
            store.close()

    passed = sum(1 for c in checks if c["passed"] == "✅")
    report_doc = {
        "probe": "asset_probe",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "workdir": str(work),
        "summary": {"passed": passed, "failed": len(failures), "total": len(checks)},
        "checks": checks,
    }
    (EVAL_DIR / "asset-probe.json").write_text(
        json.dumps(report_doc, ensure_ascii=False, indent=2), encoding="utf-8")
    md_lines = [
        "# S2.6a L3 规则资产版本化探针报告（asset_probe）",
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
    (EVAL_DIR / "asset-probe.md").write_text("\n".join(md_lines) + "\n", encoding="utf-8")

    for c in checks:
        print(f"{c['passed']} {c['name']} — {c['detail']}")
    print(f"报告：{EVAL_DIR / 'asset-probe.json'}")
    if failures:
        print("❌ asset_probe 存在失败检查", file=sys.stderr)
        return 1
    print("✅ asset_probe 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
