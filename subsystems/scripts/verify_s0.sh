#!/usr/bin/env bash
# S0 已闭合门禁一键验收（docs/07 §10）。Langfuse 实链门禁需另跑 smoke_trace --expect langfuse。
# 用法：cd subsystems && bash scripts/verify_s0.sh
set -u
cd "$(dirname "$0")/.."
PY=.venv/bin/python
# 门禁必须确定性运行：屏蔽调用方 shell 残留的 TESTPILOT_*/LANGFUSE_* 覆盖
# （如手工跑第 7 门禁时 export 的 TESTPILOT_TRACER=langfuse，会让 A2 装出
# LangfuseTracer、A6 在无 key 时 401）。Langfuse 实链是独立的手动步骤，不在这里。
unset TESTPILOT_TRACER TESTPILOT_REPO_PATH TESTPILOT_OLLAMA_URL TESTPILOT_CHROMA_URL \
      LANGFUSE_PUBLIC_KEY LANGFUSE_SECRET_KEY
pass_n=0; fail_n=0

echo "=== A1. venv 与依赖（uv sync 后四个包必须可 import） ==="
[ -x "$PY" ] || { echo "FAIL  venv 不存在，先跑: uv sync"; exit 1; }
$PY -c "import testpilot_core,testpilot_adapters,testpilot_orchestrator,testpilot_workers" \
  && { echo PASS; pass_n=$((pass_n+1)); } || { echo "FAIL  空壳 editable，修复见 README（uv sync --reinstall-package ...）"; fail_n=$((fail_n+1)); }

echo "=== A2. 全量契约测试（期望 0 failed；Chroma 未起时 3 例集成测试自动 skip） ==="
$PY -m pytest -q > /tmp/tp_pytest.log 2>&1 && { echo "PASS  $(tail -1 /tmp/tp_pytest.log)"; pass_n=$((pass_n+1)); } || { echo "FAIL  见 /tmp/tp_pytest.log"; fail_n=$((fail_n+1)); }

echo "=== A3. core 防腐门禁（禁止 IO/SDK/兄弟包） ==="
$PY scripts/import_lint.py > /tmp/tp_lint.log 2>&1 && { echo "PASS  $(cat /tmp/tp_lint.log)"; pass_n=$((pass_n+1)); } || { echo "FAIL  见 /tmp/tp_lint.log"; fail_n=$((fail_n+1)); }

echo "=== A4. API /health（FastAPI 真实起进程，curl 200） ==="
$PY -m uvicorn testpilot_orchestrator.app:app --port 8765 >/tmp/tp_api.log 2>&1 &
APID=$!
for _ in $(seq 1 20); do curl -sf http://127.0.0.1:8765/health -o /tmp/tp_health.json && break; sleep 0.5; done
kill $APID 2>/dev/null; wait $APID 2>/dev/null
if [ -f /tmp/tp_health.json ] && grep -q '"status":"ok"' /tmp/tp_health.json && grep -q '"external_write_enabled":false' /tmp/tp_health.json; then
  echo "PASS  $(cat /tmp/tp_health.json)"; pass_n=$((pass_n+1))
else echo "FAIL  见 /tmp/tp_api.log"; fail_n=$((fail_n+1)); fi

echo "=== A5. 三个 worker 独立进程装配自检 ==="
ok=1
for w in testpilot-ingest testpilot-runner testpilot-eval; do
  .venv/bin/$w >/tmp/tp_$w.log 2>&1 || ok=0
  grep -q ready /tmp/tp_$w.log || ok=0
done
[ $ok = 1 ] && { echo "PASS  ingest/runner/eval 均 ready"; pass_n=$((pass_n+1)); } || { echo FAIL; fail_n=$((fail_n+1)); }

echo "=== A6. trace 调用面（noop 空链路，期望 verified=true, exit=0） ==="
$PY scripts/smoke_trace.py > /tmp/tp_smoke.log 2>&1 && grep -q '"verified": true' /tmp/tp_smoke.log \
  && { echo "PASS  $(cat /tmp/tp_smoke.log)"; pass_n=$((pass_n+1)); } || { echo "FAIL  见 /tmp/tp_smoke.log"; fail_n=$((fail_n+1)); }

echo
echo "S0 自动门禁结果：$pass_n PASS / $fail_n FAIL"
[ $fail_n = 0 ] || exit 1
echo "剩余唯一门禁（Langfuse 服务端可见 trace）按 subsystems/README.md「S0 门禁」节手动执行。"
