#!/usr/bin/env bash
# 自动门禁一键验收（docs/07 §10）：A1–A6 为 S0 骨架门禁，A7 为 S1.4 离线问答探针，
# A8 为 S1.5 问答 HTTP API 冒烟（真实 uvicorn + curl），A9 为 S1.6 离线评测探针
# （golden smoke 题集 + 录制模型，确定性验证评测 runner 全链路），A10 为 S2.1
# 用例文档连接器探针（合成夹具全链路入库，含损坏行死信与取消/缺预期标记），
# A11 为 S2.2 需求骨架抽取探针（合成夹具 + 录制模型 AC↔FR 链接，白名单 fail-closed），
# A12 为 S2.3 测试点生成探针（确定性脊柱 13 点 + 录制模型补点，2 投毒行进 tp-gen 死信），
# A13 为 S2.4 oracle 四源挖掘探针（确定性三源 12 草稿 + 录制模型补 TC-N2，2 投毒行进
# oracle-mine 死信；v11 oracle_suggestions 草稿表不碰正式用例库），A14 为 S2.5
# 人审回写探针（tmp 库 hermetic：kill switch 拦截→开启→dispatch 落盘→解析器
# round-trip→重放幂等→AC1 正式库零变更，全程不起端口、不碰 store/offline-demo），
# A15 为 S2.6a L3 规则资产版本化探针（tmp 库 hermetic：不可变版本链/同内容幂等/
# kind 不可变/字段+行级 diff/偏好样本回流 few-shot，不起端口、不碰 store/offline-demo），
# A16 为 S2.6b 用例↔符号关系探针（tmp 库 hermetic：四源闭集派生 precision/recall=1.0
# 钉 AC3≥.85、低置信阈值、证据 dossier fail-closed、scope 隔离与幽灵清扫，不起端口），
# A17 为 S2.6c 反馈/bad case 闭环探针（tmp 库 hermetic：负反馈投影/幂等巡检/状态机
# fail-closed/golden 溯源/幽灵回归拒入/闭环统计/v13→v15 迁移，不起端口），
# A18 为 S2.6d S2 五门禁总验探针（合成夹具 hermetic：专家清单先封存再跑管线，
# 结构合法/覆盖≥.75/P0P1 漏点=0/幻觉≤.05/oracle 有效率≥.7 五门齐过，探索性用例
# 正常出口、重放幂等、盲序守卫活体，纯 core 确定性链不起端口），
# A19 为 S3.1 测试执行与 runner-worker 探针（tmp 库 hermetic：先账本后队列/幂等
# 重放/重跑配额拒入零幻影/协作取消/超时收口/租约 requeue 与死信/junit-xml 工厂
# 装配 Surefire 三态/v15 持久化，不起端口、不碰 store/offline-demo），
# A20 为 S3.2 iOS xcode 适配器与环境租约探针（FakeExecutor/FakeSpawner hermetic：
# argv 逐字对账/resultBundle 预清理/真机能力拒绝不硬跑/先租后跑透传 udid/过期租约
# shutdown 回收/reset 守卫与 shutdown→erase 顺序/cancel 杀进程/超时释放/fail-closed，
# 零 xcodebuild 零 simctl、绝不 erase 用户设备、不起端口），
# A21 为 S3.3 xcresult 制品与 os_log 探针（FakeExecutor hermetic：用例树展平/
# 附件 localfs URN 回链/畸形与缺件 fail-closed/NDJSON 时间窗过滤/坏行计数/
# query 回显可复现/ffmpeg 白名单/工厂 os-log 装配，零 xcresulttool 零 simctl
# 零 ffmpeg、不碰用户设备、不起端口），
# A22 为 S3.4 失败归因探针（纯 hermetic：五候选规则/AC1 四历史原型/不对称护栏/
# needs_human 三场景/40 例盲标混淆矩阵四门禁/误误导口径/组包服务/封存哈希，
# 零端口零网络零真机状态），
# A23 为 S3.5 S3 五门禁总验探针（tempfile 临时库走生产装配：40 例盲标先封存再
# 评测/执行成功率结构化产出率且 error/timeout 单列/归因三指标/证据充分率/提交取消
# 预算四路幂等零增生/配额拒入零幻影/红证对照证门禁有牙/空清单守卫/v15 重开无损，
# 不起端口、不碰 store/offline-demo、零真机零模拟器），
# A24 为 S4 四门禁总验探针（tempfile 临时库 hermetic：摄入延迟两段 P95 合成时钟/
# stale 默认通道零命中与 include_stale 审计对照/高价值 reindex 批次实测 <10min/
# 影响推荐精度 ≥.8 与空分母纪律/CI 闭环重放零增生与草稿 dry-run/定时兜底 scheduled
# 留痕与 CLI 挂接点/治理 reindex 存活/红证 301s·601s·0.5 精度咬门/v15 重开无损，
# 不起端口、不碰 store/offline-demo、零真机零模拟器）。
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

