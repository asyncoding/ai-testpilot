# TestPilot subsystems

研发测试质量智能体平台的工程实现。架构与需求权威见 `../docs/07-最终版总体设计-v2.md`、
`../docs/08-需求说明书.md`；本文件是 S0 骨架的**运行手册**。

## 目录

```
subsystems/
├── packages/
│   ├── core/          # testpilot-core：统一领域模型 + 端口 Protocol + 策略（零 IO/零 SDK）
│   ├── adapters/      # testpilot-adapters：文件/JSON/SQLite-FTS/录制型 等适配器
│   ├── orchestrator/  # testpilot-orchestrator：profile 装配 + FastAPI + qa_graph（S1.4 LangGraph 答案图）
│   └── workers/       # testpilot-workers：ingest / runner / eval 独立进程入口
├── config/profiles/   # offline-demo.yaml（默认，零外部依赖）/ ios-local-poc.yaml
├── tests/fixtures/    # 工单/运行结果/模型应答/文档/事件/git 录制仓 夹具（语言中立契约的证据）
├── web/               # S1.5 对话页（Next.js 15 静态导出，产物 web/out 由 FastAPI 同端口托管）
├── deploy/            # docker-compose（redis/chroma + observability profile 的 Langfuse）
└── scripts/           # import_lint 防腐门禁、smoke_trace（S0）、recall/live_verify/qa_probe 探针、build_web
```

## 环境准备（venv 强制隔离，禁止污染系统 Python）

