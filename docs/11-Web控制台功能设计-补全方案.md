# 11 · Web 控制台功能设计（补全方案）

> 状态：**v1.1 评审结论已纳入，待启动 W1**（2026-11-12）｜v1.0 评审五问结论见 §13，
> 落地按 §10 的 W1–W5 顺序切片，工期评估见 §14，落地时另开 handoff 任务文件
> 关联：[00 总体设计](00-Demo总体设计-智能研发测试助手.md) §6 演示脚本 · [05 角色协作](05-角色协作与落地推进手册.md) ·
> [06 指标体系](06-核心评测指标体系.md) §7 · [08 需求说明书](08-需求说明书.md) FR-UI/FR-WR/NFR ·
> [09 术语表](09-术语表（人话版）.md)
> 范围约束：本文档只覆盖 **web 控制台及其直接依赖的只读/编排 API、演示数据引导（bootstrap）**；
> 不改变既有核心领域模型与 S0–S4 已交付行为，除明确标注的一处（取消执行的审批化，见 6.5.4）外不做行为变更。

---

## 0. 关键决策（2026-11-12 已与用户确认）

| # | 决策点 | 结论 |
|---|---|---|
| D1 | 权限控制深度 | **角色模拟 + 接口守卫**：前端角色切换器（无真实登录），后端按 `X-Actor-Role` 头做接口级守卫，越权 403 并落审计；不做多租户/真实账号体系（SRS §14 P2 不做） |
| D2 | CI/工作流动作的 web 形态 | **Web 编排 API 封装**：新增动作类 API 在服务端进程内复用既有 service/账本/幂等/审批约束，CLI 全部保留；演示全程可不切终端 |
| D3 | 落地节奏 | **5 个垂直切片 W1–W5 顺序执行**，每切片带门禁探针与红证要求，延续 S0–S4 切片纪律 |
| D4（评审定） | 演示数据来源 | **不做合成播种，改为真实管线引导（demo bootstrap）**：用既有 CLI/worker 对 app-calendar-alarm 真实仓跑完整链路，每行流程记录都是真实管线产物；仅 CI 事件信封与工单适配器按 SRS 设计保留模拟形态，显式徽章区分，详见 §8 |
| D5（评审定） | 图表选型 | **成本最低组合**：引入 Recharts 一个依赖画折线（golden 曲线/延迟趋势），统计卡/混淆矩阵/状态分布全部 HTML+CSS 表格与色块实现，零额外图表代码 |
| D6（评审定） | 优先级 | W1→W5 严格顺序；docs/10 挂账的五条触发式任务不插队，W5 总验后再排 |

---

## 1. 背景与目标

S0–S4 已交付问答、测试设计、执行归因、保鲜影响面四条纵切的**后端能力与部分页面**，
但 10 分钟演示脚本（docs/00 §6）的第 3、4、6 爆点目前大量依赖 CLI 和临时探针库，
操作者无法在浏览器里自助走完整体功能。本方案回答两个问题：

1. 哪些流程已有 web 入口、哪些缺失（证据盘点见 §2）；
2. 缺失部分补成什么样——逐页需求、API 缺口、真实管线引导、切片排期与验收方式。

**设计目标**

- G1 演示闭环：docs/00 §6 六步演示全部可在浏览器完成，CLI 仅作为运维备用通道；
- G2 角色可信：四角色权限矩阵可演示、越权请求被接口拒绝且有审计（NFR-005 越权=0）；
- G3 数字可溯源：看板每个数字带版本四元组（数据/模型/prompt/参数）与计算口径，无基线显式标注（FR-EV-003 AC2）；
- G4 诚实空态：任何页面在无数据时显示"为什么空、怎么造数据（可照跑命令/可点按钮）"，不用假数据冒充真实运行结果；
- G5 架构延续：Next.js 静态导出 + FastAPI 同端口托管不变，新能力优先接既有 API/表，不引入新前端框架。

---

## 2. 现状盘点（2026-11-12 实测）

### 2.1 已有页面（4 页 6 路由，Next 15 静态导出）

| 路由 | 对应需求 | 实测覆盖与缺口 |
|---|---|---|
| `/` 问答页 | FR-UI-001 | 完整：问答/引用/三态标注/反馈/trace/历史；有 `acl_scope` 下拉但无角色概念 |
| `/approvals` | FR-UI-002、FR-UI-006 前半 | 用例草稿队列、diff/oracle 来源、采纳改拒、审批账本、outbox 派发、**停用开关已在本页**；仅覆盖 R2 用例回写一类审批，无 R3 统一收件箱、无审计筛选页 |
| `/runs`、`/runs/detail` | FR-UI-003 部分 | 账本列表/过滤、归因五候选卡、证据 ref 文本、取消按钮；**缺**：日志查询条件、重跑按钮、证据包下载、工单草稿审批联动；取消执行目前直发未走 R3 审批（与 FR-WR-001 文字不符，见 6.5.4） |
| `/knowledge` | FR-UI-004 主体 | 文档/切块/stale/实体/治理编辑；**未接**：关系与证据、影响面分析、精度回算、盘点导出四个已上线端点 |

全局问题：**无统一导航壳**，页面间靠各自的"返回"链接；无页面级权限控制；无统一空态组件。

### 2.2 后端 API 覆盖矩阵（6 域约 40 端点）

| API 域 | 前缀 | 端点数 | web 接入情况 |
|---|---|---|---|
| 问答 | `/api/qa`、`/api/config` | 8 | 全接（`/qa/feedback/summary` 已封装未使用） |
| 审批 | `/api/approvals` | 6 | 队列/决策/记录/outbox/写策略已接 |
| 执行 | `/api/runs` | 4 | 列表/详情/取消已接；无重跑、无制品下载端点 |
| 知识 | `/api/knowledge` | 11 | 接 6 个；`relations`、`relations/{urn}/evidence`、`impact`、`impact/precision`、`inventory-export` 未接 |
| 规则资产 | `/api/assets` | 6 | **零接入**（L3 资产、few-shot 晋升、版本/diff） |
| Bad case | `/api/bad-cases` | 6 | **零接入**（台账/stats/分诊/晋升/回归，FR-EV-005） |
| 评测/指标 | — | 0 | **无 API**：`eval_runs` 表与 `out/eval/eval-*.json`（实测 31 份）无读取通道 |
| CI 事件 | — | 0 | **无 API**：事件只能 `testpilot-ingest --replay-events` 与 gate probe 驱动；`events` 表实测 155 行、`dead_letters` 表无查看入口 |
| 审计 | — | 0 | **无统一 API**：审计数据散在 approvals/outbox/feedback/知识治理 raw_json |

### 2.3 数据可得性（sqlite 实测，2026-11-12）