echo "=== A13. oracle 四源挖掘（S2.4 FR-TD-003：A10/A11 为前置，确定性三源+录制模型白名单补点） ==="
# 前置依赖：A10 用例（7 入库/2 取消/1 缺预期）与 A11 骨架在同一 offline 库。
# 录制 oracle_mine 夹具含 4 行：2 合法补点 + 2 投毒（bad_type/unknown_source）。
# 不断言证据计数：FTS 语料状态随 A5/A7/A9 变化，检索降级只计数不阻断（单测已钉死该口径）。
TESTPILOT_PROFILE=config/profiles/offline-demo.yaml \
  $PY -m testpilot_workers.mine_oracles --full > /tmp/tp_oracle_a13.log 2>&1 || true
if grep -q '"status": "succeeded"' /tmp/tp_oracle_a13.log \
  && grep -q '"cases_seen": 7' /tmp/tp_oracle_a13.log \
  && grep -q '"cases_rebuilt": 5' /tmp/tp_oracle_a13.log \
  && grep -q '"cases_cancelled": 2' /tmp/tp_oracle_a13.log \
  && grep -q '"suggestions_total": 14' /tmp/tp_oracle_a13.log \
  && grep -q '"deterministic_oracles": 12' /tmp/tp_oracle_a13.log \
  && grep -q '"model_oracles": 2' /tmp/tp_oracle_a13.log \
  && grep -q '"model_rejected": 2' /tmp/tp_oracle_a13.log \
  && grep -q '"model_calls": 1' /tmp/tp_oracle_a13.log \
  && grep -q '"hallucination_rate": 0.5' /tmp/tp_oracle_a13.log \
  && grep -q '"degraded_cases": 0' /tmp/tp_oracle_a13.log; then
  echo "PASS  14 草稿（12 确定性+2 模型）/2 幻觉拒入 oracle-mine 死信/取消 2 用例不挖掘"
  pass_n=$((pass_n+1))
else
  echo "FAIL  见 /tmp/tp_oracle_a13.log"; fail_n=$((fail_n+1))
fi

echo "=== A14. 人审回写（S2.5 FR-TD-004/WR-001：tmp 库 hermetic 全流程，不起端口、不碰 offline 库） ==="
# 探针内部用 tempfile 工作目录 + 独立写根，重复执行幂等；报告与样例落 out/eval（gitignore）。
$PY scripts/approval_probe.py > /tmp/tp_approval_a14.log 2>&1 \
  && grep -q '✅ approval_probe 全部通过' /tmp/tp_approval_a14.log \
  && grep -q '③ AC1 正式 test_cases 零触碰' /tmp/tp_approval_a14.log \
  && { echo "PASS  $(grep -c '✅' /tmp/tp_approval_a14.log) 项检查全绿（AC1 零触碰/blocked→confirmed/round-trip/重放幂等）"; pass_n=$((pass_n+1)); } \
  || { echo "FAIL  见 /tmp/tp_approval_a14.log 与 out/eval/approval-probe.json"; fail_n=$((fail_n+1)); }

