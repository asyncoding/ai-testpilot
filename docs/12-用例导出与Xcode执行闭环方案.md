# 12 · 用例导出与 Xcode 执行闭环方案（细化设计）

> 状态：**v1.4 五切片全部落地 + 契约二 UI 测试双 target 化**（2026-11-13：E1–E5 已落地并完成真实 xcodebuild 端到端验证；v1.4 修订契约二/附录 A/B，XCUITest 生成从「人工另行开口」升级为契约内一等分支，并修正外部 Agent 阅读清单为语料仓 CLAUDE.md + V1 三文档；全量 1248 测试、verify_s0 24/24）｜落地任务见 `.agent-handoff/2026-11-13-case-export-xcode-loop.md` 与 `.agent-handoff/2026-11-13-ui-test-contract-and-prompt-fix.md`
> 关联：[08 需求说明书](08-需求说明书.md) FR-TD/FR-EX/FR-FRS · [03 实测环境](03-语料工程与实测环境（AlarmKitSpike）.md) §3.6 真机 runbook ·
> [11 Web 补全方案](11-Web控制台功能设计-补全方案.md)（已搁置，本方案是其 W3.5 断点的独立收口）
> 前置结论：系统内**不做 Swift 代码生成器**（2026-11-13 讨论定案），AI 产规格、外部 Agent 辅产代码、人审合入、系统负责映射与真实执行。

---

## 1. 目标与两个必演场景

**总目标**：在不引入代码生成器、不破坏语料仓只读纪律、schema 保持 v15 零变更的前提下，打通两条可现场演示的真实链路。

- **场景 A：需求 → 全量用例 → 外部生成代码 → 回流真实执行**
  1. 对 AlarmKitSpike 真实需求文档跑既有 S2 管线（需求骨架→测试点→结构化用例→oracle），人审批准；
  2. 导出结构化 JSON 用例集（新能力 E1）；
  3. 人工携出系统边界，在 app-calendar-alarm 的**长期演示分支**上由 Claude 等外部 Agent 按约定生成 Swift 测试，人审编译通过后合入该分支；
  4. 本系统扫描演示分支，按 TC 标记建立"用例 ↔ Swift 测试"映射（E3）；
  5. 经映射把用例选择集翻译成 `-only-testing`，真实 `xcodebuild test` 执行，xcresult 回收、失败归因（复用 S3 既有能力）。

- **场景 B：commit diff → 影响面 → 存量用例选择重跑 → 归因**
  1. 对演示分支两个真实 commit 跑既有影响面分析（三分类 runnable/no_automation/conflict_update）；
  2. 新 CLI（E4）把 runnable 集经映射翻译成 Swift 标识符并提交执行队列，未映射用例显式报告；
  3. 真实 xcodebuild 选择重跑 → xcresult → 归因五候选落账，全程 runId 贯通。

## 2. 系统边界（哪些在系统内、哪些在系统外）

```
┌─────────────────────── ai-testpilot（本仓，系统内，全自动+人审点）──────────────────────┐
│ 需求骨架 → 测试点 → 用例/oracle（S2 既有）→ HITL 审批（既有）                              │
│   → E1 导出 JSON 用例集（版本化契约）                                                     │
│                                                                                          │
│ E3 扫描演示分支 Swift 测试 + TC 标记 → automation-map.json（人审映射）                    │
│ E4 影响面 runnable → 映射 → TestExecutionRequest → 队列（配额/幂等/策略全复用既有）        │
│ E5 xcode runner（S3 既有适配器）→ xcodebuild → xcresult 解析 → 归因（S3 既有）            │
└───────────────────────────────▲────────────────────────────────────────────────────────┘
                                │ JSON 携出（人工） / 只读 git 读取演示分支（人工合入）
┌───────────────────────────────┴────────────────────────────────────────────────────────┐
│ app-calendar-alarm（另一个仓，系统外）                                                    │
│ 演示分支 demo/agent-generated-tests（长期存在，永不合主线）：                              │
│ 外部 Agent（Claude 等）按约定生成 Swift 测试 → 人审 + 编译验证 → 人工合入                  │
└──────────────────────────────────────────────────────────────────────────────────────────┘
```

边界纪律：

1. testpilot **永不写** app-calendar-alarm 仓（git 连接器只读，沿用现状）；分支创建与代码合入是人工动作，runbook 给全步骤；
2. 外部 Agent 的生成过程不属于本系统能力，演示话术与文档均如实标注，生成对话记录以附件留痕；
3. 系统只信任两类事实：人审批准的用例规格、演示分支上**真实存在且编译通过**的 Swift 测试；
4. 映射缺失时响亮失败/显式报告，绝不静默全量执行或假绿。

## 3. 数据契约（三个版本化契约，评审重点）

### 3.1 契约一：结构化用例集导出（`case-export-v1`，E1 产出）

落盘到 profile 写根 `exports/case-set-<utc时间戳>-<短hash>.json`，UTF-8、缩进 2、末尾换行。

