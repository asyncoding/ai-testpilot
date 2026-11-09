#!/usr/bin/env bash
# 自动门禁一键验收（docs/07 §10）：A1–A6 为 S0 骨架门禁，A7 为 S1.4 离线问答探针，
# A8 为 S1.5 问答 HTTP API 冒烟（真实 uvicorn + curl），A9 为 S1.6 离线评测探针
# （golden smoke 题集 + 录制模型，确定性验证评测 runner 全链路），A10 为 S2.1
# 用例文档连接器探针（合成夹具全链路入库，含损坏行死信与取消/缺预期标记），
# A11 为 S2.2 需求骨架抽取探针（合成夹具 + 录制模型 AC↔FR 链接，白名单 fail-closed），
# A12 为 S2.3 测试点生成探针（确定性脊柱 13 点 + 录制模型补点，2 投毒行进 tp-gen 死信）。
# Langfuse 实链门禁需另跑 smoke_trace --expect langfuse。
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
a4_dead=0
# 就绪轮询同时检测进程存活：端口被遗留进程占用时新进程会立即退出，
# 否则 curl 会打到旧进程造成「假绿」（2026-11-06 A8 排障实证）
for _ in $(seq 1 20); do
  kill -0 $APID 2>/dev/null || { a4_dead=1; break; }
  curl -sf http://127.0.0.1:8765/health -o /tmp/tp_health.json && break
  sleep 0.5
done
kill $APID 2>/dev/null; wait $APID 2>/dev/null
if [ $a4_dead = 0 ] && [ -f /tmp/tp_health.json ] && grep -q '"status":"ok"' /tmp/tp_health.json && grep -q '"external_write_enabled":false' /tmp/tp_health.json; then
  echo "PASS  $(cat /tmp/tp_health.json)"; pass_n=$((pass_n+1))
else echo "FAIL  uvicorn 未存活或健康检查失败（端口占用？lsof -nP -iTCP:8765），见 /tmp/tp_api.log"; fail_n=$((fail_n+1)); fi

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

echo "=== A7. 离线问答端到端探针（FR-QA-001～004，四类问题+行数对账，期望 exit 0） ==="
# 显式钉死 profile：探针内部 full=True 重切块，重复执行幂等；账本行数按差值断言，
# 不依赖库的初始行数。生成物落 store/offline-demo 与 checkpoints/，均已 gitignore。
TESTPILOT_PROFILE=config/profiles/offline-demo.yaml \
  $PY scripts/qa_probe.py > /tmp/tp_qa_probe.log 2>&1 \
  && grep -q '✅ qa_probe 全部通过' /tmp/tp_qa_probe.log \
  && { echo "PASS  $(grep '✅' /tmp/tp_qa_probe.log)"; pass_n=$((pass_n+1)); } \
  || { echo "FAIL  见 /tmp/tp_qa_probe.log"; fail_n=$((fail_n+1)); }

echo "=== A8. 问答 HTTP API 冒烟（S1.5：ask/幽灵反馈 404/config，真实 uvicorn + curl） ==="
# A7 探针已完成入库，这里只验 HTTP 契约层；web/out 缺失时静态挂载跳过，API 不受影响。
# 不把 node 构建放进自动门禁：npm 不可用不能拖垮 Python 链路（前端见 scripts/build_web.sh）。
$PY -m uvicorn testpilot_orchestrator.app:app --port 8765 >/tmp/tp_api8.log 2>&1 &
APID=$!
ok=1
a8_dead=0
for _ in $(seq 1 20); do
  kill -0 $APID 2>/dev/null || { a8_dead=1; break; }
  curl -sf http://127.0.0.1:8765/health -o /dev/null && break
  sleep 0.5
done
[ $a8_dead = 0 ] || ok=0
# ① chitchat 预设 → unknown/out_of_scope（与 A7 探针同故事，验 HTTP 序列化面）
curl -sf -X POST http://127.0.0.1:8765/api/qa/ask -H 'Content-Type: application/json' \
  -d '{"question":"你好，讲个笑话","project":"AlarmKitSpike-demo","branch":"alarmkit-spike","case_key":"chitchat"}' \
  -o /tmp/tp_a8_ask.json || ok=0