需要 [uv](https://docs.astral.sh/uv/) 与 Python 3.12+（本机网络装不了 3.12 时用 3.14 亦可，
仓内 `.python-version` 当前为 3.14）。

```bash
cd subsystems
uv sync                       # 创建 .venv 并以 editable 方式安装四个包
```

> 网络备注：若默认 PyPI 源 TLS 失败，用清华镜像（只作为本机环境变量，不写进仓内配置）：
> `UV_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple uv sync`
>
> 若曾在包目录尚未建全时执行过 `uv sync`，editable 安装可能是空壳（import 报
> ModuleNotFoundError），修复：
> `uv sync --reinstall-package testpilot-core --reinstall-package testpilot-adapters \
>  --reinstall-package testpilot-orchestrator --reinstall-package testpilot-workers`

## 常用命令

```bash
# 全量契约测试（508 个：URN/策略、适配器契约、SQLite+FTS5、队列幂等、装配、
# 核心防腐、S1.1 切块/质量分/入库管线、S1.2 三通道融合、S1.3 live-verify、
# S1.4 答案图纯函数/节点/全图/服务/真仓 stale-index E2E、S1.5 HTTP API（TestClient）、
# P1–P4 引用治理（schema v6/v7 迁移、锚点渲染 fail-closed、符号路由、提示词投影快照、
# 意图免模型旁路）、S1.6 评测（golden 结构、hostless 裁判红→绿、runner 投影/双形态/
# hermetic 备份/容器端到端）、S2.1 用例文档解析（双格式/缺预期/取消/死信/老库 v8 迁移）、
# S2.2 需求骨架抽取（FR/NFR/AC/缺口/白名单链接/老库 v9 迁移）；
# 3 个 Chroma 真容器集成例在容器未起时自动 skip）
.venv/bin/python -m pytest -q

# 核心层防腐门禁（core 禁止 import fastapi/sqlite/yaml/各兄弟包等）
.venv/bin/python scripts/import_lint.py

# API（默认 offline-demo profile；8765 是门禁/演示约定端口，可自行更换）
.venv/bin/python -m uvicorn testpilot_orchestrator.app:app --port 8765
curl -s http://127.0.0.1:8765/health | jq .
curl -s http://127.0.0.1:8765/api/config | jq .   # S1.5：录制夹具横幅等前端装配数据

# 三个 worker 的装配自检（独立进程能装起整套容器即 S0 达标）
.venv/bin/testpilot-ingest
.venv/bin/testpilot-runner
.venv/bin/testpilot-eval

# 切 profile：环境变量覆盖，不改文件
TESTPILOT_PROFILE=config/profiles/ios-local-poc.yaml testpilot-ingest
TESTPILOT_REPO_PATH=/path/to/repo .venv/bin/python -m pytest   # 覆盖被测仓路径
```

> 环境隔离说明：`source .venv/bin/activate` 与直接用 `.venv/bin/python` 是同一个解释器，
> 二者**都原样继承当前 shell 的环境变量**。跑 Langfuse 手动门禁时若 `export TESTPILOT_TRACER=langfuse`，
> 残留变量会让手工启动的 uvicorn/worker/smoke 切到 Langfuse（开新终端或 `unset TESTPILOT_TRACER`
> 即恢复）。pytest 用例会自动清掉 `TESTPILOT_*` 覆盖（`tests/conftest.py`），
> `verify_s0.sh` 开头也会统一 unset，二者不受 shell 残留影响。

## 知识入库（S1.1）

ingest-worker 已从"装配自检"升级为真实管线：扫描 → content_hash 对账
（created/updated/deleted/unchanged）→ Markdown 章节切块 / Swift 最简符号切块 →
可解释质量评分 → 符号实体与同文件调用关系 → 单事务写 SQLite + FTS5。

```bash
# 离线夹具（offline-demo，默认 store/offline-demo）
.venv/bin/testpilot-ingest                 # 增量；重复跑 unchanged，幂等
.venv/bin/testpilot-ingest --full          # 全量重切（结果不变，可安全重跑）
.venv/bin/testpilot-ingest --inventory /tmp/inventory.json   # 语料盘点（FR-CON-002 AC3）

# 真实被测仓（另一仓库，路径走配置/环境变量，不拷贝代码）
export TESTPILOT_REPO_PATH=~/Documents/Code/app-calendar-alarm
TESTPILOT_PROFILE=config/profiles/ios-local-poc.yaml .venv/bin/testpilot-ingest
```

语义与证据：

- 文档 URN 按「根别名/相对路径」稳定，内容变化只改 version/content_hash；
  改一篇只重切该篇；删除文件产生 DocumentDeleted 事件并级联清 chunks/FTS（无残留索引）。
- 单文档失败（编码错误、切块异常等）进 `dead_letters` + `ingestion_status.stage=failed`，
  批次状态 partial，不阻断整批；修复后重跑自动覆盖为 indexed。
- 入库批次留痕 `ingestion_runs`，文档级状态留痕 `ingestion_status`
  （chunk_count / avg_quality / hash / run_id），切块质量分构成存在 chunk metadata。
- Swift 切块是**词法扫描的最简符号切**（类/结构体/枚举/actor/protocol/extension/func、
  selector 风格符号 ID、同文件 calls 关系、注释字符串屏蔽、struct+extension 重名去歧）；
  生产可换 SwiftSyntax，产出契约不变（FR-ING-002 AC3 命中率对比在评测期补）。
- 实测（2026-11-03，AlarmKitSpike 真实仓）：84 文档 / 1421 切块（S1.1.1 语料定界
  修正后；44 Sources + 3 Shared + 3 WidgetExtension + 34 md）；`scheduleAlarm`
  类查询代码符号块 BM25 排名第一。注意事故文档里的
  `MissionCoordinator.schedulePrimary` 在当前 HEAD 已不存在——这是 S1.4 live-verify
  与"历史证词并列"要处理的真实重构陷阱素材。
- 重置演示库：`rm -rf store/ios-local-poc`（生成物，不入库）。

## 混合检索（S1.2）：Ollama 嵌入 + Chroma + BM25 RRF

`ios-local-poc` profile 现在是**真实模型/真实向量库**（非录制）：
`model=ollama`（chat `qwen2.5:7b`、embed `shaw/dmeta-embedding-zh`）、
`vectors=chroma`（collection `alarmkitspike-chunks`）、`search=hybrid`
（trigram-BM25 ∥ 向量召回 → RRF k=60 × 质量分 × 可选新鲜度衰减）。
`offline-demo` 保持 recorded + `sqlite-fts` + 无 vectors，离线可完整演示。

```bash
# 1) 起 Chroma（数据在 docker volume chroma-data；镜像拉取不通见文末备用路径）
docker compose -f deploy/docker-compose.yml up -d chroma

# 2) 起 Ollama 并拉模型（embed 模型约 200MB；registry 不通可退 bge-m3，
#    改 profile 里 model.settings.embed_model 即可，维度随模型自动记录）
ollama serve &
ollama pull shaw/dmeta-embedding-zh

# 3) 入库（内容加工后自动跑嵌入补偿阶段；--full 全量重切重嵌）
export TESTPILOT_REPO_PATH=~/Documents/Code/app-calendar-alarm
TESTPILOT_PROFILE=config/profiles/ios-local-poc.yaml .venv/bin/testpilot-ingest --full

# 4) 三通道召回对照（BM25-only / vector-only / hybrid 的 HIT@5 与翻盘证据）
TESTPILOT_PROFILE=config/profiles/ios-local-poc.yaml \
  .venv/bin/python scripts/recall_probe.py
```

嵌入阶段语义（`ingestion_status` 留痕 embed_stage/model/dim/latency_ms）：

- 只嵌「内容已 indexed 但向量未就绪」的文档：**unchanged 且已嵌入的文档不再调模型**
  （幂等省钱）；历史嵌入失败、向量能力首次启用的全量补偿在普通增量跑里自动完成，
  不需要 `--full`；单文档 embed/upsert 失败进死信（`ingestion-embed`）、批次 partial，
  内容加工与 FTS 不受影响。
- 文档删除同步删 Chroma 切块；文档改标题导致 anchor 变化时，旧 anchor 向量作孤儿清除。
- 检索期向量通道故障（Ollama/Chroma 停了）→ 自动降级 **FTS-only**，`degraded=true`
  与错误写 `retrieval_events.raw_json.vector_error`，不吞错；两通道都空 = 真空结果，
  不算降级。装配期配错（hybrid 却没配 vectors）直接 ValueError，不许静默退化。

> 本机代理注意：Clash 类工具会把系统代理写进 macOS SystemConfiguration，
> httpx/urllib 对 localhost 的请求也会被代理 502（curl 直连 200）。适配器对
> loopback 目标自动 `trust_env=False` 绕过（`adapters/http_util.py`）；远端地址仍读
> 环境代理。Ollama/Chroma 地址可用 `TESTPILOT_OLLAMA_URL` / `TESTPILOT_CHROMA_URL`
> 覆盖，不写死在 profile 里。

已知行为/限制（后续阶段处理）：

- 无强匹配词的查询，BM25 会被查询里的**通用中文三元串**（"怎么把/事件传"）弱命中无关块
  （trigram OR 语义：任一三元串命中即返回，2026-11-04 EventDetailView 负例实测）；
  向量通道同理会给语义近邻。这类"知识库里实际没有"的问题靠 S1.4 答案层做证据充分性
  判定（无证据显式回答不知道），不在这里靠召回层硬滤。
- 实体关系层目前只有 Swift calls，doc 章节/需求实体在 S2/S4；
  新鲜度衰减当前显式关闭（语料 mtime 集中在近期同批，`half_life_days: null`）。

三通道 HIT@5 实测（2026-11-04，84 文档/1421 切块，dmeta-embedding-zh 768 维，
`scripts/recall_probe.py`，证据见 `.agent-handoff/`）：

| 题例 | BM25 | vector | hybrid |
|---|---|---|---|
| 机身键/熄屏→锁屏按键日志（零词面交集） | ❌ | ✅ #4 | ✅ **#1** |
| 未关闭再响实现与真机验证 | ✅ | ✅ | ✅ |
| iOS27 侧边键冷启动误挂静音门 | ✅ | ✅ | ✅ |
| EventDetailView 不回归（负例） | ✅ | ✅ | ✅ |
| **HIT@5** | **3/4（75%）** | **4/4（100%）** | **4/4（100%）** |

降级实测：Chroma 指向死端口时 hybrid 退化为 FTS-only（17 条 FTS 候选照常出答案），
`retrieval_events.degraded=1`、`raw_json.vector_error="ConnectError: …"`。

## Git 连接器与现场核验（S1.3）：ChangeSet + live-verify

`scm` 端口双实现（FR-CFG-002 AC2）：`git` 走 **git CLI 子进程**（不引 GitPython，
懒构造、超时 kill、`GIT_TERMINAL_PROMPT=0`），`recorded` 回放 `tests/fixtures/git/`
下固定 author/date 的确定性演示仓（`scripts/record_git_fixtures.py` 重新录制）。
ios profile 装配真实 git；offline-demo 用 recorded + `path_map`，**离线也能现场重切块
核验**，不访问任何外部仓。

能力（docs/08 FR-CON-001 / FR-QA-002 / NFR-006）：

- **ChangeSet**：base/head 两个 commit → `changed_files`（全部类型文件）+ Swift 符号
  增/删/改（身份键 = 去 `#L` 的 anchor + 声明序号，行号漂移不会误判成删+增）；
  **不读 commit message**，空 message commit 同样有效；可按 commit 读文件；
  仓库不可达 → `AdapterUnavailableError`（可识别错误：超时 / 不是 git 仓 / 目录不存在
  / git 二进制缺失各自分类），调用方降级不抛穿。
- **live-verify**：对已入库切块，从 `source_documents.uri`（绝对 `file://`）反推
  git 路径（不从有歧义的 URN 相对路径猜；越界 → `path_outside_repo`），在目标 commit
  （默认 HEAD）用**同一个 chunker** 重切 → anchor 选块 → `sha256` 精确比对：
  - `reason=unchanged`：HEAD 与索引一致；`reason=body_changed`：**当场抓出索引旧于
    代码，不需要重新入库**（FR-QA-002 AC1）；
  - chunker 版本缺失/不一致 → `chunker_version_drift`、`body_changed=None`，
    **绝不误报变更**；
  - 仓可达但文件/符号在该 commit 不存在 → `exists=False`（事实判定，不是降级）；
    git 不可达 / URN 不可解析 / 路径越界 / 索引缺失 → `degraded=true, exists=None`，
    任何意外异常也兜底成降级结果，永不抛穿。
  - **每次调用（含失败/降级）必写 `verification_events` 一行**：NoopTracer 的 span
    不落盘，留痕不能靠 tracer（FR-QA-002 AC2）。"代码问题 100% 触发核验"的强制接线点
    在 S1.4 答案图；本阶段交付能力层。
- **入库版本标注**：每轮入库取一次 HEAD sha → `chunks.valid_from` 与 Chroma metadata
  `indexed_commit`、chunker 版本写 metadata；unchanged 文档不刷值（无写放大）；
  scm 不可达 → `valid_from=NULL` + 死信 + report 记 `head_unavailable`，入库照常成功。
- **分支一致性**：文档/切块/实体/向量统一归属 `project.default_branch`
  （ios profile 是 `alarmkit-spike`，不是 `main`）；`--full` 重灌同时覆盖 SQLite 与
  Chroma（向量 id=URN 走 upsert，不删 collection，无残留旧分支元数据）。

```bash
# 真仓四项取证探针（非 CI 门禁：依赖本机真仓与已入库的 ios profile；
# CI/离线等价覆盖在 tmp 真仓单测 + recorded 契约测试里）
export TESTPILOT_REPO_PATH=~/Documents/Code/app-calendar-alarm
TESTPILOT_PROFILE=config/profiles/ios-local-poc.yaml \
  .venv/bin/python scripts/live_verify_probe.py
# ① 符号块验 HEAD → verified/unchanged  ② 同一 URN 验旧 commit → body_changed
# ③ ChangeSet 含文件+符号 modified       ④ repo_path 指不存在目录 → 降级不抛穿且留痕
```

真仓实测（2026-11-04，HEAD `7936295`、base `09b473b`，探针 4/4、退出码 0）：
`get_change_set` 得 **25 个变更文件、29 个 modified 符号**（含 `MissionCoordinator`）；
该区间实际改动是 `killBackupInterval 90s→30s` 常量与注释（git diff 实证，
`schedulePrimary` 方法体本身未变，探针因此选 `runSnooze` 切块做 ②）；故障注入在
`verification_events` 新增 degraded 行，错误文案为"仓库目录不存在或不可访问"。

约束与已知限制：

- **正式取证前被测仓工作区应干净**：fs 入库读工作区、live-verify 读 git blob。
  本次取证时真仓有 1 篇 `docs/*.md` 未提交改动（Swift 代码干净，探针只用 Swift 符号）；
  该 md 会被索引，其切块在 HEAD 核验时会如实判 `body_changed`——这是正确行为，
  但演示前应 `git status` 确认或先提交。
- **空 message commit** 的 ChangeSet 能力由 tmp 确定性演示仓（单测 + recorded 契约）
  覆盖；真仓这两个提交都带 message。
- deferred（`.agent-handoff/` 已记账）：~~FTS SQL 暂不按 branch 过滤~~（S1.4 已补：
  branch/acl 下推到 SQL 并有 `test_fts_search_adapter` 守卫；剩余 kinds 过滤不支持，
  检索节点降级跳过）；rename 仅按 R100 配对，R50/50% 阈值的误配风险待真实
  rename 样本；类型块含全部成员文本，成员改动会让类型块级联 MODIFIED（如实保留的噪声）；
  blame API、独立 commit 列表接口、push→自动 reindex 均未做（无 AC、无调用方）。

## 问答工作流（S1.4）：LangGraph 答案图、引用强制与三态版本标注

落地 docs/08 **FR-QA-001～004**：知识问答必须带可溯源引用、代码问题强制现场核验、
事故历史证词逐字并列、回答级反馈采集。编排用 LangGraph（docs/07 §11 指定），
但 `import langgraph` 全仓只出现在 `qa_graph/graph.py` 一个薄装配文件里——节点函数
全部是框架无关的 `async f(state, deps)`，备选手写状态机可零改动接管。

```bash
# 离线端到端探针（生产装配路径：bootstrap → 真实入库 → AsyncSqliteSaver 答案图）
TESTPILOT_PROFILE=config/profiles/offline-demo.yaml \
  .venv/bin/python scripts/qa_probe.py     # 四类问题 + 行数对账，全过 exit 0
```

语义与守卫（每条都有单测/探针守着，开发期均做过回退变红自证）：

- **无引用结论率 = 0（FR-QA-001）**：模型产出的是 `AnswerDraft`，引用必须落在检索证据的
  chunk/document URN 白名单内、quote 归一化后是证据文本子串、两级 hash 对账；任一不过
  最多带修复指令**重生成 1 次**，仍失败 → 显式「未知 / citation_failed_exhausted」，
  绝不放无引用答案出门。闲聊在 ACL 节点收口（`out_of_scope`），零模型生成、零检索。
- **代码问题 100% live-verify + 三态标注（FR-QA-002）**：code 意图对每个代码证据块调
  live-verify（调用数靠 `verification_events` 行数差断言，非 code 意图反向断言零调用）；
  引用版本态三选一：`consistent`（HEAD 与索引一致）/ `verified_at:<短sha>`（索引陈旧，
  head_refresh 节点当场读 HEAD 同 chunker 重切重锚、替换证据块）/ `unknown`（scm 降级，
  None 不冒充 False）。`recursion_limit` 从 `Limits.max_agent_steps=12` 显式映射
  （默认值 10007 等于没预算守卫），超限被 service 兜底落 `status=error` 的 qa_runs。
- **事故双段、模型不碰历史原文（FR-QA-003）**：incident 意图把 `L2_experience` 文档的
  证据块作为**历史证词**逐字引用（blockquote + 出处行由装配层拼，不经过模型），与模型
  生成的「当前结论」物理分节；两节 URN 硬隔离，模型越界引用历史文档时装配层抛
  ValueError 硬停（chunk/document 两种引用粒度都守），不静默出被污染的答案。
- **反馈采集（FR-QA-004）**：回答级 👍/👎（四原因枚举）落 `feedback` 表，写前先校验
  run 存在，幽灵 run_id 被 `NotFoundError` 拒且不留行；S1.6 的 bad case 池可直接
  经 `idx_feedback_run_id` join `qa_runs`。
- **可复现与可对账**：state 是全 JSON 原语 TypedDict（pydantic/datetime 不进
  checkpoint，有 json 往返测试锁死）；qa_runs 与 eval_runs 对齐 model/prompt/params/
  data 四元组。QaService 的 saver 是**异步生命周期边界**：同步 `build_container` 不感知
  LangGraph；`astart()` 显式 `await saver.setup()`（实测 enter 不建表、首写才懒建，
  setup 幂等），checkpoint 落 `data_dir/checkpoints/qa.sqlite`，`aclose()` 幂等收口，
  重开服务可经 thread_id 恢复历史 run 状态。

本轮在真实装配路径上照出并修复的四个缺陷（均有红→绿证据，细节见 `.agent-handoff/`）：

1. 原始问题直塞 FTS5 MATCH，`. : " * ( )` 语法字符抛 OperationalError 炸断检索链
   → 两通道统一走 core `shape_trigram_query` 整形 + LIKE 兜底；
2. `upsert_chunk` 冲突时只刷 text/stale/hash，FTS 新文本与 raw_json 旧载荷自相矛盾
   → 改全行替换（与 S1.3 文档分支覆盖同口径）；
3. 事故交叉守卫只认 chunk 级 URN，文档级引用历史文档可穿透 → 双粒度守卫；
4. **FTS-only 通道水合只取 raw_json，丢掉 doc_authority/doc_uri/hash，离线 profile 上
   FR-QA-003 历史证词识别整链静默失效** → 与 hybrid 通道共用 `load_retrieval_chunks`
   折叠文档元数据（`tests/test_fts_search_adapter.py` 守卫）。

已知缺口（如实记账，留后续阶段）：

- chunk 级 `kind="incident"` 未做（要动入库全链路）：事故意图当前靠文档
  `authority_level=L2_experience` + 标题关键词识别，离线语料仅 sidebar-fallback 一份
  L2 素材，覆盖薄；留 S2。
- FTS 检索的 **kinds 过滤仍不支持**（检索节点降级跳过并打 `kinds_filter_unsupported`
  降级标记）；branch/acl 过滤本轮已补到 SQL 并有用例（S1.3 记的 deferred 已核销）。
- ~~HTTP 端点与前端不在本切片~~：HTTP 端点已在 S1.5 落地（见下节，router 薄包装 +
  异常映射，core 模型零改动）；阈值标定仍在 S1.6（机制 + 保守默认 + `PARAMS_VERSION`）。

## 问答 HTTP API（S1.5）：FastAPI 薄包装与静态前端同端口托管

落地 docs/08 **FR-UI-001 后端面**、FR-QA-004 AC2（反馈聚合）、FR-EV-004 AC1（UI→trace
反查）。fastapi 仍只出现在 orchestrator（`qa_api/` 包），core 零 web 依赖，import-lint
继续守门；router 无业务逻辑，全部能力是 `QaService` 的薄包装。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/qa/ask` | 入参 `{question, project?, branch?, acl_scope?, case_key?}`；`case_key` 是编排层字段，core `AskRequest` 冻结不改 |
| GET | `/api/qa/runs?status&limit` | 历史 run 列表（limit 1–200） |
| GET | `/api/qa/runs/{id}` | run 详情（JSON 列已反序列化） |
| GET | `/api/qa/runs/{id}/sources/{urn:path}` | 证据原文代理；URN 需百分号编码（`#` 否则被当 fragment） |
| GET | `/api/qa/runs/{id}/trace` | 本地时间线；langfuse 字段在 noop tracer 下如实为 `null` |
| POST | `/api/qa/runs/{id}/feedback` | 👍/👎（四原因枚举）；幽灵 run → 404 且零留痕 |
| GET | `/api/qa/feedback/summary` | 按 kind/reason 聚合（FR-QA-004 AC2） |
| GET | `/api/config` | profile/适配器形态/`recorded`/写开关，前端横幅数据源 |

异常映射（统一错误体 `{"error":{"type","message"}}`）：NotFound 404 / PolicyDenied 403 /
AdapterUnavailable 503 / UnsupportedCapability 501 / SchemaValidation 502 /
EvidenceInsufficient 422 / TestPilotError 兜底 500；请求体校验走 pydantic 原生 422。

安全与诚实性守卫（均有反假绿测试）：

- **原文代理无路径穿越面**：端点只收 URN 不收文件路径；白名单 = 该 run 证据块 URN ∪
  证据所属文档 URN ∪ 模型实际引用 URN，白名单外（含同库真实存在的他文档）一律 404——
  测试用「同库对照文档」证明不是「所有 URN 都 404」的假绿。
- **录制 profile 的自由提问确定性走未知降级**：录制模型按 `qa_<kind>_<case_key>` 取响应，
  无 `case_key` 时缺键 → `AdapterUnavailableError` → 未知/`model_unavailable`，不是 bug；
  四个确定性演示故事 `stale_code / kill_backup / unanswerable / chitchat` 与 qa_probe 同款。
- **trace 不造数**：FTS 通道 `retrieval_event_id` 允许为 `null`；Langfuse 深链仅在
  tracer=langfuse 时按 `LANGFUSE_HOST` 生成（`session_id=run_id`），noop 给 `null`。
- **静态挂载守卫**：`web/out` 不存在时只记 warning，`/health` 与 `/api/*` 照常服务，
  CI/无 node 环境不受影响；A8 门禁不依赖 npm。

生命周期：`QaService`（含 AsyncSqliteSaver）在 lifespan 内异步启动，关停顺序固定
`aclose()` → `store.close()`；测试经 `create_app(container=..., web_dist=...)` 注入
tmp 容器，TestClient 真实跑 lifespan 但不起端口。

## Web 对话页（S1.5）：Next.js 静态导出，单进程演示

`web/` 是独立于 uv workspace（`members=packages/*`）的 Next.js 15 App Router 工程，
`output: "export"` 纯静态导出到 `web/out/`，由 FastAPI `StaticFiles` 同端口托管——
演示时只有一个进程、一个端口（http://127.0.0.1:8765/），无 CORS；node 缺失/未构建时
后端只打 warning，API 与门禁不受影响。

```bash
# 1) 构建静态产物（不入自动门禁；国内网络用镜像）
bash scripts/build_web.sh
#   registry 受限时：NPM_REGISTRY=https://registry.npmmirror.com bash scripts/build_web.sh

# 2) 先入库（qa_probe 会顺带完成 full 入库 + 自检），再起单进程服务
TESTPILOT_PROFILE=config/profiles/offline-demo.yaml .venv/bin/python scripts/qa_probe.py
.venv/bin/python -m uvicorn testpilot_orchestrator.app:app --port 8765
# 浏览器开 http://127.0.0.1:8765/

# 前端开发态（热更）：next dev 经 next.config.ts rewrites 把 /api 代理到 8765
cd web && npm run dev        # http://localhost:3000，后端仍需单独起在 8765
```

FR-UI-001 AC 对照（均有后端测试或构建期 TypeScript 检查守着）：

| AC | 实现位置 |
|---|---|
| 问答对话 | `QuestionComposer` + `POST /api/qa/ask`；四个预设故事携带 `case_key` |
| 引用可点开原文与 hash | `CitationList` → source 白名单代理，抽屉展示 text/hash/uri/权威等级 |
| 三态版本标注 | `consistent` 绿 / `verified_at@sha` 黄 / `unknown` 灰徽章 |
| 负例「未知」样式 | `AnswerCard` 红色未知卡 + unknown_reason 中文文案 + degraded 标签 |
| 👍/👎 反馈（四原因） | `FeedbackBar` → feedback 端点；幽灵 run 404 且零留痕 |
| trace 入口 | `TraceDrawer`：时间线/证据/三态/版本四元组/langfuse 深链（noop 时如实提示 null） |
| 「证据来自录制夹具」标注 | `Banner`（FR-OPS-002），并说明自由提问确定性降级的原因 |
| 事故双段历史证词 | `AnswerCard` react-markdown 渲染双段 markdown + `HistorySection` 逐字 blockquote |

工程约束：前端只经同源 `/api` 通信，URN 全量 `encodeURIComponent`（`#` 否则被当
fragment）；不引 openapi 生成与 UI 框架，类型手写镜像后端契约；`node_modules/.next/out`
均不入库。已知缺口：仅对话页一页（其余四个控制台页在 S2–S5）、无 SSE（ask 同步）、
无 HTTP 认证/多用户（POC 单用户，acl_scope 走请求体）。

## 引用失败 / 低分观感 / 慢响应专项治理（P1–P4，2026-11-07）

起因是一次真实留痕（run `3e64fb59`）：精确符号提问
`AlertListViewModel.snoozeRingingMission(minutes:)` 回「未知 / citation_failed_exhausted」，
96.5s、20879 tokens_in；同时界面上精确命中块的 score 只有 0.012。排查先纠正一个误读：
**0.012 是 RRF 秩信号（量纲 0–0.033），不是相关性质量分**。三组隔离实验定根因：
① 长 prompt（10.3k token / 8 块证据）下 7B 模型抄写退化 3/3 复现，短 prompt 逐字复制
4/4 合格——是长上下文退化，不是 JSON 传输问题；② 精确符号查询走了向量语义抽奖，目标块
只排到第 5；③ Ollama 冷加载 55.3s（服务端 `--keep 4` 分钟即卸载）。修复按四阶段落地，
每阶段独立可回滚，每项都做过临时回退看红的红→绿自证。

- **P1 可观测与便宜项（schema v5→v6）**：`qa_runs` 增 `citation_errors` 列（ALTER 迁移，
  老行吃 DEFAULT '[]'），失败真因（`no_citations` / `urn_not_in_evidence` /
  `anchor_not_found` 等）贯通节点→账本→API→UI，AnswerCard 按错误类型说话，不再笼统
  甩「白名单」；trace 证据表删裸 RRF 分列，改展示 rank + 命中通道（fts/vector），原始分
  只留调试视图；Ollama `keep_alive=30m` + lifespan 后台预热（chat+embed，失败只 warning
  不阻断启动）；structured 输出增加 `citations` 非空浅校验，零引用草稿在适配器重试循环内
  当场重发，不白跑整轮图迭代。
- **P2 锚点引用 + 服务端渲染（PROMPT_VERSION → qa-prompt-2）**：模型只产
  `{urn, anchor}`——一个证据中逐字出现的短锚点，不抄长 quote、不数行号；core 新增
  hostless 纯函数 `render_citation_quote`：code_symbol 块锚点准入后渲染整个符号块
  （切块边界即引用边界），text 块取锚点源文切片，**锚点未命中 fail-closed 返 None
  （anchor_not_found），禁止模糊凑句**。`validate_citations` 的逐字口径不放宽，校验对象
  换成服务端渲染结果，退化为不变量断言 + 防接线 bug 的第二道防线。图级回归用例精确复刻
  3e64fb59 形态（URN 合法但 anchor 被改写 → 重生成耗尽 → 三处账本同构留痕）。
- **P3 符号路由（零重摄入、零新依赖）**：core 新增 hostless 纯函数
  `parse_symbol_reference`，解析 `Type.member(label:)` 与紧凑 selector `_:label:` 形态，
  标签归一化复用 chunker 同一套 Swift 口径（不造第二个正则）；store 经
  `entities JOIN chunks ON symbol_urn` 精确反查（库内 526 个符号 100% 已挂 symbol_urn）。
  code 意图且**唯一**命中时该 chunk 确定性置顶（trace 标 `symbol_pinned=true`），其余候选
  仍走 RRF 补位；重名 / 查无 / 非 code 意图 → 原 hybrid 路径**零行为变化**；裸 FTS 形态
  不支持此路由，落 `symbol_pin_unsupported` 降级标记。精确符号查询不再经过向量语义抽奖，
  0.012 观感问题同步消失。
- **P4 延迟治理（schema v6→v7，PROMPT_VERSION → qa-prompt-3）**：
  - **提示词瘦身只作用于符号置顶路径**：置顶块给全文，其余 code_symbol 块只给入库时归一的
    签名、其他块只给首行（200 字符上限并显式声明截断）；URN 白名单仍含全部证据，anchor
    渲染与引用校验始终读 state 里的全文证据，链路不变。`pinned_urn=None`（没有精确路由
    背书）时消息与 qa-prompt-2 **字节级一致**，由 hostless 快照测试锁死——避免在目标块
    本来排第 5 的非符号查询上把正确证据饿死。
  - **意图免模型与检索共用同一高精度解析器**：题面命中符号形态直接判 code，省一次 LLM
    调用，账本落 `intent_source='regex_bypass'`；正常模型分类记 `model`，异常关键词兜底
    记 `fallback`（与 degraded 标记同时出现）。`qa_runs` 经 schema v7 增 `intent_source`
    列（ALTER 迁移，v7 之前的历史行是空串，前端显示「—（v7 之前的历史 run）」不造数），
    trace/API/UI 全链路可见，可直接 SQL 切片对比两类 run。中文概念/howto 题有对照实测，
    仍走模型分类，旁路没有过宽。

实测验收用**真库的 hermetic 字节副本**（526 符号，副本上自动迁移 v5→v7）+ 共享只读
Chroma + 真 ollama，不碰在 8765 端口运行的旧进程、不写真库：

- 原问题暖身后连跑 3 次 **3/3 answered**：`symbol_pinned` 稳定 true、目标 chunk 稳定
  rank 1、`citation_errors=[]`、版本态 consistent、prompt=qa-prompt-3、来源 regex_bypass；
  暖身中位数延迟 6.7s（另一轮 10.9s），对照历史 run 96.5s 失败 / 31.4s 勉强成功。
- **反事实对照**（运行时 monkeypatch 关闭投影、生产代码零改动）：同题回全文形态 →
  20879 tokens_in、60.8s、citation_failed_exhausted——与历史失败的 20878 token 同量级，
  复现了原始长上下文失败条件；瘦身路径 3854 tokens_in、16.0s answered，输入 token 降约
  82%。token 数全部取自真 ollama 留痕，不是估算。
- 红→绿三组回退：旁路关闭 5 红、投影强制全文 6 红、v7 迁移条目摘除 2 红，恢复后全绿；
  全量 377 passed、import-lint OK、`web` 静态构建（含 tsc）通过。
- **静态前端与后端的版本错配防护**：`web/out` 是独立重建的静态包，可能比在跑的 uvicorn
  新——旧后端 trace 响应没有 `citation_errors`/`intent_source`/`symbol_*` 字段、证据项没有
  `channels`/`rank`，裸 `.length` 会把 trace 抽屉打白（实测用旧进程真实载荷复现逐字同款
  `TypeError`）。这些字段在前端类型上标可选，渲染侧一律 `?? []` / 序号回退；但根治仍需
  重启后端进程让新代码生效（重启即对真库自动做 v5→v7 加列迁移，列都是追加式的，建议先备份）。

明确不做（避免单样本过拟合，留 S1.6 的 20–30 题 golden set 标定）：RRF k 与 top-k 调参、
quality 分公式与物化分重算、全库重嵌入、tree-sitter（entities 已 100% 覆盖）、LCS 模糊
自救、把引用搬出 JSON 的传输重构（隔离实验已证伪其前提）。SSE 流式只改体感，未授权不做。
另如实记账：模型路径上的高难中文事故题仍可能 citation 失败——fail-closed 拦截伪造引用是
正确行为，属 S1.6 golden set 待标定项，不是本批回归（符号旁路路径近 5 run 同类失败已清零）。

## 离线评测体系（S1.6，FR-EV-001～003）：golden set、hostless 裁判与双形态 runner

题集与裁判分离、机控与质量分离：

- **题集入 git，改题走 git 评审**。`tests/fixtures/eval/golden_s1.json` 是冻结的 30 题
  golden-s1-v1（代码 12 / 文档 8 / L2 事故 3 / 版本陷阱 1 / 负例 6，负例占比 20%；
  26 条取证 URN 全部查真库填写，2 篇 holdout 文档验证 0 入库）；
  `golden_recorded_smoke.json` 是 4 题机控集，只验证评测链路装配，**不做门禁判定**。
- **裁判是 core 零 IO 纯函数**（`testpilot_core.evaluation`，import-lint 保护）：
  8 个错误标签按召回侧（intent_mismatch/empty_retrieval/recall_miss）与生成侧
  （unexpected_unknown/no_citations/citation_forgery/wrong_answer_on_negative/
  strict_reason_mismatch）二分记账，收集全部失败标签而非首错即停；聚合指标含
  golden 通过率、Recall@K、拒答/意图/引用准确率、伪造护栏、延迟分位、token、attempts。
  忠实度等语义分在 E5 隔离裁判模型接入前一律标「无基线」，报告任何位置不得造数。
- **双形态 runner**（`scripts/eval_probe.py`，裁判代码同一份）：

```bash
# ① recorded：离线确定性，进 verify_s0 A9。录制模型 + offline-demo 语料，全绿才 exit 0
TESTPILOT_PROFILE=config/profiles/offline-demo.yaml \
  .venv/bin/python scripts/eval_probe.py

# ② ollama：真模型 hermetic 基线（需 docker chroma + ollama serve；E4 标定用）
#    sqlite 经 online backup API 复制到 /tmp 隔离目录，源库/活进程只读不碰；
#    Chroma 继续 HTTP 共享只读。--limit N 可先冒烟 hermetic 路径
.venv/bin/python scripts/eval_probe.py --mode ollama \
  --profile config/profiles/ios-local-poc.yaml
```

  每次评测落 `eval_runs`（data/model/prompt/params 四元组 + 指标 + report_uri）与
  `eval_cases`（locked_version 题集版本，幂等 upsert）；JSON/Markdown 报告落
  `out/eval/`（gitignore），含 S1 门禁对照（引用准确 ≥.95 且伪造=0、Recall@K ≥.85、
  拒答 ≥.9、忠实度 ≥.9）、错误二分、逐题判定与 holdout 泄漏检查。
  ollama 形态的质量差距是发现项不是探针失败，只有机械故障才 exit 1。

### E4 参数标定（2026-11-08）：两旋钮均证伪，出厂默认零变化

真 ollama + hermetic 副本上跑单变量实验，每轮独立报告 + `--compare` 成对对比
（超 1 个四元组槽位变动即判纪律破坏 exit 1）：

- 规范基线（qa-params-1，top-k=8 / rrf_k=60）：10/30，引用准确 1.0 / 伪造 0（达标），
  Recall@K .4583、拒答 .50（未达标，归因模型侧）。
- 实验① top-k 8→12（qa-params-2）、实验② rrf_k 60→30（qa-params-3）表面分数互有
  涨跌，但 required-evidence 成员分析证明 rrf_k 改动**没有把任何 golden 证据移过
  top-8 截断边界**，翻转题在两旋钮下同时变绿/变红，是 ollama 网关无 temperature/seed
  的采样噪声而非检索收益，且 token 成本上升。两个旋钮**均不采纳**。
- 出厂默认保持 top-k=8 / rrf_k=60，`PARAMS_VERSION` 回归 `qa-params-1`（qa-params-2/3
  被实验报告永久占用，禁止复用；今后真改默认值从 qa-params-4 起）。质量公式/物化分
  重算经评估无可靠靶点（intent_mismatch 来自模型分类过触发，regex_bypass 8 题全对），
  no-go 留诚实记账。

### E5 隔离语义裁判（LLM-as-judge，docs/06 §6.3 / docs/07 §7）

裁判模型与生成模型不同体：生成 qwen2.5:7b，裁判 `deepseek-r1:7b`（prompt 版本
semantic-judge-1）。裁判纯函数在 core（`semantic_judge.py`，零 IO 零三方），
fail-closed：信封非法/空 claims/被支撑却缺 URN 一律记 parse_error、两维判负、绝不抛穿；
只裁正例已答题，人工裁定 `adjudication` 槽恒 null（专家盲标前不造 kappa 基线）。

```bash
.venv/bin/python scripts/eval_probe.py --mode ollama --semantic-judge
```

首版真模型基线（报告 eval-36a85c72，17 个正例已答题）：**忠实度陈述级微平均 .9429
过 .90 门**；逐题全支撑率 .8235（G07/G19 各 1 条无证据陈述、G02 裁判信封解析失败）；
**答案相关性 .5882 未过 .85 门**（7 题判负，与机控面 intent_mismatch 同源，归 S2
提示工程/模型侧）。

### E6 tree-sitter Swift 切块器选型复核（FR-ING-002 AC3）

tree-sitter 只作 **adapters optional dependency group**，core 零三方红线不动；缺包时
`TreeSwiftChunker` 构造抛 `AdapterUnavailableError`，入库单文件失败自动回退词法器并写
dead_letter（`ingestion-tree-sitter-fallback`）。安装：

```bash
VIRTUAL_ENV=$PWD/.venv uv pip install -e 'packages/adapters[tree-sitter]'
# profile 开启：adapters.docs.settings.swift_chunker: tree-sitter（默认 lexical）
```

离线边界对比探针 `scripts/chunker_compare.py`：扫三个 code_root 共 50 个 Swift 文件，
以 `xcrun swiftc -dump-ast`（iOS 模拟器 target，否则 `#if canImport(UIKit)` 整段被
条件编译跳过）的 564 个非隐式声明为真值。结论（报告 `out/eval/chunker-comparison.md`）：

- **tree-sitter 真漏报 0（564/564 全覆盖），词法器真漏报 27（全部是 init 构造器）**；
  GT 符号召回 .9858 vs .9167；边界精确率（结束行对真值）双双 1.0；真实仓 ERROR 节点 0；
  tree-sitter 是词法器严格超集（独有 39 个紧凑声明/构造器符号，反向独有 0）。
- 两器共享的命名口径差 8 处（core `_selector` 吸入返回元组标签、dotted 扩展只取首段），
  非边界变量，修需动 core 共享函数，超出本次隔离范围，记档不做。
- 选型期实证修复适配器一个真 bug：全树 DFS 取类型名会把 `@MainActor`/`@available`
  属性里的类型误当 owner（真实仓曾切出 `MainActor.xxx`/`available.xxx`），改为只认
  声明节点直接子节点，红→绿回归测试 `test_attributes_do_not_pollute_type_owner_naming`。

**在线双索引 30 题对比与选型结论（默认不换，词法器留作降级路径）**：用独立 collection
`alarmkitspike-chunks-treesitter`（生产 collection 不碰）+ 全新 data_dir 全量重建索引
（565 块，版本位 tree-sitter-swift-1，零回退死信），同一份 golden_s1 跑批与词法基线
成对对比（四元组零槽位变化，唯一变量是切块方式）：

- **Recall@K 持平 .4583 → .4583**（切块器本应改善的检索指标零变化）；通过率 10/30→11/30
  在采样噪声带内（1 退 G03、2 进 G14/G26，与 E4 rrf_k 实验同型的随机翻转），引用准确
  1.0 / 伪造 0 持平。
- 根因实证：26 条 golden 取证 URN **无一指向 init/构造器**，而 tree-sitter 多出的 39 个
  符号绝大多数是 init；TS 检索进 top-k 的独有 URN 仅 31 个、其中 init 只 1 个——离线边界
  优势在当前题集上没有检索收益。仅有的 2 条 required 文档新命中（G22/G23，RRF 池变化的
  间接排名效应）仍因模型侧 intent_mismatch 失败，无端到端收益，且零证据丢失。
- **决策：出厂默认保持 lexical**。tree-sitter 作为 optional 能力保留（profile 可开、
  单文件失败自动回退词法器并写 dead_letter），词法器同时是降级路径；待未来题集覆盖
  构造器/紧凑声明类问题、或语料出现词法盲区导致的真实检索 miss 时再重评切换。

## 一键验收（11 个自动门禁）

```bash
cd subsystems
bash scripts/verify_s0.sh    # 11 PASS / 0 FAIL；日志在 /tmp/tp_*.log
```

覆盖：venv 四包可 import、508 个契约测试、core 防腐 import-lint、`/health` 真实 200、
三个 worker 独立进程 ready、noop 空链路 trace 调用面、A7 离线问答探针
（FR-QA-001～004 四类问题 + qa_runs/feedback 行数对账）、A8 问答 HTTP API 冒烟
（真实 uvicorn + curl：chitchat 未知降级、幽灵反馈 404、`/api/config`）、
A9 离线评测探针（FR-EV-001～003：golden smoke 4 题 + hostless 裁判 + eval 账本/报告）、
A10 用例文档连接器（FR-CON-003：双格式解析 + 死信/取消/缺预期形状断言）、
A11 需求骨架抽取（FR-TD-001：确定性解析 + 录制模型 AC↔FR 白名单链接）。
A4/A8 就绪轮询带进程存活检测：端口被遗留 uvicorn 占用时直接判 FAIL，杜绝 curl 打到
旧进程的假绿（排障：`lsof -nP -iTCP:8765`）。

## 基础设施（Docker）

```bash
# S1.2 起只用 chroma 即可：docker compose -f deploy/docker-compose.yml up -d chroma
# 全量起：redis（S3 arq 队列；S0 只在容器网内被 langfuse 用）+ chroma（S1.2 向量检索）
docker compose -f deploy/docker-compose.yml up -d

# S0 trace 门禁：Langfuse v3 自托管栈（pg/clickhouse/minio 占位/langfuse web + worker）
# 必须同时起 langfuse 和 langfuse-worker（v3 拆分：web 入队，worker 消费写 ClickHouse）
docker compose -f deploy/docker-compose.yml --profile observability up -d langfuse langfuse-worker
# 健康判定（约 1–2 分钟，首次要跑迁移）
curl -s http://127.0.0.1:3030/api/public/health   # 期望 {"status":"OK","version":"3.x"}
# worker 没有端口，看日志确认起来
docker logs testpilot-langfuse-worker-1 --tail 5
```

镜像拉取（受限网络实测：docker.io 直连 TLS EOF；**docker.1ms.run 可用**，daocloud blob CDN 不稳；
镜像源不写进 compose，拉完 `tag` 回标准名保持文件可移植）：

```bash
for img in library/postgres:16-alpine clickhouse/clickhouse-server:24.8-alpine \
           langfuse/langfuse:3 langfuse/langfuse-worker:3 chromadb/chroma:0.5.20; do
  docker pull docker.1ms.run/$img && docker tag docker.1ms.run/$img ${img#library/}
done
```

### 无 MinIO 镜像时的备用路径（2026-11 实测打通）

各镜像站均不代理 `minio/*` 老标签。改用 brew 在宿主机直接跑 MinIO（bottle 走 ghcr.io）：

```bash
HOMEBREW_NO_AUTO_UPDATE=1 brew install minio minio-mc
# 起宿主机 MinIO 并建桶（幂等；内存紧张被系统回收时重跑此脚本即可）
bash scripts/start_host_minio.sh
# 起栈时叠加 host-minio 覆盖（S3 指向 host.docker.internal，不发布容器 redis 端口避免与宿主 redis 冲突）
docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.host-minio.yml \
  --profile observability up -d langfuse langfuse-worker
```

> 内存提醒：观测栈容器约占 2.2 GiB（ClickHouse ~1.1G、Langfuse ~1G，机器 8G 时偏紧），
> 宿主机 MinIO 可能被系统回收；症状是 trace 导出失败，重跑 `scripts/start_host_minio.sh` 即可。

踩坑记录（compose 已修，别再踩）：①ClickHouse alpine 内 `localhost` 走 IPv6 而 CH 没绑
IPv6，健康检查必须用 `127.0.0.1`；②Langfuse v3 的 `ENCRYPTION_KEY` 必须 **64 位 hex**
（`openssl rand -hex 32`，compose 里是仅本地开发的固定全零值），且除 `S3_*` 媒体桶外
还必须配 `LANGFUSE_S3_EVENT_UPLOAD_*` 事件桶变量，否则启动 500（ZodError）；
③Langfuse 镜像内 Node 服务只绑容器网卡 IP（不绑 loopback/::1），它的 healthcheck
必须用 `http://$(hostname):3000`；④**v3.225 的 BullMQ 队列只认 `REDIS_HOST/REDIS_PORT`
（或 `REDIS_CONNECTION_STRING`），旧文档里的 `LANGFUSE__QUEUE__REDIS__CONNECTION`
被静默忽略——现象是 OTLP 返回 500、日志只有 "Failed to instantiate otel ingestion queue"、
事件已写 S3 但 ClickHouse traces 表为空、Redis 无队列键。compose 已改。
⑤修了④之后任务进了队列但**只入队不消费**（wait 堆积）：v3.225 的 node-redis worker
连接在 Redis 无密码时仍会发 AUTH（未设置时发的是字符串 `"undefined"`，空串也会发），
被 Redis 拒连。解法：容器 Redis 加 `--requirepass tp-dev-redis`，langfuse 配
`REDIS_AUTH=tp-dev-redis`（仅本地开发固定值）。排查命令：
`redis-cli -a tp-dev-redis LLEN bull:otel-ingestion-queue:wait`（正常应为 0）。
⑥**v3 还必须起独立的 `langfuse-worker` 容器**（镜像 langfuse/langfuse-worker:3）：
web 只把 OTel 事件写 S3+入队，读 S3→写 ClickHouse 的消费者是 worker。少了它的症状是
OTLP 不报错（或 500）但 trace 永不入库、`bull:otel-ingestion-queue:wait` 只增不减。
已实测：补齐 worker 后积压任务立即被消费，3 条 smoke trace 入库。

## 手动门禁：空链路 trace 出现在 Langfuse（约 2 分钟，独立于 8 个自动门禁）

前置：上面的 observability 栈 `health=OK`。

1. 浏览器打开 http://localhost:3030 → 注册首个本地账号 → 建 project
   （Settings → Projects，默认 project 也可）→ Project Settings 里创建 API keys，复制
   `pk-lf-...` / `sk-lf-...`；
2. SDK（v5，OTel API；LangfuseTracer 不兼容旧 v2）：
   `UV_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple uv pip install --python .venv/bin/python 'langfuse>=5.0'`；
3. 发一条空链路 trace，脚本**回查 Langfuse API 确认可见**（不是只信 SDK flush）：

```bash
LANGFUSE_PUBLIC_KEY=pk-lf-... LANGFUSE_SECRET_KEY=sk-lf-... \
TESTPILOT_TRACER=langfuse \
.venv/bin/python scripts/smoke_trace.py --expect langfuse
# 成功：{"verified": true, "tracer": "langfuse", ...}，退出码 0；同时 UI Traces 出现 smoke.empty_chain
```

判定标准与排障：

- 退出码 0 + UI 可见 → 门禁闭合。
- `30s 内未查询到`：先 `curl -u pk:sk 'http://127.0.0.1:3030/api/public/traces?sessionId=<输出里的 run_id>'`；
  401=key 错；空数组看 `docker logs testpilot-langfuse-1` 与 S3 连通（host 路径下
  `mc ls local/langfuse/events/` 应有对象）。
- 无 Docker 环境：`scripts/smoke_trace.py`（默认 noop）只能证明调用面，**不算闭合此门禁**。

## 写入安全默认值

所有 profile 默认 `external_write_enabled: false`；JSON 工单适配器的写操作经
`policy.assert_writable` 守卫（R2/R3 需审批、R4 禁止、全局 kill switch），
outbox 先落库后外发。这是演示环境的安全底线，换真实适配器时不得绕过。

## 用例文档连接器（S2.1，FR-CON-003）：双格式 Markdown 解析、缺预期标记与死信隔离

测试设计纵切的第一块基石：把自由格式用例文档变成可检索、可评测的结构化用例。
**core 解析器是零依赖纯函数**（`testpilot_core.testcase`，只依赖 stdlib + pydantic，
import-lint 守卫），适配/落库在 adapters/workers 层，遵循既有分层。

### 解析契约（V1 用例文档两种形态都覆盖）

| 维度 | 行为 |
|---|---|
| 叙事体 | `## 模块` + `### TC-Xn 标题（FR 引用）P0` → 顶格编号步骤 → `**预期**：` 子弹（支持 `**预期**（备注）：` 夹注形态，TC-A7 实证）；嵌套缩进子弹/编号子项挂同一条 oracle |
| 表格体 | 含 `ID` 列的表格按表头语义映射（步骤要点/步骤、预期、优先级/级、环境），列名不统一也能识别；无 ID 列的表（自动化现状/缺陷定级）整体跳过 |
| 缺预期 | 有步骤无预期 → `oracle_missing=true` 但**不丢弃**（真档仅 TC-A4：无独立预期段，"预期"散文嵌在步骤里，原文不丢，留 S2.4 oracle 挖掘复评） |
| 取消态 | 删除线标题/行或优先级=取消 → `lifecycle=cancelled`（真档 TC-B6），留存不参与漏报统计 |
| 死信 | 空壳用例/列数损坏行进 `CaseParseError`，单条失败不阻断整批；代码围栏内的标题/表格不是结构 |
| oracle 四分类 | 确定性关键词规则分到 UI/日志/状态/人工四类，拿不准归 manual；每条回链来源文档 URN，置信度统一 0.5 待 S2.4 复评 |
| 幂等 | URN = 文档 external_id + TC ID，同文档重解析 URN 不变；重灌按 doc_urn 整组替换，删用例不留幽灵 |

### 运行方式

```bash
# 自动门禁 A10：合成夹具 --full 全链路入库（离线 profile，确定性断言 partial 形状：
# 7 用例/1 死信/2 取消/1 缺预期；夹具故意带损坏行，worker 按设计 exit 1，门检验 JSON 形状）
# 单跑（不与 A4/A8 的 8765 端口冲突）：
TESTPILOT_PROFILE=config/profiles/offline-demo.yaml \
  .venv/bin/python -m testpilot_workers.ingest_cases --full

# 真档验证（非 CI 门禁；语料仓是另一个只读仓，路径参数化、不写死、不拷贝原文）：
.venv/bin/python scripts/case_parse_probe.py \
  ~/Documents/Code/app-calendar-alarm/docs/V1测试用例文档.md
# 报告落 out/eval/case-parse-probe.{json,md}（gitignore），独立正则反查原文
# TC 标记与解析结果一一对应，不一致/有死信 exit 1
```

### 验收证据（2026-11-09，V1 真档）

- **52 个 TC 标记 → 52 解析成功，零死信、零漏解析**（探针独立计数对账）；
  active 51 / cancelled 1（TC-B6）；P0 44 / P1 7 / 取消行无优先级 1；
  oracle 分布 UI 20 / 人工 23 / 日志 11 / 状态 10；缺预期仅 TC-A4。
- **红→绿实证一个真 bug**：TC-A7 的 `**预期**（回归说明）：` 夹注形态曾让旧正则
  失配、整段预期丢失并误标 oracle_missing；先证旧正则失配（红），修复后探针
  缺预期 2→1（TC-A7 收回 4 条 oracle），新增回归测试锁定（绿）。
- 老库 v7→v8 平滑迁移（`test_cases` 表 IF NOT EXISTS 幂等补建）；
  全量 **480 passed**、import-lint OK；A10 单跑通过。
- 注意：ios-local-poc 活库（PID 64634 持有的 v7 库）下次进程重启时才会跑 v8 迁移，
  当前运行进程不受影响；test_cases 是新表，对既有表零影响。

## 需求骨架抽取（S2.2，FR-TD-001）：四段骨架、显式标缺与白名单模型链接

测试设计纵切第二刀：自由格式需求文档 → `{功能点, 规则, 验收标准, 涉及模块}` 骨架与
缺口清单。延续 S2.1 分层：**core 零依赖确定性解析为脊，模型只做语义配对且受白名单
约束**，无模型环境也能产出完整骨架。

### 抽取契约

| 维度 | 行为 |
|---|---|
| 功能点 | `## 功能需求` 节内 `### x.y 模块` 小节的 FR 表：FR-1 / FR-2a 子编号、优先级、删除线或「取消」→ `lifecycle=cancelled`（不进骨架但留缺口）、缺优先级显式记 `priority_missing` |
| 规则 | `## 非功能需求` 两列 NFR 表；其他节（风险/元信息）无 ID/需求列的表整体跳过 |
| 验收标准 | `## 验收标准` 顶格编号列表，跨行续接拼到同一条；无此节 → `ac_section_missing` 且每个功能点一条 present=False |
| 显式标缺 | 没有任何 AC 覆盖的活跃功能点产出 `AcceptanceCriterion(present=False)`，**原文一条不丢、模型一个字不替** |
| 死信 | 列数损坏/编号不符的行进 `RequirementParseError`，单条失败不阻断整批；围栏内假结构整体剔除 |
| 模型链接 | 模型只返回 `{ac_index, fr_ids[]}` 配对；未知 FR ID/越界序号/形状非法 → 整包 `SkeletonValidationError` fail-closed（同问答引用白名单纪律）；模型不可用/降级 → 保留确定性结果并计 `link_degraded_docs`，绝不编造 |
| 幂等 | URN = 文档 external_id + FR/NFR ID；hash 增量跳过，重灌按 doc_urn 整组替换不留幽灵；schema v9 新表 `requirement_skeletons` |

### 运行方式

```bash
# 自动门禁 A11：合成夹具 --full 全链路（offline profile 带录制模型，确定性断言形状；
# 夹具故意带损坏行，worker 按设计 exit 1，门检验 JSON 形状）：
TESTPILOT_PROFILE=config/profiles/offline-demo.yaml \
  .venv/bin/python -m testpilot_workers.ingest_requirements --full

# 真档验证（非 CI 门禁；语料仓只读、路径参数化、不拷原文、探针不写业务库）：
.venv/bin/python scripts/requirement_probe.py \
  ~/Documents/Code/app-calendar-alarm/docs/V1需求文档-强提醒闹钟.md
# 附加本机 Ollama 链接验收（hermetic 内存验证，失败按拒绝/降级记录）：
.venv/bin/python scripts/requirement_probe.py <需求.md> --link-model
```

### 验收证据（2026-11-09，V1 真档）

- 确定性探针：**34 个 FR 标记 → 34 解析、9 个 NFR → 9，零死信零漏解析**；
  FR-7 取消态正确识别；V1 §7 七条产品口径不带 FR ID，纯确定性下 33 个活跃功能点
  全部显式标缺——如实暴露「验收口径与需求条目未机器链接」的规格现状。
- 真模型 hermetic 验收（qwen2.5:7b）：链接载荷**整包通过白名单（零幻觉 ID）**，
  保守链上 16/33（强提醒主链路 FR-2 系列/FR-9 系列/起床 FR-16~19 等），
  剩余 17 个为真实规格缺口（生日 CRUD、快速闹钟剪除、首页外壳等确实没有产品级 AC）；
  报告 `out/eval/requirement-probe.{json,md}`（gitignore）。
- 老库 v8→v9 平滑迁移有专门测试；全量 **508 passed**（S2.2 净增 28）、
  import-lint OK；A11 单跑通过，不触碰 8765 端口。