| 表 | offline-demo（app 默认 profile） | ios-local-poc（真机 profile） | 说明 |
|---|---|---|---|
| qa_runs / retrieval_events / verification_events | 234 / — / — | 31 / 55 / 53 | 问答演示数据充足 |
| eval_runs / eval_cases | 24 / 4 | 0 / 0 | 看板在真机 profile 无数据 |
| requirement_skeletons / test_points / test_cases / oracle_suggestions | 1 / 16 / 7 / 14 | 0 全空 | 设计工作流仅默认 profile 有半成品数据 |
| test_runs / jobs / approvals / outbox / bad_cases | 全 0 | 全 0 | 执行/审批/bad case 页面打开即空态 |
| events | 2（仅 DocumentChanged） | 155（DocumentChanged/Deleted） | **两 profile 均无 PipelineFinished 行** |
| dead_letters | 144 | 2 | 死信无 UI 可见 |
| feedback | 60 | 0 | 反馈汇总未上页面 |

结论：后端能力与数据模型齐备度 ≈80%，缺口集中在 **web 入口、只读聚合 API、演示数据连贯性**，
不需要新建复杂后端子系统。但必须配套"真实管线引导"（§8），否则新页面在真机 profile 全是空态。

---

## 3. 设计原则与约束

1. **静态导出不动摇**：页面仍是 Next export 预渲染 + 客户端 fetch，不引入服务端会话/cookie 登录；角色状态存 `localStorage`，每个 API 请求带 `X-Actor-Role` 头。
2. **动作只封装不新造逻辑**：web 动作 API 必须调用既有 core service / 队列 / 策略层，禁止在路由里重写业务规则；R2/R3 动作继续受 `policy.assert_writable` 与 outbox 约束（先账本后副作用）。
3. **只读优先、动作后置**：每个新页面先交付只读视图（低风险、可演示数据资产），动作类按钮在同一切片后半程接入。
4. **诚实标注数据身份**：真实管线产物、模拟信封、录制夹具三类数据在 UI 上有可见标记（徽章，口径见 §8.2），杜绝把夹具/模拟件讲成真实成绩。
5. **轮询替死等**：所有异步动作（事件重放、阶段编排）返回 job 句柄后轮询，不用长连接（SSE 已决策不做）；沿用"旧状态消失"式断言防假绿。
6. **schema 节制**：仅新增一张 `audit_events` 表（schema v15→v16，老库 ALTER 迁移），其余需求全部复用现有 40 张表。
7. **权限失败响亮**：403 不是静默隐藏——按钮对无权限角色隐藏，但任何直达 API 的越权调用返回统一 403 错误体并写 `audit_events`，看板可统计"越权拦截数"（NFR-005 护栏指标）。
8. **术语纪律**：新术语首入同步 docs/09；页面文案沿用既有术语（R0–R4、oracle、stale、outbox、死信、版本四元组）。

---

## 4. 总体信息架构

### 4.1 导航树（全局导航壳，所有页面共用）

```
TestPilot 控制台
├── 问答工作台        /                （现有，FR-UI-001）
├── 测试设计          /design          （新，D-UI-002）
│   └── 需求详情      /design/req/{urn}
├── CI 与执行         /ci              （新，D-UI-003）
│   ├── 执行账本      /runs            （现有增强）
│   │   └── run 详情  /runs/detail?runId=
│   └── 死信队列      /ci#dead-letters
├── 审批中心          /approvals       （升级，D-UI-004）
├── 质量治理          /quality         （新，D-UI-006）
│   ├── Bad case      /quality#badcase
│   ├── L3 规则资产   /quality#assets
│   └── 影响面        /quality#impact
├── 指标看板          /metrics         （新，D-UI-005，FR-UI-005）
├── 知识管理          /knowledge       （现有增强）
└── 审计与写控制      /audit           （新，D-UI-007，FR-UI-006 完整版）
```

### 4.2 全局组件

| 组件 | 说明 |
|---|---|
| `AppShell`（新） | 左侧导航 + 顶栏；当前 profile、适配器模式（recorded/ollama/xcode…，读 `/health`）常驻显示 |
| `RoleSwitcher`（新） | 四角色切换器，明示徽章"**角色模拟，非真实认证**"；切换写 localStorage 并全页刷新数据；无权限导航项灰显带锁图标+说明 |
| `WritePolicyBadge`（新） | 外部写总开关状态常驻顶栏（绿"外部写启用"/红"已停用"），点击跳 `/audit`；复用 `/api/approvals/write-policy` |
| `DemoDataBadge`（新） | 数据身份徽章：真实管线产物（绿）/ 模拟信封·真实内容（黄）/ 录制夹具（灰）三类，按记录元数据渲染 |
| `EmptyState`（新） | 统一空态：原因说明 + 可照跑的 bootstrap 命令 +（有权限时）引导触发/事件重放按钮 |
| `JobProgress`（新） | 异步动作通用进度条：job 句柄轮询、阶段日志、失败原因、死信跳转 |

### 4.3 页面与角色可见性总览

| 页面 | 测试工程师 | 研发工程师 | 测试专家 | 管理员 |
|---|---|---|---|---|
| 问答工作台 | ✅ | ✅ | ✅ | ✅ |
| 测试设计（看） | ✅ | ✅ | ✅ | ✅ |
| 测试设计（触发生成/送审） | ✅ | ❌ | ✅ | ❌ |
| CI 与执行（看） | ✅ | ✅ | ✅ | ✅ |
| 事件模拟投递 / 重跑 | ✅ | ❌ | ✅ | ✅ |
| 审批中心（R2/R3 决策） | ❌ | ❌ | ✅（R2/R3） | ✅（R3/紧急） |
| 质量治理（分诊/晋升） | ✅ 看 | ✅ 看 | ✅ 操作 | ❌ |
| 指标看板 | ✅ | ✅ | ✅ | ✅ |
| 知识治理编辑 | ❌ | ❌ | ✅ | ✅ |
| 审计与写控制/停用开关 | ❌ | ❌ | ✅ 只读 | ✅ 全量 |

---

## 5. 角色与权限设计（D1 落地方案）

### 5.1 角色定义（对齐 docs/05 四类人，不新设角色）

| 角色 key | 中文名 | 现实映射 | 核心职责 |
|---|---|---|---|
| `qa-engineer` | 测试工程师 | 一线测试 | 提问、跑设计流程、提交草稿、执行/重跑、反馈 |
| `dev-engineer` | 研发工程师 | 一线研发 | 看影响面、确认/纠偏归因、只读为主 |
| `qa-expert` | 测试专家 | 审批/裁定人 | R2/R3 审批、bad case 分诊晋升、知识治理、裁定 |
| `admin` | 管理员 | 配置/权限 | 写策略/停用开关、审计、CI 模拟、紧急取消 |

### 5.2 机制

1. 前端：`RoleSwitcher` 选定后写 `localStorage.tp_role`；`lib/api.ts` 统一注入
   `X-Actor-Role: <key>` 请求头；无角色时默认 `qa-engineer` 并提示一次。