```json
{
  "export_schema_version": "case-export-v1",
  "exported_at": "2026-11-13T08:00:00Z",
  "content_hash": "<sha256，归一化排除 exported_at/content_hash 自身>",
  "profile": "ios-local-poc",
  "project": "AlarmKitSpike",
  "source_branch": "alarmkit-spike",
  "version_quadruple": {
    "data_version": "<语料摄入批次 hash>",
    "model_version": "ollama:<model>",
    "prompt_version": "qa-prompt-3 / testpoint-prompt-x",
    "params_version": "qa-params-1"
  },
  "generation_convention": {
    "marker_comment": "// testpilot-case: <TC-ID>",
    "naming": "func test_<TC-ID 去横线转驼峰>_<短描述>()",
    "target_target": "AlarmKitSpikeTests",
    "ui_test_target": "AlarmKitSpikeUITests",
    "detail_doc": "docs/12 §3.2"
  },
  "requirements": [
    {"urn": "...", "external_id": "REQ-1", "features": [], "rules": [],
     "acceptance_criteria": [{"id": "AC-1", "text": "..."}]}
  ],
  "test_points": [
    {"text": "...", "priority": "P0", "fr_id": "FR-9", "kind": "functional",
     "provenance": "deterministic|model", "source_refs": []}
  ],
  "cases": [
    {
      "external_id": "TC-C1",
      "title": "倒计时各档保存",
      "module": "RoutineAlarm",
      "priority": "P0",
      "lifecycle": "active",
      "exploratory": false,
      "oracle_missing": false,
      "preconditions": ["..."],
      "steps": ["..."],
      "oracles": [
        {"type": "ui_assertion|log_assertion|state_assertion|manual",
         "expectation": "...", "source_refs": [], "confidence": 0.8}
      ],
      "mined_oracle_suggestions": [
        {"type": "log_assertion", "expectation": "...", "source_refs": [],
         "confidence": 0.7, "source_kind": "incident", "provenance": "deterministic",
         "status": "approved|pending|rejected"}
      ],
      "requirement_urn": "...",
      "tags": []
    }
  ],
  "excluded": {
    "cancelled_case_ids": [],
    "exploratory_case_ids": [],
    "reason": "lifecycle=cancelled 不导出；exploratory 仅在显式排除时列入"
  }
}
```

契约规则：

- 只导出 `lifecycle=active` 的用例；cancelled 列入 `excluded.cancelled_case_ids` 留痕；`exploratory=true` 照常导出但打标（外部 Agent 不应为其生成自动化代码，映射扫描时也不纳入 runnable 自动化集），`--no-include-exploratory` 显式排除时移入 `excluded.exploratory_case_ids`；
- 用例内嵌正式 `oracles` 与 `mined_oracle_suggestions`（S2.4 挖掘草稿，带 status/source_kind）分列，pending 草稿不被夸大成确认口径；
- 字段全部来自既有 core 模型（`TestCase/TestPoint/Oracle/MinedOracle/RequirementParseResult`），导出层只做序列化不造字段；
- 文件内容寻址 hash 同时写入载荷顶层 `content_hash` 与文件名（`<hash8>`），不新建表、不动 schema v15；导出账本暂不引入，需要对账时以文件 hash 为准（E1 落地决策，避免过度设计）；
- 同一库重复导出生成不同时间戳文件，但同内容 hash 可对账（已实测：两次导出 hash 完全一致）。

### 3.2 契约二：外部生成约定与 TC 标记规范（E2，文档+人工流程；v1.4 起双 target）

对外部 Agent 的硬性约定（写进 runbook 的 prompt 模板）：单元测试与 UI 测试是契约内的
两个对等分支，共用标记/命名/溯源规范，仅落点与代码形态不同。

1. **标记注释**：每个生成的测试方法上方一行 `// testpilot-case: <TC-ID>`；一个方法可覆盖多个 TC 时写多行标记；单元/UI 两类测试的标记语法完全一致，扫描器按目录自动分流 target；
2. **命名**：`func test_<TC-ID 去横线转驼峰>_<英文短描述>()`，例：`TC-C1 → test_TCC1_countdownSnapshots()`；
3. **target 分流规则（依据导出 JSON 既有字段，不引入新字段）**：
   - 该 TC 任一 `oracles[].type == "ui_assertion"` → **UI 分支**：写入 `AlarmKitSpike/UITests/AgentGenerated/`（新目录，按模块命名文件，类名 `Agent<Module>UITests`），归属 `AlarmKitSpikeUITests` target（XCUITest，驱动真实产品 UI）；
   - 其余 → **单元分支**：写入 `AlarmKitSpike/Tests/AgentGenerated/`（新文件，类名 `Agent<Module>Tests`），归属 `AlarmKitSpikeTests` target（hostless 纯 Foundation 测试）；
   - 人工可在交付指令中显式覆盖分流（如把 state_assertion 用例改成 UI 走查），但必须在 `generation-report.md` 逐条记录覆盖原因；系统侧不强制分流，扫描映射只认最终落点目录；
   - `exploratory=true`、或 `oracle_missing=true` 且无任何 oracle 的用例两个分支都不生成，进报告 skipped 清单；
4. **UI 分支额外硬约定**（对齐存量 `UITests/ProductFlowUITests.swift`，外部 Agent 必须先读它再写）：
   - 形态为 XCUITest：`XCUIApplication` 驱动真实 UI，不直接 init 领域模型；`setUpWithError` 中 `continueAfterFailure = false`，每用例冷启动零累积，启动参数固定带 `--ak-clear-all --ak-purge-dates --ak-accept-privacy`（隐私门专项用 `--ak-reset-privacy` 强制重弹，参数语义以语料仓 `CLAUDE.md` 避坑指南为准）；
   - 等待一律 `waitForExistence(timeout:)` 轮询，禁止死等；断言旧页面消失而非仅断言新标题存在（防同名假绿）；关键步截图作为 xcresult attachment；
   - V1 测试用例文档中标 📱（硬件键/全屏闹钟画面/音频/震动/锁屏视觉）的用例模拟器不可验，**不得生成模拟器 UI 测试**，进 skipped 并注明「仅真机可验」；🤖 标记才可生成；
