#!/usr/bin/env python3
"""S0 门禁冒烟：发一条"空链路"trace 并验证它真的落盘/上报（docs/07 §10）。

默认 profile（noop tracer）：
    .venv/bin/python scripts/smoke_trace.py
只验证 span 调用面可用，打印 {"verified": true, "tracer": "noop"}。

Langfuse（先 docker compose --profile observability up -d，并在 UI 建好 project）：
    LANGFUSE_PUBLIC_KEY=pk-lf-... LANGFUSE_SECRET_KEY=sk-lf-... \\
    TESTPILOT_TRACER=langfuse \\
    .venv/bin/python scripts/smoke_trace.py --expect langfuse
脚本会轮询 Langfuse traces API，按 sessionId=run_id 确认 trace 已可见，
证明"空链路 trace 出现在 Langfuse"，而不是只信 SDK flush 成功。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

SUBSYSTEMS = Path(__file__).resolve().parents[1]

from testpilot_orchestrator.config_loader import load_profile
from testpilot_orchestrator.factory import build_container


def _trace_visible(host: str, pk: str, sk: str, run_id: str, *, timeout_s: float = 30.0) -> bool:
    """轮询 Langfuse traces 查询 API，按 sessionId 确认 trace 已可检索。"""
    url = f"{host.rstrip('/')}/api/public/traces?sessionId={run_id}"
    token = base64.b64encode(f"{pk}:{sk}".encode()).decode()
    deadline = time.monotonic() + timeout_s
    last_err = ""
    while time.monotonic() < deadline:
        req = urllib.request.Request(url, headers={"Authorization": f"Basic {token}"})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode())
            if data.get("data"):
                return True
        except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
            last_err = str(exc)
        time.sleep(2)
    print(f"[smoke] 30s 内未在 Langfuse 查询到 trace（last_error={last_err}）", file=sys.stderr)
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default=str(SUBSYSTEMS / "config" / "profiles" / "offline-demo.yaml"))
    ap.add_argument("--expect", choices=["noop", "langfuse"], default="noop",
                    help="noop：只验证调用面；langfuse：要求 trace 在 Langfuse 可查询")
    args = ap.parse_args()

    config = load_profile(args.profile)
    container = build_container(config, SUBSYSTEMS)
    try:
        tracer = container.adapters["tracer"]
        tracer_name = type(tracer).__name__
        run_id = f"smoke-{uuid.uuid4()}"

        if args.expect == "langfuse" and tracer_name != "LangfuseTracer":
            print(f"[smoke] --expect langfuse 但实际装配 {tracer_name}（检查 TESTPILOT_TRACER 与 profile）",
                  file=sys.stderr)
            return 2

        with tracer.span("smoke.empty_chain", run_id=run_id, metadata={"gate": "S0"}):
            pass  # 空链路：没有节点，只验证观测通路

        if tracer_name == "NoopTracer":
            print(json.dumps({"verified": True, "tracer": "noop", "run_id": run_id}, ensure_ascii=False))
            return 0

        host = os.environ.get("LANGFUSE_HOST", "http://localhost:3030")
        pk = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
        sk = os.environ.get("LANGFUSE_SECRET_KEY", "")
        ok = _trace_visible(host, pk, sk, run_id)
        print(json.dumps({"verified": ok, "tracer": "langfuse", "run_id": run_id, "host": host},
                         ensure_ascii=False))
        return 0 if ok else 1
    finally:
        container.adapters["store"].close()


if __name__ == "__main__":
    raise SystemExit(main())