grep -q '"status":"unknown"' /tmp/tp_a8_ask.json && \
  grep -q '"unknown_reason":"out_of_scope"' /tmp/tp_a8_ask.json || ok=0
# ② 幽灵 run 反馈必须 HTTP 404 且错误体带类型名（不留孤儿行由 A2 单测/行数差守卫）
code=$(curl -s -o /tmp/tp_a8_fb.json -w '%{http_code}' -X POST \
  http://127.0.0.1:8765/api/qa/runs/run-ghost-a8/feedback \
  -H 'Content-Type: application/json' -d '{"kind":"useful"}')
[ "$code" = "404" ] || ok=0
grep -q 'NotFoundError' /tmp/tp_a8_fb.json || ok=0
# ③ /api/config：前端录制夹具横幅的数据源
curl -sf http://127.0.0.1:8765/api/config -o /tmp/tp_a8_cfg.json || ok=0
grep -q '"profile":"offline-demo"' /tmp/tp_a8_cfg.json && \
  grep -q '"recorded":true' /tmp/tp_a8_cfg.json || ok=0
kill $APID 2>/dev/null; wait $APID 2>/dev/null
[ $ok = 1 ] && { echo "PASS  ask=unknown/out_of_scope, ghost feedback=404, config=offline-demo/recorded"; pass_n=$((pass_n+1)); } \
  || { echo "FAIL  见 /tmp/tp_api8.log 与 /tmp/tp_a8_*.json"; fail_n=$((fail_n+1)); }

echo "=== A9. 离线评测探针（FR-EV-001～003，golden smoke 4 题+裁判/落库/报告，期望 exit 0） ==="
# 机控题集只验证评测链路装配；S1 门禁数字由 ollama hermetic 形态跑 golden-s1-v1 产出，
# 不进自动门禁（需 docker chroma + ollama serve，见 README「S1.6 评测」节）。
TESTPILOT_PROFILE=config/profiles/offline-demo.yaml \
  $PY scripts/eval_probe.py > /tmp/tp_eval_probe.log 2>&1 \
  && grep -q '✅ eval_probe recorded 全部通过' /tmp/tp_eval_probe.log \
  && { echo "PASS  $(grep '通过' /tmp/tp_eval_probe.log | tail -1)"; pass_n=$((pass_n+1)); } \
  || { echo "FAIL  见 /tmp/tp_eval_probe.log"; fail_n=$((fail_n+1)); }

echo "=== A10. 用例文档连接器（S2.1 FR-CON-003：合成夹具 --full 入库，partial 形状确定性断言） ==="
# 夹具故意含 1 条列数损坏行，worker 按设计 exit 1；门禁断言 JSON 形状而非退出码。
# --full 强制整组重建，避免增量跳过导致 cases_upserted 不确定。
TESTPILOT_PROFILE=config/profiles/offline-demo.yaml \
  $PY -m testpilot_workers.ingest_cases --full > /tmp/tp_cases_a10.log 2>&1 || true
if grep -q '"status": "partial"' /tmp/tp_cases_a10.log \
  && grep -q '"cases_upserted": 7' /tmp/tp_cases_a10.log \
  && grep -q '"parse_dead_letters": 1' /tmp/tp_cases_a10.log \
  && grep -q '"cancelled": 2' /tmp/tp_cases_a10.log \
  && grep -q '"oracle_missing": 1' /tmp/tp_cases_a10.log; then
  echo "PASS  7 用例入库 / 1 死信 / 2 取消 / 1 缺预期（FR-CON-003 双格式+隔离全链路）"
  pass_n=$((pass_n+1))
else
  echo "FAIL  见 /tmp/tp_cases_a10.log"; fail_n=$((fail_n+1))
fi