echo "=== A15. L3 规则资产版本化（S2.6a FR-KNO-002：tmp 库 hermetic，版本链/diff/偏好回流，不起端口） ==="
# 探针内部 tempfile 工作目录；rule_assets 只 INSERT 不可变，同内容重放幂等，报告落 out/eval（gitignore）。
$PY scripts/asset_probe.py > /tmp/tp_asset_a15.log 2>&1 \
  && grep -q '✅ asset_probe 全部通过' /tmp/tp_asset_a15.log \
  && grep -q '⑦ 偏好回流 few_shot 报告键齐全' /tmp/tp_asset_a15.log \
  && { echo "PASS  $(grep -c '✅' /tmp/tp_asset_a15.log) 项检查全绿（版本单调/幂等/kind 不可变/diff/回流/seal/重开无损）"; pass_n=$((pass_n+1)); } \
  || { echo "FAIL  见 /tmp/tp_asset_a15.log 与 out/eval/asset-probe.json"; fail_n=$((fail_n+1)); }

echo "=== A16. 用例↔符号关系（S2.6b FR-ING-004：tmp 库 hermetic，闭集派生 AC3≥.85/证据/scope 隔离，不起端口） ==="
# 探针内部 tempfile 工作目录 + 生产装配路径，独立手写 ground truth 三元组对账；
# 报告落 out/eval/relation-probe.{json,md}（gitignore），重复执行幂等。
$PY scripts/relation_probe.py > /tmp/tp_relation_a16.log 2>&1 \
  && grep -q '✅ relation_probe 全部通过' /tmp/tp_relation_a16.log \
  && grep -q '③ AC3 precision/recall 过门且确定性构建无错配' /tmp/tp_relation_a16.log \
  && { echo "PASS  $(grep -c '✅' /tmp/tp_relation_a16.log) 项检查全绿（precision/recall=1.0/低置信/dossier/scope 隔离/幽灵清扫/重开无损）"; pass_n=$((pass_n+1)); } \
  || { echo "FAIL  见 /tmp/tp_relation_a16.log 与 out/eval/relation-probe.json"; fail_n=$((fail_n+1)); }

echo "=== A17. 反馈/bad case 闭环（S2.6c FR-EV-005：tmp 库 hermetic，投影/幂等/状态机/golden/回归/统计/v15 迁移，不起端口） ==="
# 探针内部 tempfile 工作目录 + 生产装配路径，schema v15 bad_cases 表内容寻址幂等；
# 报告落 out/eval/badcase-probe.{json,md}（gitignore），重复执行幂等。
$PY scripts/badcase_probe.py > /tmp/tp_badcase_a17.log 2>&1 \
  && grep -q '✅ badcase_probe 全部通过' /tmp/tp_badcase_a17.log \
  && grep -q '⑤ 全生命周期：golden 冻结/幽灵拒入/fail 不闭环/pass 闭环' /tmp/tp_badcase_a17.log \
  && { echo "PASS  $(grep -c '✅' /tmp/tp_badcase_a17.log) 项检查全绿（投影/幂等/内容寻址/生命周期/fail-closed/零幻影/统计/v15 迁移/重开无损）"; pass_n=$((pass_n+1)); } \
  || { echo "FAIL  见 /tmp/tp_badcase_a17.log 与 out/eval/badcase-probe.json"; fail_n=$((fail_n+1)); }

echo "=== A18. S2 五门禁总验（S2.6d：合成夹具 hermetic，结构合法/覆盖/P0P1 漏点/幻觉/oracle 有效率，不起端口） ==="
# 专家清单先封存再跑管线（封存副本落 out/eval，盲序留痕），core 纯函数全链确定性；
# 报告落 out/eval/s2-gate-probe.{json,md}（gitignore），重复执行幂等。
$PY scripts/s2_gate_probe.py > /tmp/tp_s2gate_a18.log 2>&1 \
  && grep -q '✅ s2_gate_probe 全部通过' /tmp/tp_s2gate_a18.log \
  && grep -q '⑤ 盲标四门禁：覆盖/精确率/P0P1 漏点/幻觉全部过门' /tmp/tp_s2gate_a18.log \
  && { echo "PASS  $(grep -c '✅' /tmp/tp_s2gate_a18.log) 项检查全绿（盲序封存/结构合法/脊柱 11 点/盲标四门/oracle 有效率/探索性出口/重放幂等/守卫活体）"; pass_n=$((pass_n+1)); } \
  || { echo "FAIL  见 /tmp/tp_s2gate_a18.log 与 out/eval/s2-gate-probe.json"; fail_n=$((fail_n+1)); }