2. 后端：新增轻量依赖 `require_role(*allowed)`（FastAPI dependency），按 §5.3 矩阵守卫；
   失败返回 `403 {"error":"role_denied","required":[...],"actual":"..."}`，
   同时写 `audit_events`（action=`api_denied`）。
3. 动作分级不变：R0 只读 / R1 内部草稿自动放行 / R2、R3 需审批 / R4 一律禁止，
   仍由 `core/policy.py::assert_writable` 统一裁决，角色守卫是它前面的第一道门，不替代它。
4. 停用开关（kill switch）继续全局生效：开关打开时所有 R2/R3 写入在策略层被拒，与角色无关；
   开关操作本身仅 `admin` 可做（现状 API 无角色守卫，本方案补上）。
5. 审计留痕：所有写动作、审批决策、403 拦截、停用开关切换统一进 `audit_events`（§6.9）。

### 5.3 动作权限矩阵（接口守卫依据）

| 动作 | 分级 | qa-engineer | dev-engineer | qa-expert | admin |
|---|---|---|---|---|---|
| 问答/检索/反馈 | R0/R1 | ✅ | ✅ | ✅ | ✅ |
| 知识 owner/权威等级编辑 | R1 | ❌ | ❌ | ✅ | ✅ |
| 触发需求抽取/测试点/oracle/用例摄入 | R1 | ✅ | ❌ | ✅ | ❌ |
| 用例草稿送审/撤回 | R1 | ✅ | ❌ | ✅ | ❌ |
| R2 审批决策（用例回写、MR 评论） | R2 | ❌ | ❌ | ✅ | ❌ |
| outbox 手动派发 | R2 | ❌ | ❌ | ✅ | ✅ |
| 执行提交/选择重跑 | R1 | ✅ | ❌ | ✅ | ✅ |
| 取消执行（常规路径，R3 审批化，见 6.5.4） | R3 | 申请 | ❌ | 批准 | 紧急直发+强制审计 |
| R3 工单草稿审批 | R3 | ❌ | ❌ | ✅ | ✅ |
| CI 事件模拟投递/死信重投 | R1 | ✅ | ❌ | ✅ | ✅ |
| bad case 分诊/晋升 golden/回归登记 | R1 | ❌ | ❌ | ✅ | ❌ |
| L3 资产晋升/新版本 | R1 | ❌ | ❌ | ✅ | ✅ |
| 停用开关/写策略修改 | R1 配置 | ❌ | ❌ | ❌ | ✅ |
| 审计流查看 | R0 | ❌ | ❌ | ✅ | ✅ |
| 指标/知识/执行只读浏览 | R0 | ✅ | ✅ | ✅ | ✅ |

### 5.4 验收 AC（权限部分，纳入 W1 门禁）

- AC-P1 同一接口用四种角色头各请求一次，矩阵中每个 ❌ 都得 403，且 `audit_events` 有对应 `api_denied` 行；
- AC-P2 停用开关打开后，任意角色发起 R2/R3 均被策略层拒绝（403/策略错误体），开关状态顶栏即时更新；
- AC-P3 红证：临时删掉某接口的角色依赖，门禁探针断言该接口 403 消失→探针变红，恢复后变绿；
- AC-P4 前端无权限按钮不渲染（非 disabled 伪装），但直接输入 URL 可看到页内"权限不足"空态而非白屏。

---

## 6. 逐页需求

> 编号约定：本文档派生需求编号 **D-UI-xxx**（页面）/ **D-API-xxx**（接口），
> 评审通过后升格入 docs/08 正式编号；每条给出可测试 AC。

### 6.1 全局导航壳与演示工作台（D-UI-001，W1）

**目标**：解决"页面孤岛"，并给操作者一条可点击的演示主线。

- 6.1.1 `AppShell`：§4.1 导航树全量实现；顶栏展示 profile 名、适配器模式（来自 `/health` 的 adapters 投影）、`WritePolicyBadge`、`RoleSwitcher`。
- 6.1.2 问答页 `/` 迁入壳内，现有功能零删减；首页增加"**10 分钟演示路线**"卡片（docs/00 §6 六步映射到具体页面链接，每步标注预计时长与前置数据条件）。
- 6.1.3 `EmptyState` 统一空态首次落地：无数据页面给出原因 + 可复制命令 + 有权限时显示一键动作按钮。
- AC：
  - AC-6.1.1 九条路由（含详情页/404）全部渲染同一导航壳，刷新后角色与当前页保持；
  - AC-6.1.2 故意停掉后端，导航壳仍可渲染，各页显示请求失败空态而非白屏（回归既有 trace 容错纪律）；
  - AC-6.1.3 演示路线卡片六步链接全部可达，未满足数据条件的步骤显式标注"需先跑 bootstrap 引导/事件重放"。

### 6.2 测试设计工作流页 `/design`（D-UI-002，W2）

**目标**：把当前五个 CLI（`testpilot-extract-requirements / generate-testpoints / mine-oracles / ingest-cases` + 审批送审）
串成"需求 → 测试点 → 用例 → oracle → 送审"的可视化流水线，对应 docs/00 §6 第 3 步前半。

**列表视图 `/design`**

- 需求骨架卡片列表（`requirement_skeletons`）：external_id、FR/规则/AC 数、AC 缺失数、gap 数、死信数、分支、indexed_at；
- 顶部流程五段进度条：①需求抽取 ②测试点生成 ③结构化用例 ④oracle 挖掘 ⑤送人审，每段显示该 profile 下产物计数与最近运行时间；
- 动作按钮（受角色矩阵）：`触发需求抽取`、`生成测试点`、`挖掘 oracle`、`摄入用例`，点击走 `JobProgress`（D-API 编排）。

**详情视图 `/design/req/{urn}`**

- 区 A 需求画像：FR/AC/gap 清单与 `linking_source`（骨架来源可溯源）；
- 区 B 测试点表（`test_points`）：fr_id、kind、priority、provenance、text、source_refs 可点开白名单原文；
  支持按 priority/kind 过滤；P0/P1 行高亮，呼应 S2 门禁"P0/P1 漏点=0"；
- 区 C 结构化用例（`test_cases`）：case_id、模块、优先级、lifecycle、步数、oracle 数、
  `oracle_missing` 徽章（缺预期用例显式标红，不静默）；
- 区 D oracle 建议（`oracle_suggestions`）：oracle 类型、期望、source_kind/provenance、
  置信度、status（含 exploratory 探索性出口标记，口径同 docs/10 五术语）；
- 区 E 送审区：勾选草稿 → `送审` 生成 R2 审批单（跳转 `/approvals` 可见）；送审后状态回写可见。