5. **编译门禁**：两类产物都必须在 `AlarmKitSpike/` 子目录先 `xcodegen generate`（两个 target 的 sources 都是 folder group，新文件必须重新生成工程才进编译）再 `xcodebuild build-for-testing` 通过，人审确认断言与 oracle 期望一致才可合入；
6. **禁止事项**：不改 `Sources/` 生产代码、不修改/删除 `Tests/` 与 `UITests/` 存量测试、不引入第三方依赖与新 Pod；
7. 每个方法的断言必须能回溯到导出 JSON 中该 TC 的 `oracles[].expectation`（`mined_oracle_suggestions` 仅 `status=approved` 可作补充断言来源），生成记录（对话/补丁）作为附件存 testpilot 写根 `external-generation/<export hash8>/`。

### 3.3 契约三：自动化映射文件（`automation-map-v1`，E3 产出）

落盘 `store/<profile>/automation-map.json`（`--out` 可改路径），可人审编辑后重跑扫描合入，或用 `--bind/--review` CLI 登记。

```json
{
  "map_schema_version": "automation-map-v1",
  "generated_at": "...",
  "repo_path": ".../app-calendar-alarm",
  "branch": "demo/agent-generated-tests",
  "head_commit": "<sha>",
  "map_hash": "<sha256，剔除 generated_at/map_hash 自身后规范化哈希；head_commit 参与哈希——HEAD 移动 hash 必变>",
  "bindings": [
    {
      "case_external_id": "TC-C1",
      "only_testing_id": "AlarmKitSpikeTests/RoutineAlarmTests/test_TCC1_countdownSnapshots",
      "source_file": "AlarmKitSpike/Tests/AgentGenerated/RoutineAlarmAgentTests.swift",
      "line": 42,
      "origin": "marker",
      "reviewed_by": "",
      "reviewed_at": null
    },
    {
      "case_external_id": "TC-U1",
      "only_testing_id": "AlarmKitSpikeUITests/AgentQuickAlarmUITests/test_TCU1_quickCountdownFlow",
      "source_file": "AlarmKitSpike/UITests/AgentGenerated/QuickAlarmAgentUITests.swift",
      "line": 31,
      "origin": "marker",
      "reviewed_by": "",
      "reviewed_at": null
    }
  ],
  "unlinked_swift_tests": [
    {"only_testing_id": "AlarmKitSpikeTests/HolidayCalendarTests/testPlainDays",
     "source_file": "...", "line": 60}
  ],
  "unmapped_active_cases": ["TC-N2"],
  "scan_warnings": ["重复标记: TC-C1 同时出现于 FileA.swift:10 与 FileB.swift:20"]
}
```

规则：

- `origin=marker`：扫描器按 TC 标记自动绑定；`origin=manual`：人审对无标记存量测试的手工绑定（CLI `--bind <TC>=<Target/Class/method> --by <人>`，或编辑本文件后重扫合入；Swift 方法必须真实存在，悬空即 fail-closed）；
- 一个 TC 可绑定多个方法（执行时标识符全量带入、xcresult 按方法分别回收，产生 exit 3 警告催人审）；一个方法标记多个 TC 时执行记录双向回链；
- 映射同时投影到 `test_cases.raw_json.automation`（只追加、reindex 存活口径同知识治理），**schema 保持 v15 不动**；
- 三档退出码：**exit 2 硬错误**（TC 不存在/非活跃、畸形标记、非法 OID、悬空 manual、旧 map 损坏）→ 不写 map、不覆盖旧 map；**exit 3 警告**（一 TC 多方法、探索性标记被剔除）→ map 照写催人审；**exit 0** 干净。重扫幂等：同 HEAD 同内容 map_hash 稳定，人审记录跨重扫保留。

## 4. 切片设计（E1–E5）

### E1 结构化用例集导出（约 1 人日）——✅ 已落地（2026-11-13）

| 项 | 内容 |
|---|---|
| 新增 | `core/case_export.py`（零 IO 纯函数：模型列表 → `case-export-v1` 载荷 + 内容 hash）；`workers/export_cases.py`（CLI `testpilot-export-cases`，workers 包第 13 个入口） |
| 复用 | store 既有 `list_test_cases/list_test_points/list_oracle_suggestions/list_requirement_skeletons` 读取、版本四元组采集口径（对齐 eval_runner，不可得留空串） |
| CLI | `testpilot-export-cases [--out <文件或目录>] [--no-include-exploratory] [--data/--model/--prompt/--params-version ...] [--test-target ...] [--source-branch ...]`，默认写 `<data_dir>/exports/`，stdout 打印路径/hash/分区计数 |
| AC | ①导出 JSON 通过 schema 自校验（`test_case_export_core.py` 11 例）；②cancelled 不进 cases 只进 excluded；③同库两次导出同内容 hash 一致（实测 `7e546181…` 两次相同）；④空库导出响亮失败 exit 2，不产空壳文件（ios-local-poc 空库实测 exit=2） |
| 红证 | 已执行：临时把 active 过滤改为全量 → `test_cancelled_case_never_enters_cases_but_is_listed_in_excluded` 等 2 测试变红；还原变绿（19/19 通过，全量 1207 通过） |

### E2 外部 Agent 协作约定与演示分支 runbook（约 0.5 人日，纯文档）——✅ 已落地（2026-11-13，附录 A/B；v1.4 双 target 修订）