echo "=== A11. 需求骨架抽取（S2.2 FR-TD-001：合成夹具 --full，确定性解析+录制模型链接） ==="
# 夹具故意含 1 条列数损坏行，worker 按设计 exit 1；门禁断言 JSON 形状而非退出码。
# offline profile 带录制模型且含 req_link 夹具键：模型补链后 ac_missing 3→1（仅 FR-3）。
TESTPILOT_PROFILE=config/profiles/offline-demo.yaml \
  $PY -m testpilot_workers.ingest_requirements --full > /tmp/tp_reqs_a11.log 2>&1 || true
if grep -q '"status": "partial"' /tmp/tp_reqs_a11.log \
  && grep -q '"features_active": 6' /tmp/tp_reqs_a11.log \
  && grep -q '"features_cancelled": 1' /tmp/tp_reqs_a11.log \
  && grep -q '"rules": 2' /tmp/tp_reqs_a11.log \
  && grep -q '"acceptance_criteria": 2' /tmp/tp_reqs_a11.log \
  && grep -q '"ac_missing": 1' /tmp/tp_reqs_a11.log \
  && grep -q '"model_linked_docs": 1' /tmp/tp_reqs_a11.log \
  && grep -q '"link_degraded_docs": 0' /tmp/tp_reqs_a11.log \
  && grep -q '"parse_dead_letters": 1' /tmp/tp_reqs_a11.log; then
  echo "PASS  6 活跃需求/1 取消/2 规则/2 AC，模型补链后仅 1 缺 AC（FR-CON 白名单链路）"
  pass_n=$((pass_n+1))
else
  echo "FAIL  见 /tmp/tp_reqs_a11.log"; fail_n=$((fail_n+1))
fi

echo "=== A12. 测试点生成（S2.3 FR-TD-002：A11 骨架为前置，确定性脊柱+录制模型白名单补点） ==="
# 前置依赖：A11 已把合成需求骨架写入同一 offline 库；worker 对空骨架会直接 ValueError。
# 录制 tp_gen 夹具含 6 行：3 合法补点 + 2 投毒（unknown_fr/unknown_source）+ 1 重复去重。
# 不断言证据计数：FTS 语料状态随 A5/A7 变化，检索降级只计数不阻断（单测已钉死该口径）。
TESTPILOT_PROFILE=config/profiles/offline-demo.yaml \
  $PY -m testpilot_workers.generate_testpoints --full > /tmp/tp_tps_a12.log 2>&1 || true
if grep -q '"status": "succeeded"' /tmp/tp_tps_a12.log \
  && grep -q '"docs_rebuilt": 1' /tmp/tp_tps_a12.log \
  && grep -q '"points_total": 16' /tmp/tp_tps_a12.log \
  && grep -q '"spine_points": 13' /tmp/tp_tps_a12.log \
  && grep -q '"model_points": 3' /tmp/tp_tps_a12.log \
  && grep -q '"model_rejected": 2' /tmp/tp_tps_a12.log \
  && grep -q '"model_deduped": 1' /tmp/tp_tps_a12.log \
  && grep -q '"hallucination_rate": 0.4' /tmp/tp_tps_a12.log \
  && grep -q '"degraded_docs": 0' /tmp/tp_tps_a12.log; then
  echo "PASS  16 测试点（13 脊柱+3 模型）/2 幻觉拒入死信/1 去重，幻觉率 0.4 如实入账"
  pass_n=$((pass_n+1))
else
  echo "FAIL  见 /tmp/tp_tps_a12.log"; fail_n=$((fail_n+1))
fi

echo
echo "自动门禁结果：$pass_n PASS / $fail_n FAIL（A1–A6 S0 / A7 S1.4 / A8 S1.5 / A9 S1.6 / A10 S2.1 / A11 S2.2 / A12 S2.3）"
[ $fail_n = 0 ] || exit 1
echo "剩余手动门禁（Langfuse 服务端可见 trace、Web 对话页构建）按 subsystems/README.md「手动门禁」节执行。"