- AC：
  - AC-6.2.1 离线 demo 库（1 需求/16 测试点/7 用例/14 oracle）详情页四区计数与 DB 直查一致（页面渲染后用 sqlite 对账）；
  - AC-6.2.2 以 `qa-engineer` 触发一次测试点生成（录制模式），JobProgress 走到成功，产物计数增加且页面轮询自动刷新；以 `dev-engineer` 调用同一编排接口得 403；
  - AC-6.2.3 红证：构造一条 `oracle_missing=true` 用例，详情页必须显示红色徽章；临时移除徽章渲染逻辑，探针断言失败；
  - AC-6.2.4 编排动作重复点击两次：服务端幂等（同 hash 组不重复建产物，沿用 CLI 的 `--full` 对账语义，web 默认非 full），无重复行；
  - AC-6.2.5 死信数 >0 的需求卡片可点击跳转死信面板并显示原始错误。

### 6.3 CI 中心 `/ci`（D-UI-003，W3）

**目标**：给 FR-CON-005/FR-EX-008 一个浏览器入口，对应 docs/00 §6 第 3 步"CI 触发闭环"与第 4 步事件驱动保鲜。

**区 A 事件流**（`events` 表只读投影）

- 表：event_id、type（PipelineFinished/CommitPushed/DocumentChanged/Deleted…）、source_system、
  status、occurred_at、received_at；支持类型/状态过滤；
- 行点击展开 payload_json（只读 JSON 查看器）与处理结果；PipelineFinished 行提供"跳转关联 run"链接（`idempotency_key = ci-run:{event_id}` 贯通，见 pipeline_loop 既有逻辑）。

**区 B 模拟投递（演示控制台）**

- 下拉选择内置夹具（首批：`tests/fixtures/events/pipeline-finished-42.json` 成功/失败两版 +
  一个缺字段坏事件用于演示死信），也支持粘贴事件 JSON；
- `投递`按钮调 D-API 重放端点（服务端复用 `event_dispatch` + `handle_pipeline_finished`，不经 CLI）；
- 同 event_id 二次投递按钮常驻，用于现场演示幂等（AC1：第二次显示"重复事件已忽略"，无新增 run）；
- 坏事件投递后行出现在区 C 死信表，演示 FR-CON-005 AC2"缺字段进死信不静默"。

**区 C 死信队列**（`dead_letters` 表）

- 表：id、source、error、created_at、payload 摘要；`重投`按钮（修正后重放）与"查看修复指引"；
- 显示死信总数与按 source 分组计数。

**区 D CI 闭环时间线**（选中 PipelineFinished 事件后）

- 事件接收 → 结果解析 → 执行账本 run → 归因结论 → R3 工单草稿（如有）的纵向时间线，
  每节点给跳转链接，直观呈现 runId 贯通（FR-UI-003 核心 AC）。

- AC：
  - AC-6.3.1 投递失败版夹具后，`/runs` 出现一条新 run 且 CI 时间线五节点齐全；同 event_id 再投一次，run 数不增加；
  - AC-6.3.2 投递缺字段坏事件 → 死信表 +1 且事件流可见 failed 状态；重投修正版后死信状态闭环；
  - AC-6.3.3 `dev-engineer` 调投递/重投接口得 403，`audit_events` 留痕；
  - AC-6.3.4 红证：临时破坏 event_dispatch 幂等键，重复投递产生两条 run，探针变红后复原；
  - AC-6.3.5 事件流分页/过滤在 155 行事件库上响应 P95 < 500ms（本地）。

### 6.4 执行中心增强（D-UI-003b，W3；现有 `/runs` 路线）

在现有列表/详情页基础上补齐 FR-UI-003 未覆盖 AC：

| 缺口 | 需求设计 |
|---|---|
| 日志查询条件 | 详情页新增"os_log 查询条件"卡：udid、ISO 时间窗（起止）、filter 谓词，只读展示并生成可复制的 `log stream` 命令（真机命令口径见 docs/03 §3.6）；不直接执行主机命令 |
| 证据包/制品 | 制品 URN 列表（`artifacts`）每项给"白名单代理下载/查看"入口（新 D-API 只读端点，URN 校验 fail-closed，沿用 QA 原文代理的白名单守卫） |
| 选择重跑 | `重跑`按钮：弹窗显示 rerun_budget 剩余配额与本 run 已重跑次数，确认后调重跑端点（服务端复用执行配额守卫，第三笔超额拒绝要在按钮上提前置灰并说明） |
| 工单草稿联动 | 归因结论为真缺陷/需人工时，显示"生成 R3 工单草稿"入口 → 跳审批中心；草稿含 runId、证据 URI、未覆盖层声明（FR-WR-003 AC2） |
| 未覆盖层 | 现有未覆盖层声明保留并在证据卡顶部固定展示（环境差异意识：哪层已验、哪层只能生产验） |

- AC：
  - AC-6.4.1 重跑两次后第三次按钮置灰且直调 API 返回配额拒绝（既有 S3.5 门⑤行为在 UI 可演示）；
  - AC-6.4.2 制品代理对白名单外 URN 返回 4xx 且不泄漏文件系统路径（红证：构造越界 URN）；
  - AC-6.4.3 详情页所有异步数据分段加载，单段失败不拖垮整页（沿用 Suspense/可选字段兜底纪律）。

### 6.5 统一审批中心 `/approvals`（D-UI-004，W3）

现状页升级，保留全部已有能力。

- 6.5.1 **双收件箱 Tab**：`R2 用例回写/MR 评论`（现有队列迁移而来）与 `R3 工单/执行取消`（新增）；
  每条审批卡统一展示：目标系统、请求体 diff、幂等键、回滚说明、动作分级、申请人/时间（FR-WR-001 AC1 全字段）。
- 6.5.2 决策三态（批准/修改后批准/拒绝）+ reason 必填校验；决策即时落库，未批准不进正式库（既有语义不变）。
- 6.5.3 outbox 面板保留并增强：状态筛选（pending/sent/confirmed/failed）、failed 人工重试沿用原幂等键、
  幻影记录（账本有外部无）对账结果可见（FR-WR-002）。
- 6.5.4 **取消执行审批化（唯一行为变更，需评审）**：现状 `POST /api/runs/{id}/cancel` 直发取消，
  与 FR-WR-001"R3（取消执行）必须审批"的字面要求不一致。改为：
  - 常规路径：取消按钮生成 R3 审批单 → `qa-expert` 批准后由 outbox 派发取消（协作取消标记机制不变）；
  - 紧急路径：`admin` 可直发取消，但强制填原因且写 `audit_events`（action=`emergency_cancel`），
    看板与审计页单列紧急取消次数；
  - 兼容性：API 保留原路径，无审批单直发时按角色返回 403/409 并提示走申请；既有测试同步改口径。
- 6.5.5 停用开关从本页抽到顶栏 `WritePolicyBadge` + `/audit` 主页操作区（本页保留只读状态条），
  开关接口加 `admin` 角色守卫。