- 产出：docs/12 附录 A（prompt 模板全文，v1.4 起含单元/UI 双生成分支与语料仓 CLAUDE.md/V1 三文档阅读清单）、docs/13 说明书对应章；演示分支创建/合入/只读对接的完整 git 命令；
- 演示分支约定名 `demo/agent-generated-tests`，从 `alarmkit-spike` 切出，README 顶部加"演示分支不合主线"声明（人工操作步骤写入说明书）；
- AC：第三方按 runbook 从零操作可独立完成分支创建与一次示例生成；命令全部可复制执行，无机密/绝对路径。

### E3 自动化身份映射器（约 1.5–2 人日，本方案核心）——✅ 已落地（2026-11-13）

| 项 | 内容 |
|---|---|
| 新增 | `core/automation_mapping.py`（零 IO 纯函数：Swift 文件内容字典 → 测试方法清单 + 标记解析 + 绑定/未链接/未映射/警告）；`workers/scan_automation.py`（CLI `testpilot-scan-automation`，第 14 个入口）负责文件遍历与落盘；`sqlite_store.project_case_automation()` 投影方法（schema v15 零 DDL） |
| 扫描口径 | 正则识别 `class …Tests` 与 `func test\w+`（Swift 方法签名封闭形态，不引 Swift 解析器）；标记只认行首空白后 `// testpilot-case: TC-…`，挂在下方最近方法、遇 class 清空悬挂标记；target 按目录映射（Tests→AlarmKitSpikeTests，UITests→AlarmKitSpikeUITests）；非 ASCII 方法名（如 `test调休`）不被 `\w+` 识别，不计入台账 |
| 人审 | `--review <TC-ID> --by <人>` 登记 marker 绑定 reviewer；`--bind <TC-ID>=<Target/Class/method>` 手工绑定无标记存量测试（必须配 `--by`）；重扫自动保留旧 map 的人审记录与 manual 绑定并重新校验，悬空 manual fail-closed 且不覆盖旧 map |
| 三档退出码 | **0** 干净；**2** 硬错误（未知/非活跃 TC、畸形标记、非法 OID、悬空 manual、旧 map 损坏——不写 map）；**3** map 已产出但有警告（一 TC 多方法、探索性绑定被剔除），催人审 |
| 投影 | 每个活跃用例写 `test_cases.raw_json.automation = {mapped, bindings, head_commit, map_hash, projected_at}`，未映射用例显式负态，零 schema 变更 |
| AC | ①带标记样例文件绑定正确率 100%（含一方法多 TC、一 TC 多方法）；②不存在的 TC 标记、畸形标记 fail-closed exit 2；③存量无标记测试全部进 unlinked_swift_tests；④manual 绑定指向已删除方法时校验失败且旧 map 字节不变；⑤重跑扫描幂等，同 HEAD 下 map_hash 稳定，HEAD 移动 hash 必变 |
| 测试 | hostless 纯函数 12 条 + worker hermetic 5 条 = 18 条全绿；全量 **1225 passed**（1207 基线 + 18），import-lint OK |
| 红证 | 已执行：临时掐断 marker 入对分支（`if False and marker_match`）→ 7 条测试变红（marker 绑定/未知 TC/探索性剔除/投影/CLI 退出码全链路）；还原后 18/18 复绿 |
| 真仓冒烟 | `ios-local-poc` profile 对真实语料仓 `app-calendar-alarm`（alarmkit-spike @ 7936295a）只读扫描：10 个 Swift 测试文件、110 个测试方法全部正确入册，双 target（AlarmKitSpikeTests / AlarmKitSpikeUITests）分流正确，空用例库下 bindings=0、unmapped=[]、exit 0；语料仓 git 状态前后不变（纯只读）。**E5 注记（2026-11-13）**：该次冒烟的 110/10 含仓里根 `Tests/` 作废 SPM 包的 12 方法/4 文件，E5 真实 xcodebuild 联调前抓出并修复（工程根 containment），修复后口径为 98 方法/6 文件，详见 E5 章 |

### E4 影响面 → 选择执行接线（约 1–1.5 人日）——✅ 已落地（2026-11-13）

| 项 | 内容 |
|---|---|
| 新增 | `core/impact_execution.py`（零 IO 纯函数：ImpactAnalysis + map bindings → only_testing 选择集/三桶分流/幂等键/鲜度守卫）；`workers/impact_rerun.py`（CLI `testpilot-impact-rerun`，第 15 个入口） |
| 流程 | `analyze_impact(base, head)` → runnable external_id 集 → 读 automation-map 并做版本/鲜度守卫 → 译 only_testing ID（再过 `validate_only_testing` 形态校验）→ 构造 `TestExecutionRequest` 调既有 `execution_service.submit_execution`（队列、配额、状态机零新造） |
| CLI | `testpilot-impact-rerun --base <sha> --head <sha> [--requirement <urn>...] [--dry-run] [--project ...] [--map <路径>]`；dry-run 只打印选择集与拟提交请求，不写账本不入队 |
| 守卫 | ①选择集为空（runnable 全未映射）→ exit 2 并打印未映射 TC 清单与补映射提示，绝不退化为全量；②幂等键 = `impact:<head短sha12>:<排序后ID集 sha256 前16位>`，重复提交返回 replayed 不增生；③exploratory（no_automation）/conflict_update 不进自动选择，仅报告；④**map 鲜度**：`map.head_commit ≠ --head` 直接拒，禁止拿旧映射跑新代码；⑤git diff 不可达等 `AdapterUnavailableError` 收口 exit 2 不出 traceback |
| 配额 | 请求带 `daily_budget_key=impact:<head短sha>`，按 head 分桶占每日重跑配额（默认 20）；重放命中幂等账本不双扣 |
| AC | ①合成夹具 runnable 2 条其中 1 条有映射：提交请求仅含映射 OID，未映射 1 条与 no_automation/conflict 两桶显式上报；②同参数二次提交 replayed=true 且 jobs/账本零增生、配额只扣一次；③dry-run 账本/队列零写入；④无映射库/过期 map/缺 map 均 exit 2；⑤一 TC 多方法 OID 全量带入，多 TC 共享方法去重 |
| 测试 | hostless 纯函数 10 条 + worker hermetic 9 条 = 19 条全绿；全量 **1244 passed**（1225 基线 + 19），import-lint OK |
| 红证 | 已执行：放开空选择守卫（`if True or ...` 提前 return）→ 2 条守卫测试精准变红（core + worker 各一）；还原后 19/19 复绿，与既有 `validate_only_testing` 空选择拒绝互证 |
| 真 profile 冒烟 | `ios-local-poc` 对真实语料仓（alarmkit-spike 真实 sha 区间）：真 git diff 链路打通；缺 map → exit 2 带补映射提示；构造过期 map → "已过期" exit 2；鲜 map 零绑定 → "选择集为空" exit 2（冒烟 map 写 gitignored 的 store/ 工作区，验完即删） |

