# ai-testpilot

智能研发测试助手 **TestPilot**——面向真实工程的测试质量智能体平台（工程 POC）。
系统围绕一个带 CI 与 UI 测试的 iOS 工程构建，提供三类能力：

1. **知识问答**：工程文档与代码符号切块入库，检索带引用溯源，并做版本一致性校验；
2. **测试设计 Agent**：需求 → 测试点 → 用例 → oracle 草稿的完整生成链，人工审批闸门兜底；
3. **测试执行与失败归因 Agent**：用例导出 → Xcode 测试生成与静态红线 → 真实 xcodebuild
   执行 → xcresult/os_log 解析与结构化归因 → 按 commit 影响面选择重跑。

## 能力概览

- **知识入库与混合检索**：文档/Swift 符号切块、向量+关键词混合检索、引用溯源、死信隔离与幂等批次；
- **测试设计链**：需求骨架抽取（FR/NFR/AC 白名单 fail-closed）、确定性脊柱+模型补点、
  oracle 四源挖掘，草稿恒为 pending 不碰正式用例库，人审结论才派发写回；
- **Xcode 执行闭环**：case-export 结构化导出、外部 Agent 生成测试、GS-001/002/003 静态红线、automation-map 映射、真实 xcodebuild 执行与制品 URN 回链；
- **影响面选择重跑**：commit diff 三分类（runnable/no_automation/conflict_update）、幂等入键、按 head 分桶的重跑配额，未映射用例响亮上报不静默退化；
- **混合执行与人工闸门**：hybrid-plan-v1 五 actor 编排、六个前置探针 fail-closed、USB 反向监听 ws-gate 真机会话，人工真机动作与感官判定经闸门承载；
- **执行与归因**：队列至少一次投递、租约回收与死信、Surefire 三态、结构化失败归因、坏例回流；
- **控制台与 API**：Next.js 静态导出 6 路由 + FastAPI 7 域 47 端点（同端口托管，另含 /health）；
- **可复现质量基线**：141 个测试文件、1704 个 hostless 测试用例；`offline-demo` profile
  以 recorded 适配器覆盖全链，克隆即可零外部依赖运行。

## 快速开始（零外部依赖路径）