- AC：
  - AC-6.5.1 R2/R3 两类审批单在两个 Tab 互不串列，决策后 outbox 生成对应分级记录；
  - AC-6.5.2 `qa-engineer` 调决策接口 403；`qa-expert` 全流程可操作；
  - AC-6.5.3 停用开关打开时批准动作本身可成功，但派发在策略层被拒且原因可见（验证 kill switch 不拦审批只拦外部写）；
  - AC-6.5.4 红证：常规角色直发 cancel 被拒；去掉守卫后探针变红；紧急取消无原因提交被拒。

### 6.6 指标看板 `/metrics`（D-UI-005，W4；FR-UI-005 完整交付）

**数据源**：新增只读聚合 API（D-API，§7），读 `eval_runs/eval_cases/qa_runs/feedback/bad_cases/
ingestion_runs/audit_events` 表与 gate probe 封存报告；不做实时计算，聚合在请求时做且限最近 N 条。

**版面（按 docs/06 §7 十指标对齐，不新增自创指标；图表按 D5：折线用 Recharts，其余 HTML/CSS）**

| 区块 | 内容 | 口径/约束 |
|---|---|---|
| A 总览卡 | 十指标最小集（golden 通过率、引用准确率、Recall@K、忠实度、拒答正确率、测试点覆盖/漏点、归因准确率/真缺陷 recall、采纳率/撤销率、单请求成本与延迟、摄入延迟 P95） | 每卡：当前值 + 版本四元组 + 口径 tooltip + 最近一次 eval_run 链接；**无基线显式"无基线"徽章，禁止百分比结论**（FR-EV-003 AC2） |
| B golden 曲线 | 按 eval_runs 时间序列的通过率/引用准确率/Recall@K 折线，降级路径（degraded_path）点用空心标记 | 悬停显示四元组；降分报告照常展示不隐藏（FR-EV-002 AC1） |
| C 成本与延迟 | 单请求 token 成本、延迟中位数/P95 前后对比；模型路由/缓存前后成对报告（docs/06 §8 反模式约束：成本与质量成对出现） | 数据来自 eval_runs.metrics 与 qa_runs |
| D 归因质量 | 6×6 混淆矩阵热力图（口径同 A22 封存）、acc/recall/误误导/充分率四门禁值 + seal hash | 标注"生产人工抽检口径"弃权率 |
| E 保鲜与摄入 | 摄入延迟两段 P95、stale 误命中、reindex 耗时、推荐精度（A24 四门禁） | 带阈值线（300s/600s/0.8 等），越界变红 |
| F bad case 分类 | 七态分布（open→fixed 全状态机）、闭环周期、晋升 golden 数 | 读 `/api/bad-cases/stats`（已存在） |
| G 护栏与越权 | 无引用结论/白名单外调用/越权写/越层声称/幂等重复/越权拦截六计数（FR-EV-006 + NFR-005） | 任一非零显示红色，点击跳审计/死信 |
| H 评测报告库 | eval_runs 历史表：run id、set_id、四元组、报告 md/json 下载 | 报告文件只读代理，目录由 profile 配置 |

- AC：
  - AC-6.6.1 看板每个数字可下钻到计算口径说明与原始记录（eval_run 详情/qa_run 列表），抽检 5 个指标手工复算一致；
  - AC-6.6.2 空库（新建 profile）打开看板：十卡全部"无基线/无数据"空态，无任何 NaN/0% 冒充；
  - AC-6.6.3 红证：故意把某指标聚合口径改错（如召回分母 +1），看板数值与探针期望值分叉变红；
  - AC-6.6.4 降级路径 eval_run 在曲线上视觉可区分，且图例说明；
  - AC-6.6.5 看板加载不触发任何模型调用/评测执行（纯只读，防止演示时点出高额开销）。

### 6.7 质量治理页 `/quality`（D-UI-006，W4）

把三个"后端已完成、web 零入口"的能力合为一页三 Tab：

- **Tab 1 Bad case 闭环（FR-EV-005）**：列表（七态徽章 + 分诊状态机按钮，非法迁移由后端拒绝并提示）、
  stats 卡、分诊/晋升 golden/回归登记操作（仅 `qa-expert`）；每条展示与 golden/回归 eval_run 的关联；
- **Tab 2 L3 规则资产（KNO-002）**：`/api/assets` 资产列表、版本修订历史、版本 diff 视图、
  few-shot 晋升记录（含 author/note）；偏好回流样本计数；
- **Tab 3 影响面分析（FR-FRS-003）**：符号/文件输入 → 影响面结果（复用 `POST /api/knowledge/impact`）、
  精度回算（`impact/precision`）、用例↔符号关系浏览与证据（S2.6b 能力，端点已存在）。
- AC：
  - AC-6.7.1 bad case 状态机非法迁移在按钮层置灰、直调 API 返回 4xx 且页面有错误提示；
  - AC-6.7.2 资产 diff 页面与 `GET /assets/{id}/diff` 返回逐字一致；
  - AC-6.7.3 影响面查询为空时显示"无命中"而非报错（输入一个不存在的符号验证）。

### 6.8 知识管理页增强（D-UI-006b，W4）

`/knowledge` 现有内容不动，补接五个闲置端点：

- 文档行增加"关系"展开（relations + evidence 抽屉，复用 S2.6b 关系证据）；
- 顶部增加"影响面查询"入口（与 6.7 Tab3 同组件复用）；
- `导出盘点`按钮（`inventory-export`，下载文件，保留 FR-UI-004"盘点表可导出"AC）；
- stale 区增加 reindex 状态刷新与"高价值自动 reindex"标记说明（S4.2 语义）。
- AC：关系证据抽屉对无证据关系显示空态；导出文件内容与 API 响应字节一致。

### 6.9 审计与写控制页 `/audit`（D-UI-007，W5；FR-UI-006 完整版 + FR-WR 留痕收口）

- 区 A 写控制操作台（仅 admin）：停用开关（write-policy）、profile 外部写启用状态（只读）、
  R0–R4 分级说明与当前策略版本；
- 区 B 统一审计流（新表 `audit_events`，聚合投影）：时间、actor（角色+操作人标识）、
  action、动作分级、目标系统、结果（允许/拒绝/策略拦截/紧急取消）、关联幂等键/审批 id/run id；
  支持按项目、人、动作类型、结果筛选并导出（FR-UI-006"按项目/人筛选"AC）；
- 区 C 外部写对账：outbox 全量记录视图 + 幻影记录计数 + 对账修复动作记录；
- 区 D 越权拦截榜：`api_denied` 计数按角色/接口分组，支撑 NFR-005 汇报。

`audit_events` 建议字段（schema v16）：
`event_id PK、occurred_at、actor_role、actor_name、action、action_level、target_system、
result、related_ref、payload_hash、raw_json`；写入点：角色守卫 403、写策略切换、审批决策、
outbox 派发结果、紧急取消、知识治理编辑（治理历史仍保留 raw_json 双写）。