### E5 xcode profile 与端到端验证（约 1 人日）——✅ 已落地（2026-11-13）

| 项 | 内容 |
|---|---|
| 新增 profile | `config/profiles/ios-local-poc-xcode.yaml`：与 ios-local-poc 六处差异组——①runner recorded→**xcode**（真实 xcodebuild，SubprocessExecutor 永不 shell）；②environment stub→**ios-simulator**（simctl 设备池，先租后跑，udid 透传 labels）；③logs stub→**os-log**（显式 udid，拒绝 booted 隐式目标）；④data_dir 独立 `store/ios-local-poc-xcode`，执行账本与录制 profile 物理隔离；⑤xcresult 工作/产物根独立；⑥2026-11-13 补 S2 三适配器：cases/requirements 由 stub 换 **fs-cases/fs-reqs 单文件 root**（`${REPO_PATH}/docs/V1测试用例文档.md`、`V1需求文档-强提醒闹钟.md`，真档实测零死信：52 用例/33 活跃 FR），case_repo 装配 **markdown-fs**（写根 data_dir/case-writeback，不碰语料仓）。limits 放宽 request_timeout 900s / lease 1200s（冷启动分钟级），daily_rerun_quota=20；其余适配器一字不差，原 `ios-local-poc.yaml` 零改动（私改不夹带，回退零成本） |
| 实测抓出的 E3 精度缺陷 | 真仓首扫把仓里根 `Tests/CalendarStrongReminderTests/`（2026-08 已作废的 CalendarStrongReminder SPM 包，4 文件 12 方法，不被 AlarmKitSpike.xcworkspace 编译）误纳进 AlarmKitSpikeTests（110 = 98 真实 + 12 作废），其 OID 跑 xcodebuild 必然 suite-not-found。修复：`core.automation_mapping.target_for_path` 两道 containment——路径必须含工程根段 `AlarmKitSpike`，工程根内再按 Tests/UITests 分 target；worker 文件遍历改以 core 为唯一判定源。红证：先加 `test_target_for_path_rejects_stale_root_package_outside_workspace` 看红，再修变绿；修复后真扫 **98 方法（86 单元 + 12 UI）、scanned_files 10→6** |
| 场景 A 真实全链 | 提交 `run-f9e66995606b` → runner run-once 真实 xcodebuild 冷启动 ~2m52s → **passed**；环境 provider=ios-simulator、env_id=5411787C-8AFF-4EBF-9000-EB5EFD1457D5；CaseResult `AlarmKitSpike/AlarmKitSpikeTests/RoutineAlarmTests/testWeekend()` passed；制品 `urn:testpilot:localfs-artifact:xcresult:fc6596af4f37-test-results.json@fc6596af4f37`（keys devices/testNodes/testPlanConfigurations，设备 iPhone 17 / iOS 27.0）；`.xcresult` bundle 含真实 `Data/database.sqlite3`+`Info.plist`（非 fail-closed 占位）；capability=xcresulttool-schema-0.4.0；uncovered_layers=[background_audio, codesigning, ios.device] 如实留档 |
| 场景 B 真实全链（commit diff→影响→选择重跑） | 真实区间 `09b473b→7936295`（语料仓 alarmkit-spike）：①最小播种（脚本 `subsystems/out/e5_scenario_b_seed.py`，gitignored）按 hermetic 测试同口径播 1 文档/1 符号 chunk（锚点 `RoutineAlarmScheduler.runReconcile()` 取自该区间真实 git diff）/1 用例 TC-IMPACT-1/1 条 case_references_symbol 关系，打通「真实 diff→库内 URN→用例」唯一未覆盖接缝；②真实 scan-automation 落 map（map_hash `d7a0c2e5…`，head=7936295a，1 条 manual 绑定 → `AlarmKitSpikeTests/RoutineAlarmTests/testWeekend`，97 未链接，6 文件，exit 0）；③dry-run：runnable=1、选择集恰为该 OID、幂等键 `impact:7936295a700c:da647889db8e51ba`、账本零写入；④真实提交 `run-aadf932176c1`（queued, replayed=false），同参重放返回**同 run_id 且 replayed=true、jobs 零增生**，rerun_budget 桶 `impact:7936295a700c` 仅 1 行；⑤run-once 真实 xcodebuild **passed**，制品 `…:82fab885eeb5-test-results.json@82fab885eeb5`（testNodes 1 个 Passed，同台 iPhone 17/iOS 27.0），真实 bundle `run-aadf932176c1.xcresult` |
| 真机库负链守卫 | map 停在旧 head（--head 传父 sha）→ exit 2「automation-map 已过期…禁止拿旧映射跑新代码」；`--map` 指向不存在路径 → exit 2 带「请先运行 testpilot-scan-automation」可操作提示；两守卫均在真实进程 + 真实 git 链路上验证，零入队 |
| AC④ runbook 指引 | `execution_service` 外层 except 对 `AdapterUnavailableError`（模拟器全忙/simctl/xcodebuild 失败族）统一追加「环境排障见 docs/03 §3.6 与 docs/13 故障排查章」，500 字截断预留指引长度防截没；run 账本 error 与 jobs 死信原因同文；普通业务异常不挂指引（负对照测试防信号稀释） |
| 红证 | 已执行：临时 `if False and isinstance(...)` 掐断指引 → `test_adapter_unavailable_error_carries_runbook_hint` 精准变红、负对照保持绿；还原后复绿。另含 E3 containment 红证一条（见上） |
| 测试与门禁 | 新增 4 条测试（containment 回归 1 + xcode profile 装配 1 + runbook 指引正/负 2），全量 **1248 passed**（1244→1246→1248），import-lint OK；verify_s0 **24/24 两轮全绿**（含 execution_service 改动后复跑），s3/s4/xcode-adapter/xcresult-oslog 探针全绿；schema 定格 **v15 零 DDL**（实测 34 表 = schema.sql 27 张业务表 + chunks_fts 6 张 FTS5 影子表 + sqlite_sequence） |
| 验证边界（如实声明） | ①长期演示分支 `demo/agent-generated-tests` 尚不存在，由人工按附录 B 创建；本次 E2E 在 alarmkit-spike 工作区**纯只读**进行，map 的 branch 字段如实记录 alarmkit-spike，鲜度只锚 head_commit；②场景 B 知识侧用单锚点最小播种证明接缝，完整「需求→测试点→用例→关系」S2 管线由 A10–A18 hermetic 探针与既有入库流程覆盖，docs/13 给完整人工操作顺序；③模拟器三未覆盖层（后台音频/签名/真机）本地永不可验，只能真机/生产验，docs/03 §3.6 手动 runbook 永不进 verify_s0 |