前置：Python ≥ 3.12、[uv](https://docs.astral.sh/uv/)。

```bash
git clone https://github.com/asyncoding/ai-testpilot.git
cd ai-testpilot/subsystems
uv sync
# 注意：uv sync 可能对齐移除 tree-sitter optional extra，补装一次：
VIRTUAL_ENV=$PWD/.venv uv pip install -e 'packages/adapters[tree-sitter]'

# ① 全量单测（约 20 秒，无需模拟器/容器/外部服务）
uv run pytest tests/ -q

# ② S0 门禁 24 项 hermetic 总验（数分钟；8765 端口需空闲）
bash scripts/verify_s0.sh

# ③ 容器装配自检（offline-demo profile，无参跑通即装配就绪）
TESTPILOT_PROFILE=$PWD/config/profiles/offline-demo.yaml uv run testpilot-runner
```

起 Web 控制台与 HTTP API（另需 Node.js ≥ 18）：

```bash
bash scripts/build_web.sh           # 受限网络可加 NPM_REGISTRY=https://registry.npmmirror.com
uv run uvicorn testpilot_orchestrator.app:app --port 8765
# 控制台 http://127.0.0.1:8765/ ；健康检查 http://127.0.0.1:8765/health
```

从零到两个完整演示场景的分步操作、四个 profile 选择、20 个 CLI 与故障排查表，见
[docs/13 系统使用说明书](13-系统使用说明书（克隆到使用全流程）.md)。

## 语料仓边界（两条接入路径）

- **路径一：自包含离线演示（开源用户默认）**。`offline-demo` profile 使用 recorded 适配器与
  `tests/fixtures/` 内录制数据，不依赖任何外部工程或服务，克隆即可跑通全链。
- **路径二：完整 iOS 真机/模拟器链路**。自备一个 iOS 工程（本项目方案以 AlarmKitSpike 类
  工程为蓝本），经环境变量 `TESTPILOT_REPO_PATH` 只读接入；本仓**不包含**该工程源码，
  系统对语料仓始终只读（不切分支、不写 git、不做抹机）。

## 文档索引

| 文档                                                  | 内容                                                                                 |
| --------------------------------------------------- | ---------------------------------------------------------------------------------- |
| [01 总体设计](docs/01-总体设计.md)                          | **架构权威入口**：设计原则、分层与端口适配器、领域模型、知识工程、导出/映射/刺激四契约、混合执行与人工闸门、部署 profile 与切片门禁          |
| [03 语料工程与实测环境](docs/03-语料工程与实测环境（AlarmKitSpike）.md) | 被测工程事实、语料资产、xcodebuild/日志/xcresult 命令与真机 runbook                                   |
| [04 系统边界与外部对接清单](docs/04-系统边界与外部对接清单.md)            | 端口、服务、外部系统对接面与离线降级策略                                                               |
| [05 角色协作与落地推进手册](docs/05-角色协作与落地推进手册.md)            | RACI 分工、推进节奏与阻力应对                                                                  |
| [06 核心评测指标体系](docs/06-核心评测指标体系.md)                  | 检索/生成/执行/归因各层指标定义与阈值                                                               |
| [08 需求说明书](docs/08-需求说明书.md)                        | SRS：功能/非功能需求与验收标准                                                                  |
| [09 术语表（人话版）](docs/09-术语表（人话版）.md)                  | 全系统术语对照                                                                            |
| [13 系统使用说明书](13-系统使用说明书（克隆到使用全流程）.md)               | 克隆到使用全流程、CLI/API 参考、排障表；附录 A 外部 Agent prompt、附录 B 演示分支 git runbook、附录 C 偏差回流 AR 流程 |
| [17 核心文档规范化方案](docs/17-核心文档规范化方案.md)                | 用例/需求文档模板、lint 探针与分批迁移计划                                                           |
| [archive 历史证据归档](docs/archive/README.md)            | 事故踩坑（INC）/探针校准（CAL）/方案演进（ADR）/真机台账（EVD）/评测快照（BASE）五类追加式台账                          |

[CLAUDE.md](CLAUDE.md) 是本仓的工程纪律与跨 Agent 接力规则；`AGENTS.md` 为指向它的软链，
其他编码 Agent 读取同一份。

## 能力边界（不做什么）

- 不做通用问答/聊天机器人，领域限定在研发测试；
- 不自动化平台授权与设备抹机：真机 reset 策略显式拒绝，人工动作只经混合执行闸门承载；
- 不内置任何真实密钥或用户数据；compose 内均为开发默认值，配置文件头部已标注禁止用于生产；
- 控制台角色守卫、审计流等增强项处于搁置状态（见 [ADR-20261119-04](docs/archive/03-方案演进与废弃决策.md#adr-20261119-04)；其中 audit_events
  提案与当前 SQLite schema v15 零 DDL 红线冲突，落地需单独立项）；
- 物理真机端到端用例需要自行配对的设备，CI 与离线演示不覆盖该层。

## 数据边界与隐私

- 全链路默认本机运行：SQLite 账本、向量库、本地模型、日志/截图/xcresult 均落本机
  gitignored 目录（`store/`、`out/`），不收集、不上传与功能无关的用户数据；
- 可选观测栈 Langfuse 以本机容器自托管，其自身遥测由 `TELEMETRY_ENABLED=false` 关闭；
  不设置 `TESTPILOT_TRACER=langfuse` 时不导出任何 trace；
- `.env.*` 已 gitignore（仅 `.env.example` 白名单允许入库）。

## 目录结构

```
ai-testpilot/
├── LICENSE                 # MIT
├── CLAUDE.md / AGENTS.md   # 工程纪律（AGENTS.md 为软链）
├── docs/                   # 设计与操作文档（见上方索引）
└── subsystems/
    ├── packages/           # core / adapters / orchestrator / workers 四个 Python 包
    ├── web/                # Next.js 15 静态导出控制台
    ├── scripts/            # verify_s0 门禁与 20+ hermetic 探针
    ├── config/profiles/    # offline-demo / ios-local-poc / -xcode / -device 四个 profile
    └── tests/              # 141 个测试文件与只读 fixtures
```

## License

[MIT](LICENSE)，版权行见 LICENSE 文件。