- AC：
  - AC-6.9.1 一次完整"投递事件→生成草稿→R2 批准→派发→拒绝尝试"演练后，审计流可按人筛出全程且不断链；
  - AC-6.9.2 非 admin/qa-expert 访问该页路由得页内权限空态，直调审计 API 得 403；
  - AC-6.9.3 schema v15→v16 在含数据老库上 ALTER 迁移成功且既有 40 表行数零变化（延续零损伤迁移纪律）；
  - AC-6.9.4 红证：临时绕过一次角色守卫，越权拦截榜不增、探针变红。

---

## 7. 后端 API 缺口清单（D-API）

> 全部挂在现有 FastAPI app，前缀风格对齐现状；标 ★ 的为动作类（受角色矩阵 + 策略层双重约束），
> 其余只读。实现时每个新端点配契约测试，异常映射走既有统一错误体。

| 编号 | 方法/路径 | 用途 | 角色 | 复用的既有能力 | 切片 |
|---|---|---|---|---|---|
| D-API-001 | `GET /api/me` | 当前角色/能力位回显、profile 模式 | 全部 | `/health` 投影 + 角色配置 | W1 |
| D-API-002 | （中间件）`X-Actor-Role` 守卫统一 403 体 + audit 写入 | 权限基座 | — | 新 dependency + audit 表 | W1 |
| D-API-003 | `GET /api/design/requirements`、`/api/design/requirements/{urn}` | 骨架列表/详情 | 全部 | store 直读四表 | W2 |
| D-API-004 | `GET /api/design/requirements/{urn}/test-points`、`/test-cases`、`/oracles` | 设计产物三级联查 | 全部 | store 直读 | W2 |
| D-API-005 ★ | `POST /api/design/run-stage` | 编排四阶段（extract/testpoints/oracles/cases），入队返 job 句柄 | qa-engineer/qa-expert | jobs 表 + SqliteQueue + 既有 worker service 函数 | W2 |
| D-API-006 | `GET /api/jobs/{job_id}` | 编排任务状态轮询 | 本人可见 | jobs 账本 | W2 |
| D-API-007 | `GET /api/events` | 事件流分页/过滤 | 全部 | events 表 | W3 |
| D-API-008 ★ | `POST /api/events/replay` | 夹具/粘贴 JSON 重放（含 PipelineFinished 闭环） | qa-engineer/qa-expert/admin | `event_dispatch` + `pipeline_loop.handle_pipeline_finished` | W3 |
| D-API-009 | `GET /api/dead-letters`、★`POST /api/dead-letters/{id}/replay` | 死信查看/重投 | 查看全部/重投三角色 | dead_letters + dispatch | W3 |
| D-API-010 ★ | `POST /api/runs/{run_id}/rerun` | 选择重跑（配额前置校验） | qa-engineer/qa-expert/admin | execution_service 配额/幂等 | W3 |
| D-API-011 | `GET /api/runs/{run_id}/artifacts/{urn:path}` | 制品/证据白名单代理下载 | 全部 | 证据 Provider + 白名单守卫 | W3 |
| D-API-012 ★ | `POST /api/runs/{run_id}/cancel-request` | R3 取消申请单（审批化） | qa-engineer 申请 | approvals/outbox 既有机制 | W3 |
| D-API-013 | `GET /api/metrics/overview`、`/golden-curve`、`/cost-latency`、`/guardrails` | 看板聚合（只读） | 全部 | eval_runs/qa_runs/audit 聚合查询 | W4 |
| D-API-014 | `GET /api/eval/runs`、`/api/eval/runs/{id}`、报告文件代理 | 评测报告库 | 全部 | eval_runs 表 + out/eval 目录（profile 配置） | W4 |
| D-API-015 | `GET /api/audit/events` | 统一审计流筛选/导出 | qa-expert/admin | audit_events（新表） | W5 |
| D-API-016 | 既有写策略/审批/outbox 端点补角色守卫 | 权限收口 | 按矩阵 | 既有逻辑不动 | W1/W3 |
| D-API-017 | `GET /api/bootstrap/status`、★`POST /api/bootstrap/run`（见 §8） | 引导状态面板与触发（不绕过 HITL，审批步仍由真人完成） | admin 触发/全员只读 | bootstrap 编排（只调既有 CLI/service） | W1 |

明确不做：SSE/WebSocket 推送（轮询替代）、真实认证 token、多项目切换、任何 R4 动作的 HTTP 入口。

---

## 8. 演示数据方案：真实管线引导（demo bootstrap，D4 评审定案）

### 8.1 问题与定案

§2.3 实测：真机 profile 的**语料层是真实且充足的**（85 文档/1427 切块/532 实体/573 关系/
155 个真实 git 事件/31 条真实问答 run），但**流程产物表全为 0**——S2–S4 探针都在临时
hermetic 库运行，产物未回流主 profile 库。评审定案：**不造合成数据，改为用既有 CLI/worker
对 app-calendar-alarm 真实仓把完整链路真实跑一遍**，让每条流程记录都是真实管线产物
（真实模型输出、真实 xcodebuild、真实评测、真人决策）。

### 8.2 数据身份三分类（UI 徽章口径，替代原"演示播种"单徽章）

| 身份 | 含义 | 典型数据 | 徽章 |
|---|---|---|---|
| 真实管线产物 | 对真实仓跑真实管线产生，账本可逐行回溯命令与时间 | 语料/测试点/用例/oracle/审批/test_run/归因/eval_run/bad case/QA run | 绿"真实产物" |
| 模拟信封 | SRS 本就设计为 JSON  replay 的外部事件外壳，**信封手工构造、内容引用真实制品** | PipelineFinished 事件 JSON（payload_uri 指向真实 xcresult） | 黄"模拟信封·真实内容" |
| 录制/夹具 | P2 不做真实对接的外部系统，按 FR-CON-004 用夹具 | JSON 工单（2 条）、offline 模式的录制模型响应/录制 xcresult | 灰"录制夹具" |

红线：任何数字在看板/页面上必须可看出身份；gate probe 封存成绩与引导产物分区展示，
引导产生的 eval_run 不得覆盖 golden 封存集（呼应 docs/10 诚实红绿灯纪律）。

### 8.3 引导流程（新增 `testpilot-bootstrap-demo` CLI，workers 包第十三个入口）

按 docs/00 §6 演示叙事编排既有命令，本身只做**顺序编排与幂等检查，不直接写业务表**：