## 5. 验收总表与红证清单

| 验收 | 方式 |
|---|---|
| 单测 | 三个新 core 纯函数模块 hostless 测试 + 三个 CLI 的夹具测试，进 pytest 全量 |
| 契约 | case-export-v1 / automation-map-v1 各一份封存样例 + schema 自校验测试 |
| 红证（四条） | cancelled 混入、标记正则删除、空选择守卫放开、manual 绑定悬空——每条必须实测红→绿并留记录 |
| 回归 | import-lint、verify_s0 全 PASS（E5 两轮 24/24）、A19–A24 既有探针零退化、schema 定格 **v15 零 DDL**（PRAGMA 实测 34 表 = 27 业务表 + 6 张 chunks_fts FTS5 影子表 + sqlite_sequence） |
| 端到端 | 场景 A/B 各一次真实跑通并留证：A=`run-f9e66995606b`（制品 URN `fc6596af4f37…`），B=`run-aadf932176c1`（map_hash `d7a0c2e5…`、制品 URN `82fab885eeb5…`），均为真实 xcodebuild + 真实 .xcresult bundle；证据落 gitignored `store/ios-local-poc-xcode/`，复现命令与路径见 docs/13 |

## 6. 工期与切片顺序

| 切片 | 人日 | 依赖 |
|---|---|---|
| E1 导出 | 1 | 无，可立即开工 |
| E2 协作约定/分支 runbook | 0.5 | 无（可与 E1 并行，单人则串行） |
| E3 映射器 | 1.5–2 | E2 的标记规范定稿 |
| E4 影响面接线 | 1–1.5 | E3 映射产物 |
| E5 xcode profile + E2E | 1 | E1/E3/E4 + 演示分支就绪 |
| docs/13 使用说明书 | 1 | 全部切片完成后据实写命令 |
| **合计** | **6–7 净人日** | 顺序 E1→E2→E3→E4→E5→说明书 |

关键路径风险：E5 依赖模拟器环境（docs/03 §3.6 的手动步骤永不进 verify_s0）；若环境受阻，E1–E4 可全部先用 recorded profile  hermetic 验证，E5 延后不阻塞代码收口。

## 7. 明确不做

1. 不做系统内 Swift/任何语言代码生成、不做编译器封装（`xcodebuild build-for-testing` 只在 runbook 由人工执行）；
2. 不做 web 页面（docs/11 搁置）；本方案全部入口为 CLI + 既有 API，说明书以终端操作为主线；
3. 不在系统内生成任何 Swift 代码（单元与 XCUITest 一律由系统外 Agent 按 §3.2 双分支约定生成、人审合入；v1.4 起 XCUITest 已是契约内一等分支，但系统本身仍零代码生成）；不做自动分支创建/自动合入；
4. 不做 schema v16（映射走 raw_json + 独立 map 文件）；不改动 S2/S3/S4 既有语义与探针封存值；
5. 不把外部 Agent 生成的代码拷入本仓，不把演示分支合入语料仓主线。

---

## 附录 A：外部 Agent prompt 模板（E2 产出，可整段复制）

> 适用对象：Claude / 任意编码 Agent，在 **app-calendar-alarm 仓的演示分支工作区** 内执行。
> 输入：E1 导出的 `case-set-*.json`（`case-export-v1`）。输出：带 TC 标记的 XCTest
> 代码 + 生成报告，全部留在语料仓演示分支；testpilot 不内置此模板的任何自动化。