echo "=== A19. 测试执行与 runner-worker（S3.1 FR-EX-001/002：tmp 库 hermetic，账本/幂等/配额/取消/超时/租约回收/junit-xml 装配，不起端口） ==="
# 生产装配路径（load_profile→build_container→execution_service），临时库不碰 offline-demo；
# 报告落 out/eval/execution-probe.{json,md}（gitignore），重复执行幂等。
$PY scripts/execution_probe.py > /tmp/tp_exec_a19.log 2>&1 \
  && grep -q '✅ execution_probe 全部通过' /tmp/tp_exec_a19.log \
  && grep -q '⑧ attempts 顶格过期任务死信' /tmp/tp_exec_a19.log \
  && { echo "PASS  $(grep -c '✅' /tmp/tp_exec_a19.log) 项检查全绿（空队列/幂等/落账/配额拒入/取消/超时/requeue/死信/junit 装配/持久化 v15）"; pass_n=$((pass_n+1)); } \
  || { echo "FAIL  见 /tmp/tp_exec_a19.log 与 out/eval/execution-probe.json"; fail_n=$((fail_n+1)); }

echo "=== A20. iOS xcode 适配器与环境租约（S3.2 FR-EX-003/004：FakeExecutor hermetic，argv 对账/预清理/能力拒绝/租约回收/reset 顺序/cancel/超时释放，零真机零模拟器） ==="
# 进程出口全部换假件：不跑 xcodebuild/simctl、不 erase 用户设备、不起端口；
# 报告落 out/eval/xcode-adapter-probe.{json,md}（gitignore），重复执行幂等。
$PY scripts/xcode_adapter_probe.py > /tmp/tp_xcode_a20.log 2>&1 \
  && grep -q '✅ xcode_adapter_probe 全部通过' /tmp/tp_xcode_a20.log \
  && grep -q '⑦ reset：持租约守卫零命令' /tmp/tp_xcode_a20.log \
  && { echo "PASS  $(grep -c '✅' /tmp/tp_xcode_a20.log) 项检查全绿（argv 对账/工厂装配/预清理/cancel/能力拒绝/先租后跑/租约回收/reset 守卫/超时释放/fail-closed）"; pass_n=$((pass_n+1)); } \
  || { echo "FAIL  见 /tmp/tp_xcode_a20.log 与 out/eval/xcode-adapter-probe.json"; fail_n=$((fail_n+1)); }

echo
echo "=== A21. xcresult 制品与 os_log 查询（S3.3 FR-EX-005/006：假执行器 hermetic，树展平/附件 URN 回链/坏行 fail-closed/时间窗 AC2/query 可复现/ffmpeg 白名单/工厂装配，零真机零模拟器） ==="
# 进程出口全部换假件：不跑 xcresulttool/simctl/ffmpeg、不碰用户设备、不起端口；
# 报告落 out/eval/xcresult-oslog-probe.{json,md}（gitignore），重复执行幂等。
$PY scripts/xcresult_oslog_probe.py > /tmp/tp_xcresult_a21.log 2>&1 \
  && grep -q '✅ xcresult_oslog_probe 全部通过' /tmp/tp_xcresult_a21.log \
  && grep -q '⑦ NDJSON 窗内 3/窗外 2/坏行 1' /tmp/tp_xcresult_a21.log \
  && { echo "PASS  $(grep -c '✅' /tmp/tp_xcresult_a21.log) 项检查全绿（树解析/畸形守卫/附件分型/URN 回链/响亮失败/argv 对账/时间窗/坏行封顶/ffmpeg/工厂）"; pass_n=$((pass_n+1)); } \
  || { echo "FAIL  见 /tmp/tp_xcresult_a21.log 与 out/eval/xcresult-oslog-probe.json"; fail_n=$((fail_n+1)); }