| 步   | 动作（复用既有能力）                                                                                     | 产物（真实）                                               | 前置           |
| --- | ---------------------------------------------------------------------------------------------- | ---------------------------------------------------- | ------------ |
| 1   | `testpilot-ingest` 增量摄入真实仓（已跑过则 hash 对账秒过）                                                     | 语料/事件/stale 现状                                       | ollama、仓只读挂载 |
| 2   | `testpilot-extract-requirements` 抽真实 PRD/需求文档                                                  | requirement_skeletons                                | 步 1          |
| 3   | `testpilot-generate-testpoints`（真实 ollama 生成）                                                  | test_points（含 P0/P1）                                 | 步 2          |
| 4   | `testpilot-ingest-cases` + `testpilot-mine-oracles`                                            | test_cases、oracle_suggestions（含真实 oracle_missing 标记） | 步 3          |
| 5   | 真人在 `/approvals` 决策（引导脚本只列出待审清单，不代替人点头）                                                        | approvals + outbox（dry-run）                          | 步 4          |
| 6   | `testpilot-runner submit/run-once` 跑**真实模拟器 xcodebuild**（AlarmKitSpike 现成测试）→ 真实 xcresult 解析入账 | jobs/test_runs/制品 URN                                | 模拟器可用，约数分钟   |
| 7   | 投递 PipelineFinished 事件（模拟信封，引用步 6 真实 xcresult），跑 `pipeline_loop` 闭环                            | events、CI run、归因 attributions、R3 工单草稿                | 步 6          |
| 8   | `testpilot-eval` 跑 golden_s1 真实评测（30 题，本地模型）                                                   | eval_runs/eval_cases（含四元组与降级标记）                      | ollama 暖身    |
| 9   | 从步 8 真实失败案例 + QA 反馈建 bad case 并走一次分诊/晋升                                                        | bad_cases、feedback                                   | 步 8          |

### 8.4 双模式（应对现场环境差异，模式差异只在适配器层，账本语义一致）

- `--mode full`（默认，录制备份用）：步 6 真 xcodebuild、步 8 真 ollama 30 题，全真实，耗时约 20–40 分钟；
- `--mode lite`（无模拟器/离线现场用）：步 6 切录制 xcresult 适配器、步 8 切 golden_recorded_smoke
  录制题集（秒级），其余步 1–5、7、9 仍真实；产物带灰徽章标明录制段，**不允许把 lite 的 eval 数字讲成模型成绩**。

### 8.5 幂等、安全与清理

1. 全流程内容寻址/hash 对账，重复执行：已完成步显示 unchanged 跳过，不增生（沿用 ingest `--full` 语义，bootstrap 默认非 full）；
2. 只往**运行时账本**写真实产物，语料仓保持只读；外部写全部 dry-run 落 outbox，不触任何真实外部系统；
3. `--reset` 仅清理带 `bootstrap_run_id` 标记的本批次行，先备份库到 `store/<profile>/backups/`，
   真实日常数据（如既有 31 条 qa_runs）零影响；
4. 步 5 审批与步 9 bad case 决策必须由真人角色操作，脚本不提供"自动批准"参数（HITL 红线不可脚本绕过）；
5. web 端 D-API-017 改为"引导状态面板"（只读进度 + 有权限时触发后台模式），不做绕过审批的一键造数。

- AC：
  - AC-8.1 在 ios-local-poc 空流程库跑 full 引导后，§4.1 全部页面无空态；逐表计数与引导日志的步骤产出对账一致，抽查 5 条记录可回溯到具体 CLI 调用与真实源文件；
  - AC-8.2 连跑两次 bootstrap，已完成步骤零增生；`--reset` 后库 hash 与引导前备份一致；
  - AC-8.3 红证：模拟信封/录制段产物若缺失身份徽章，A26 探针失败；lite 模式 eval 区出现无徽章数字同样失败；
  - AC-8.4 红证：尝试给 bootstrap 加自动审批参数的变体无法通过 HITL 守卫测试（审批表无真人 reviewer 的行必须为 0）；
  - AC-8.5 步 7 同 event_id 重放第二次，CI run 零增生（复用既有 event_dispatch 幂等，本 AC 与 AC-6.3.1 互证）。

---

## 9. 非功能需求

| 项 | 要求 |
|---|---|
| 浏览器 | 最近两个版本 Chrome/Edge/Safari；演示分辨率 1440×900 优先；不做移动端适配（P2） |
| 渲染架构 | 维持静态导出，新增路由全部可 `npm run build` 预渲染；`tsc --noEmit` 零错；构建失败不阻断 API 启动（既有挂载守卫不变） |
| 性能 | 本地列表页 P95 < 500ms（千行内）；看板聚合请求 P95 < 1s；全量轮询间隔 ≥3s 且页面不可见时暂停 |
| 容错 | 所有 fetch 段独立兜底；403/4xx/5xx 三类错误体分样式渲染；后端不可达不白屏 |
| 可访问性 | 表单控件有 label、颜色不作为唯一状态载体、键盘可达（演示现场可能投屏操作） |
| 日志 | 新 API 用固定 logger `testpilot.orchestrator.webapi`；角色拒绝/编排动作/重放全结构化，docs/03 补抓取谓词 |
| 隐私 | 不新增任何用户个人数据；actor_name 仅自由文本操作人标识，默认用角色名 |
| 测试 | 纯逻辑（权限矩阵、聚合口径、状态机）抽 hostless 单测；页面契约沿用现有 API 测试模式；每切片红证对照 |

---

## 10. 分切片排期（W1–W5，D3 决策）

> 节奏按每切片 2–3 天设计（单人 POC），每切片结束：pytest 全绿、import-lint、tsc/build、
> 新增 gate probe 项、verify_s0 计数更新、README/docs/09 同步、handoff 归档。push 仍等用户明确指令。

### W1 基座：导航壳 + 角色权限 + 审计表 + 引导 CLI

- 范围：AppShell/RoleSwitcher/WritePolicyBadge/EmptyState/三身份数据徽章；D-API-001/002/016/017；
  `audit_events` 表与 schema v16 迁移；`testpilot-bootstrap-demo` 引导 CLI（§8，full/lite 双模式）；
- 门禁：AC-P1~P4、AC-6.1、AC-8.1~8.5；新探针 **A25 web_role_probe**（四角色 × 关键接口矩阵 + 红证）；
- 风险：schema 迁移是本方案唯一 DDL，优先做并在两 profile 现库演练；bootstrap full 模式依赖模拟器与 ollama，
  先在本机跑通并录一份 lite 兜底。

### W2 测试设计工作流

- 范围：`/design` 两视图、D-API-003~006、JobProgress；送审联动既有审批 API；
- 门禁：AC-6.2 全部；编排幂等红证；离线 demo 库计数对账；tsc/build 路由数更新。

### W3 CI 中心 + 执行增强 + 统一审批

- 范围：`/ci` 四区、D-API-007~012、死信重投、`/runs` 五缺口、`/approvals` 双 Tab、
  取消执行 R3 审批化（含既有测试改口径）；
- 门禁：AC-6.3/6.4/6.5；幂等双投红证、配额红证、白名单 URN 红证；
  重点回归 S3/S4 既有探针（A23/A24）零退化。
- 风险最高切片：取消审批化是唯一行为变更，单独 commit，回滚方案写明。

### W4 指标看板 + 质量治理 + 知识接线

- 范围：`/metrics` 八区、D-API-013/014、`/quality` 三 Tab（接齐 badcase/assets 12 个闲置端点）、
  `/knowledge` 五端点接线；