```text
你是 AlarmKitSpike（iOS Swift 6 / XCTest）项目的测试工程师。本次任务是把一份
TestPilot 导出的结构化用例集（case-export-v1 JSON，随消息附上或给出文件路径）
实现为可编译、可执行的 XCTest 测试：按用例的 oracle 类型分流为 hostless 单元测试
（AlarmKitSpikeTests）与 XCUITest UI 测试（AlarmKitSpikeUITests）两个分支。

【开工前必读】（以下路径均相对本仓根目录，按顺序读，不要凭文件名猜测）
1. CLAUDE.md——工程纪律与避坑指南（隐私总门、AlarmKit/通知授权、UIKit/SwiftUI
   混用、iOS-only API 条件编译等实测坑，生成代码必须遵守）；
2. docs/V1需求文档-强提醒闹钟.md——需求口径与验收标准；
3. docs/V1技术设计文档.md——架构与模块边界；
4. docs/V1测试用例文档.md——存量用例与环境标记（🤖=模拟器可跑，📱=必须真机）；
5. 可选补充：docs/agent_pitfalls.md（历史避坑日志，与 CLAUDE.md 冲突时以 CLAUDE.md 为准）；
6. 若本次包含 UI 分支，再读 AlarmKitSpike/UITests/ProductFlowUITests.swift——
   存量 XCUITest 的唯一参照写法（启动参数、轮询、截图、用例隔离都照它）；
7. 读 JSON 的 generation_convention 段：marker_comment / naming / target_target /
   ui_test_target 是本次生成的硬性约定，逐字遵守。

【用例资格与分流】
1. 只读 cases[] 中 lifecycle=active 且 exploratory=false 的用例；exploratory=true
   与 oracle_missing=true 且无任何 oracle 的用例不生成代码，写入报告 skipped 并逐条说明；
2. 分流规则（依据 JSON 既有字段，不要自造标记）：
   - 任一 oracles[].type == "ui_assertion" → UI 分支（AlarmKitSpikeUITests）；
   - 其余 → 单元分支（AlarmKitSpikeTests）；
   - 人工交付指令显式指定分流时以指令为准，但必须在 generation-report.md 标注覆盖原因；
3. V1 测试用例文档标 📱 的用例（硬件键/全屏闹钟画面/音频/震动/锁屏视觉）模拟器
   不可验，一律不生成 UI 测试，进 skipped 注明「仅真机可验」。

【通用代码规则】（两个分支都适用）
1. 每个测试方法上方独占一行写标记：// testpilot-case: <TC-ID>
   一个方法覆盖多个 TC 时，连续写多行标记，每行一个 TC-ID；
2. 方法命名：func test_<TC-ID 去横线转驼峰>_<英文蛇形短描述>()，
   例 TC-C1 → func test_TCC1_countdownSnapshots()；转换规则与 TestPilot
   tc_id_to_swift_token 同源（去 "-"，各段首字母大写）；
3. 断言必须能回溯到该 TC 的 oracles[].expectation；mined_oracle_suggestions 中
   status=approved 的条目可作为补充断言来源，pending/rejected 不得作为断言依据，
   每条断言在方法内注释标注来源（oracle 序号或 suggestion）；
4. 前置条件（preconditions）用私有 helper 或 setUp 表达；步骤（steps）翻译成
   方法内分段注释 // Step 1 ...，保持与 JSON 文本可对照；
5. 禁止事项：不改 Sources/ 下任何生产代码；不修改/删除 Tests 与 UITests 下存量
   测试；不引入第三方依赖与新 Pod；新类不得与存量类重名。

【单元分支规则】
1. 落点：AlarmKitSpike/Tests/AgentGenerated/（新文件，按模块命名，
   如 RoutineAlarmAgentTests.swift），类名 Agent<Module>Tests；
2. hostless 风格：只构造纯 Foundation 输入调被测类型，不做网络/UI 跳转，
   参考存量 RoutineAlarmTests（Calendar.shanghai、直接 init 领域模型）。

【UI 分支规则】
1. 落点：AlarmKitSpike/UITests/AgentGenerated/（新文件，按模块命名，
   如 QuickAlarmAgentUITests.swift），类名 Agent<Module>UITests，XCUITest 形态；
2. 照抄 ProductFlowUITests 的基建：XCUIApplication 驱动真实 UI，不直接 init
   领域模型；setUpWithError 中 continueAfterFailure = false；启动参数固定
   app.launchArguments = ["--ak-clear-all", "--ak-purge-dates", "--ak-accept-privacy"]
   （隐私门专项改用 --ak-reset-privacy；参数语义见 CLAUDE.md 避坑指南），每用例冷启零累积；
3. 等待一律 waitForExistence(timeout:) 轮询，禁止 sleep 死等；断言「旧页面消失」
   而不仅是「新标题存在」，防同名节点假绿；关键步骤截图存为 xcresult attachment；
4. 系统级授权弹窗由测试自己点掉（UI 测试是模拟器上唯一可靠的点屏通道），
   不假设 simctl 能 tap。

【编译门禁】（必须自行跑通后才算交付，在 AlarmKitSpike/ 子目录执行）
xcodegen generate   # 两个 target 的 sources 都是 folder group，新文件必须重新生成工程
xcodebuild build-for-testing -workspace AlarmKitSpike.xcworkspace \
  -scheme AlarmKitSpike -destination 'platform=iOS Simulator,id=<本机 udid>'
失败必须修到通过，不允许留无法编译的交付物。

【交付物】
1. Swift 文件：单元分支在 AlarmKitSpike/Tests/AgentGenerated/，
   UI 分支在 AlarmKitSpike/UITests/AgentGenerated/；
2. generation-report.md：一张「TC-ID → 分支(unit/ui) → Swift 类/方法 → 所用 oracle
   序号」映射表、人工分流覆盖记录（如有）、skipped 清单（含原因，真机-only 单独标注）、
   build-for-testing 的结论与时间；
3. 全部改动以 git diff / 补丁形式给出，等待人工评审，禁止自行 commit 到受保护分支。
```