echo
echo "=== A22. 失败归因（S3.4 FR-EX-007：纯 hermetic，五候选/AC1 四原型/护栏/needs_human/40 例混淆矩阵四门禁，不起端口） ==="
# 纯 core/workers 确定性链：盲标夹具封存后跑引擎，零端口零网络零真机；
# 报告落 out/eval/attribution-probe.{json,md}（gitignore），重复执行幂等。
$PY scripts/attribution_probe.py > /tmp/tp_attribution_a22.log 2>&1 \
  && grep -q '✅ attribution_probe 全部通过' /tmp/tp_attribution_a22.log \
  && grep -q '⑨ 40 例盲标四门禁全过' /tmp/tp_attribution_a22.log \
  && { echo "PASS  $(grep -c '✅' /tmp/tp_attribution_a22.log) 项检查全绿（四原型/产品改动分界/护栏/needs_human/矩阵四门/误误导口径/组包/封存）"; pass_n=$((pass_n+1)); } \
  || { echo "FAIL  见 /tmp/tp_attribution_a22.log 与 out/eval/attribution-probe.json"; fail_n=$((fail_n+1)); }

echo
echo "=== A23. S3 五门禁总验（S3.5：tempfile 临时库生产装配 hermetic，执行成功率/归因三指标/证据充分率/幂等/重跑预算，不起端口不触真机） ==="
# 盲标先封存再评测（封存副本落 out/eval，盲序留痕），执行段走 load_profile→build_container
# 但 data_dir 指向 tempfile，不碰 offline-demo；报告落 out/eval/s3-gate-probe.{json,md}（gitignore）。
$PY scripts/s3_gate_probe.py > /tmp/tp_s3gate_a23.log 2>&1 \
  && grep -q '✅ s3_gate_probe 全部通过' /tmp/tp_s3gate_a23.log \
  && grep -q '门禁① 执行成功率' /tmp/tp_s3gate_a23.log \
  && { echo "PASS  $(grep -c '✅' /tmp/tp_s3gate_a23.log) 项检查全绿（盲序封存/归因三门/证据充分/红证对照/空清单守卫/重放幂等/结构化产出率/装配归因/四路幂等/预算零幻影/异常单列/v15）"; pass_n=$((pass_n+1)); } \
  || { echo "FAIL  见 /tmp/tp_s3gate_a23.log 与 out/eval/s3-gate-probe.json"; fail_n=$((fail_n+1)); }

echo
echo "=== A24. S4 四门禁总验（S4.5：tempfile 临时库 hermetic，摄入延迟 P95/stale 过期命中/高价值 reindex/影响推荐精度，不起端口不触真机） ==="
# 合成时钟 20 样本验两段 P95，stale 走真实 FtsSearchAdapter 通道，CI 闭环与治理存活
# 走生产装配；红证（301s/601s/0.5 精度/负延迟/空样本）内置，报告落 out/eval/s4-gate-probe.{json,md}。
$PY scripts/s4_gate_probe.py > /tmp/tp_s4gate_a24.log 2>&1 \
  && grep -q '✅ s4_gate_probe 全部通过' /tmp/tp_s4gate_a24.log \
  && grep -q '门禁① 摄入延迟 P95' /tmp/tp_s4gate_a24.log \
  && grep -q '门禁② stale 过期命中' /tmp/tp_s4gate_a24.log \
  && { echo "PASS  $(grep -c '✅' /tmp/tp_s4gate_a24.log) 项检查全绿（两段 P95/红证守卫/stale 零命中与审计对照/CI 闭环重放幂等/dry-run pending/reindex 预算/精度红证与空分母/治理存活/scheduled 留痕/v15）"; pass_n=$((pass_n+1)); } \
  || { echo "FAIL  见 /tmp/tp_s4gate_a24.log 与 out/eval/s4-gate-probe.json"; fail_n=$((fail_n+1)); }

echo
echo "自动门禁结果：$pass_n PASS / $fail_n FAIL（A1–A6 S0 / A7 S1.4 / A8 S1.5 / A9 S1.6 / A10 S2.1 / A11 S2.2 / A12 S2.3 / A13 S2.4 / A14 S2.5 / A15 S2.6a / A16 S2.6b / A17 S2.6c / A18 S2.6d / A19 S3.1 / A20 S3.2 / A21 S3.3 / A22 S3.4 / A23 S3.5 / A24 S4）"
[ $fail_n = 0 ] || exit 1
echo "剩余手动门禁（Langfuse 服务端可见 trace、Web 对话页构建）按 subsystems/README.md「手动门禁」节执行。"