- 门禁：AC-6.6/6.7/6.8；指标手工复算 5 项、空库空态、聚合口径红证；
  看板只读红线：抓网络请求证明无模型调用。

### W5 审计页 + 总验收口

- 范围：`/audit` 四区、D-API-015、docs/08 正式编号升格（D-UI/D-API → FR-UI-007…）、
  docs/00 演示脚本改写为浏览器版、README 演示节；
- 门禁：AC-6.9；**A26 web_full_gate_probe**：以两 profile 各跑一遍"六步演示剧本"全自动巡检
  （路由可达性 + 关键按钮角色矩阵 + 数据身份徽章 + 审计不断链）；verify_s0 总验；
  全部红证复原零残留、schema 定格 v16。

---

## 11. 需求追溯矩阵

| 源头需求 | 本方案条目 | 页面 | 新增/复用 API | 切片 |
|---|---|---|---|---|
| FR-UI-001 | 6.1 | `/` | 复用 | W1 |
| FR-TD-001~004、FR-CON-003 | 6.2 | `/design` | D-API-003~006 | W2 |
| FR-CON-005、FR-EX-008 | 6.3 | `/ci` | D-API-007~009 | W3 |
| FR-UI-003、FR-EX-002/007、FR-WR-003 | 6.4、6.5 | `/runs*`、`/approvals` | D-API-010~012 + 复用 | W3 |
| FR-WR-001/002、FR-UI-002 | 6.5 | `/approvals` | 复用 + D-API-016 | W3 |
| FR-UI-005、FR-EV-002/003/006、docs/06 §7 | 6.6 | `/metrics` | D-API-013/014 | W4 |
| FR-EV-005、KNO-002、FR-FRS-003 | 6.7、6.8 | `/quality`、`/knowledge` | 复用闲置 12 端点 | W4 |
| FR-UI-006、NFR-005、FR-WR 全链 | 5、6.9 | `/audit`、全局 | D-API-001/002/015/016 | W1/W5 |
| docs/00 §6 演示可操作性 | 6.1、8 | 全局 | D-API-017 | W1 |
| FR-UI-004 | 6.8 | `/knowledge` | 复用 | W4 |

---

## 12. 明确不做（本方案边界）

1. 不做真实账号/密码/SSO/会话 token，不做多项目多租户组织权限（SRS §14 P2）；
2. 不做 SSE/WebSocket 实时推送，不做在线触发真实 xcodebuild 的 web 按钮（真机执行仍走 runner CLI/runbook，web 只做录制/账本层演示与重放）；
3. 不做 R4（删数据/生产操作）任何入口；不做 web 端模型训练、配置热编辑 profile 文件；
4. 图表依赖已定案（D5）：仅引入 Recharts 画折线，统计卡/混淆矩阵/状态分布用 HTML+CSS 色块，不引入组件库/UI 框架；
5. 不改动语料仓与既有评测封存集；引导产物与封存成绩全程标识隔离。

---

## 13. 评审结论（2026-11-12，五问全部闭环）

1. **取消执行审批化（6.5.4）**：✅ 认可，按 R3 审批化 + admin 紧急通道落地，W3 单独 commit；
2. **schema v16 `audit_events` 表**：✅ 接受，W1 随迁移先行，老库 ALTER 零损伤为硬 AC；
3. **演示数据**：✅ 定案为真实管线引导（§8 重写）：不造合成数据，用真实仓+真实管线产出，
   仅 CI 事件信封/工单按 SRS 保留模拟形态并徽章区分；用户原话"直接用 app-calendar-alarm 的工程，
   数据真实存在不是假数据"；
4. **图表选型**：✅ 成本最低方案（D5）：Recharts 画折线，其余 HTML/CSS；
5. **优先级**：✅ W1→W5 严格顺序，docs/10 五条触发式挂账不插队，W5 后再排。

## 14. 工期评估（单人，2026-11-12 估算）

### 14.1 估算口径

参照本仓历史速度（S2 六切片约 2 天、S3 五切片约 2 天、S4 五切片约 1 天，均含测试/红证/探针/文档），
web 切片额外含 tsc/build、组件与页面契约测试。以下为**净开发人日**，含单测、红证对照、
探针与文档同步，不含评审等待与返工。

| 切片 | 范围摘要 | 估算 | 主要工作量与风险 |
|---|---|---|---|
| W1 基座 | 导航壳/角色切换/守卫中间件/审计表 v16 迁移/bootstrap CLI（full+lite）/A25 探针 | **2.5–3 人日** | v16 迁移需在两 profile 现库演练；bootstrap full 跑通真实 xcodebuild+ollama 链路有环境调试成本（最高风险项） |
| W2 测试设计 | `/design` 两视图 + 4 只读端点 + 1 编排端点 + job 轮询 + 送审联动 | **2 人日** | 模式成熟（复用既有 worker service），风险低 |
| W3 CI/执行/审批 | `/ci` 四区、死信重投、执行五缺口、审批双 Tab、**取消 R3 审批化行为变更**、A23/A24 回归 | **3 人日** | 行为变更需改既有测试口径并保 S3/S4 探针零退化；端点最密的一切片 |
| W4 看板/治理/知识 | `/metrics` 八区 + 聚合 API + `/quality` 三 Tab（接齐 12 个闲置端点）+ 知识五端点 + Recharts 接入 | **2.5–3 人日** | 页面最多但全只读；指标聚合口径需与 A22/A24 封存值手工对账 |
| W5 审计/总验 | `/audit` 四区、docs/08 编号升格、演示剧本浏览器化、A26 全功能探针、verify_s0 总验 | **1.5–2 人日** | 收尾型，风险在跨切片联调与红证复原检查 |
| **合计** | | **11.5–13 人日** | 约 **2.5 周净开发**；加 15–20% 评审/环境/返工缓冲，对外承诺按 **3 周自然时间** |

### 14.2 关键路径与缓冲

- 关键路径：W1 的 v16 迁移与 bootstrap full 模式——它决定后续所有页面演示时有没有真实数据，
  若模拟器环境受阻，立即降级先交付 lite 模式（录制段徽章诚实标注），不阻塞 W2；
- W3 的取消审批化是唯一行为变更，独立 commit、独立回滚预案，排在该切片最后一步；
- 每项红证对照与探针耗时已计入各切片，不另立缓冲；
- 若中途插入 docs/10 挂账任务或真机相关工作，按 D6 顺延 W 序列、不并行（单人上下文切换成本高于串行）。

### 14.3 交付物清单（W5 末状态）

9 条前端路由、17 个 API 缺口闭合、schema v16、bootstrap CLI、A25/A26 两探针、
pytest 全绿 + verify_s0 全 PASS + tsc/build 全静态、docs/08 正式编号升格、
README 浏览器版演示 runbook、docs/00 §6 六步演示全程不切终端。