模板纪律：

- prompt 本身、输入 JSON 快照、Agent 产出的补丁与 `generation-report.md` 由人工归档到
  testpilot 写根 `external-generation/<export hash8>/`（生成物 gitignore，不入库），
  满足 §3.2 第 7 条的逐 TC 可溯源（报告内映射表逐 TC 列条）；
- 演示话术中外部生成环节必须如实表述为「系统外 Agent + 人审」，不得演示成系统内自动生成。

## 附录 B：演示分支 git runbook（E2 产出，命令可整段复制）

> 事实基线（2026-11-13 核对）：语料仓 app-calendar-alarm 当前分支 `alarmkit-spike`；
> 当前产品工程在子目录 `AlarmKitSpike/`（根目录 project.yml 是已作废的
> CalendarStrongReminder 规格，勿用）；测试 target `AlarmKitSpikeTests`
> （`AlarmKitSpike/project.yml:74`，sources 为 `path: Tests` group，新增文件需
> `xcodegen generate` 重新生成工程）；UI target `AlarmKitSpikeUITests`；
> 存量测试 5 个单测类（Tests/）+ 1 个 UI 测试（UITests/）。
> 以下全部路径相对语料仓根目录，不出现任何机器绝对路径。

### B.1 一次性：创建长期演示分支（人工执行）

```bash
cd app-calendar-alarm
git checkout alarmkit-spike
git pull --ff-only
git checkout -b demo/agent-generated-tests
# 分支声明：在 README.md 顶部加一行「演示分支，永不合入主线，仅承载 Agent 生成测试」
git add README.md
git commit -m "docs: demo branch declaration (agent-generated tests, never merge)"
git push -u origin demo/agent-generated-tests
```

testpilot 侧只读对接（profile 的 `project.repo_path` 指向本仓工作区即可，
连接器全程不发起 push/merge）：

```bash
# 让 testpilot 扫描时看到演示分支，只需人工把本地工作区切到该分支
git checkout demo/agent-generated-tests
```

### B.2 每轮生成：工作分支 → 外部 Agent → 编译门禁 → 人审合入演示分支

```bash
cd app-calendar-alarm
git checkout demo/agent-generated-tests
git pull --ff-only
git checkout -b gen/<export-hash8>          # 一轮导出一条工作分支，hash 取自 E1 文件名

# ① 人工把 case-set-*.json 交给外部 Agent（附录 A 模板），按 §3.2 分流规则产物落
#    单元分支 AlarmKitSpike/Tests/AgentGenerated/ 和/或
#    UI 分支  AlarmKitSpike/UITests/AgentGenerated/，并取得 generation-report.md
# ② 工程再生成 + 编译门禁（在子目录执行；两个 target 的 sources 都是 folder group，
#    任一目录有新文件都必须重新 xcodegen generate）
cd AlarmKitSpike
xcodegen generate
xcodebuild build-for-testing \
  -workspace AlarmKitSpike.xcworkspace \
  -scheme AlarmKitSpike \
  -destination 'platform=iOS Simulator,id=<本机模拟器 udid>'
cd ..

# ③ 人审：逐条核对标记/命名/断言溯源/分流正确性（generation-report.md 映射表的
#    分支列），确认无生产代码改动、无第三方依赖、📱 真机用例未被误生成模拟器 UI 测试
git diff --stat
git diff -- AlarmKitSpike/Sources/      # 必须为空：禁止改生产代码

# ④ 合入长期演示分支（人工，fast-forward 或 --no-ff 均可，保留一轮一分支轨迹）
git add AlarmKitSpike/Tests/AgentGenerated/ AlarmKitSpike/UITests/AgentGenerated/ \
        AlarmKitSpike/AlarmKitSpike.xcodeproj
git commit -m "test(agent): generated tests for <export-hash8> (human reviewed)"
git checkout demo/agent-generated-tests
git merge --no-ff gen/<export-hash8> -m "merge: agent-generated tests <export-hash8>"
git push origin demo/agent-generated-tests
```

### B.3 回流后：testpilot 扫描与执行（系统内，E3/E4 实现，此处只给顺序）

```bash
# 回到 testpilot 工作区；语料仓已在 demo/agent-generated-tests 分支且含新测试
cd ai-testpilot/subsystems
TESTPILOT_PROFILE=config/profiles/ios-local-poc-xcode.yaml uv run testpilot-scan-automation
# E4（切片实现后）：
TESTPILOT_PROFILE=config/profiles/ios-local-poc-xcode.yaml \
  uv run testpilot-impact-rerun --base <base-sha> --head <head-sha> --dry-run
```

### B.4 回滚与对账

- 单轮回滚：`git revert -m 1 <merge commit>`（演示分支可强推的约定由仓库所有者定，
  runbook 默认用 revert 保留轨迹）；
- testpilot 侧重扫即重建 automation-map（E3 幂等），map 文件自身带 `head_commit`，
  与语料仓 HEAD 不一致时 E4 拒绝提交执行并提示重扫，杜绝对着过期映射跑测试；
- 演示分支永不合入 `alarmkit-spike`/`master`；如误合，按语料仓正常 revert 流程处理，
  testpilot 不提供也不需要任何写语料仓的能力。
