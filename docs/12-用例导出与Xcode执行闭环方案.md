# 12 · 用例导出与 Xcode 执行闭环方案（细化设计）

> 状态：**v1.6 = v1.5 全部落地 + E6 USB 反向监听 ws-run 转正**（2026-11-18：批次态 `out/cs-architecture/` 的 C/S 拓扑转正为 workers 正式能力，CLI-first 单用例，新增 `test-without-building -xctestrun` argv 契约与 `usb_relay` profile 块；hermetic 红证六张变异-复原双向闭合，模拟器 E2E 见 docs/03 §3.8。v1.5 = v1.4 全部落地 + 附录 C 外部生成偏差回流与用例变更流程；2026-11-13：E1–E5 已落地并完成真实 xcodebuild 端到端验证；v1.4 修订契约二/附录 A/B，XCUITest 生成从「人工另行开口」升级为契约内一等分支，并修正外部 Agent 阅读清单为语料仓 CLAUDE.md + V1 三文档；全量 1248 测试、verify_s0 24/24。2026-11-17：v1.5 新增附录 C，源自 TC-A1 首批外部生成的三条实测偏差，首批实例见 `subsystems/out/external-generation/tca1/04-corpus-hook-proposal/` 与 `05-amendments/`）｜落地任务见 `.agent-handoff/2026-11-18-usb-cs-productization.md`、`.agent-handoff/2026-11-13-case-export-xcode-loop.md`、`.agent-handoff/2026-11-13-ui-test-contract-and-prompt-fix.md` 与 `.agent-handoff/2026-11-16-h4-childrun-wiring-and-tca1-generation.md`
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

## 3. 数据契约（四个版本化契约，评审重点）

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
6. **禁止事项**：不改 `Sources/` 生产代码、不修改/删除 `Tests/` 与 `UITests/` 存量测试、不引入第三方依赖与新 Pod（外部 Agent 的生成活动受此约束；若实测确认是产品侧测试钩子/能力缺口，按**附录 C** 立 `fixture_change` 类 AR，补丁由 TestPilot 侧起草、人工评审后在语料仓独立分支应用，不属于外部 Agent 的生成 diff）；
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

### 3.4 契约四：通道一刺激能力圈（`stimulus-channel1-v0`，草案；spike S-2/S-3 通过后转正）

切片一（2026-11-13）只落**契约**不引新基建：真机刺激的第一个自动化通道就是现有
xcodebuild 管线内的**包内 XCUITest**（docs/14 方案④），编号"通道一"。通道二
（Appium+WDA）、通道三（go-ios/CoreDevice 私有面）留待 docs/14 P1 spike 后另立契约，
本契约不提前承诺它们的能力。

**通道一能力圈（硬边界，证据 = docs/14 E7：Xcode 27 SDK `XCUIDeviceButton.h` 头文件）**

| 刺激（DeviceStimulus 拟议名） | 通道一判定 | 依据与守卫 |
|---|---|---|
| `volume_up` / `volume_down` | **在圈（官方 API）** | `XCUIDeviceButton.volumeUp/volumeDown`，枚举标 `XCUI_SIMULATOR_UNAVAILABLE`，仅真机可用；模拟器 run 不得声明覆盖 |
| `home` | **在圈（官方 API）** | 同枚举 `.home`，真机限定 |
| `unlock` | **条件在圈（社区技法，非 API 承诺）** | 仅允许 `XCUIApplication(bundleIdentifier:"com.apple.springboard")` 滑屏 + 密码盘路径；守卫见下"解锁技法四守卫"；spike S-3 通过前不得进自动执行 |
| `lock` | **圈外** | 枚举无 lock case（E7）；官方特性面无锁屏动作（devicectl 全量子命令亦无，docs/14 E6）；通道一遇到 lock 必须 fail-loud 或挂起人工，禁止伪装成功 |
| `side_button`（真实 HID 侧键） | **圈外** | 枚举无对应 case（`.action`/`.camera` 不等同侧键 HID 语义）；不得用软件事件冒充硬件侧键，两类事件的 os_log 因果链不同（docs/14 S-5） |

解锁技法四守卫（缺一不可，spike S-3 判据）：① pin 住 iOS 大版本，密码盘布局漂移即
失效；② 断言"锁屏页面消失"而非仅"新标题存在"，防同名假绿（工程纪律同 UI 自动化章）；
③ 设备密码只经 profile/环境变量注入测试 bundle，**密码明文绝不入仓、不入日志、不进
query 回显**；④ 连续 10 次运行零假绿方可转正在圈，证据录屏随 spike 报告封存。

**接合规则（与 docs/14 §7.2 DeviceControlPort 设计稿对齐，本切片不实现 Port）**

- 刺激只允许作用于**已租约的 env_id**：无 EnvironmentPort 租约不裸操作设备，租约口径
  复用既有 acquire → env_id_for_lease → release；真机环境 `reset` 永远 PolicyDenied，
  抹除只能人工经系统设置或 MDM；
- 未来 `DeviceControlPort.capabilities()` 的通道一实现必须逐字按上表声明，不支持的
  DeviceStimulus 不进能力集；worker 预检遇圈外刺激 → 响亮失败或挂起人工混合流，
  **绝不静默跳过**（用户红线，docs/14 方案⑨）；
- 通道一适配器与 xcodebuild 同进程出口（唯一 SubprocessExecutor，argv 数组永不 shell），
  不引入 Node/WDA/Go 任何新运行时；schema 保持 **v15 零 DDL**，刺激记录走既有执行事件
  契约 + ArtifactStorePort 制品 URN。

**A 层（日志归因层）os_log 因果链硬约定**

1. 每个刺激用例必须带可回放时间窗（start/end ISO8601）与 NSPredicate 原文，随 query
   回显落执行记录（FR-EX-006 AC1 同口径）；真机走 `.logarchive` 两步法（docs/03 §3.7），
   manual 模式下人工 collect 命令原文必须随档留存，证据链不断；
2. 刺激生效的断言必须以 os_log 条目（subsystem/process/message + 时间戳）为因果证据，
   不允许仅凭 UI 状态截图判定"刺激已生效"；UI 录屏只作旁证；
3. 时间窗闭区间 `[start, end]`，窗外条目经服务端 `filter_window` 二次剔除，不得入证据
   集（AC2）；NDJSON 坏行计数留片、fail-closed，不用残缺数据凑因果；
4. 模拟器 run 的 `uncovered_layers` 继续显式声明 ios.device 等三层未覆盖；真机 run
   （profile `target: device`）三层物理可验， uncovered_layers 为空，两种口径不得互串；
5. 红证要求：通道一刺激测试必须能抓住"刺激未真实发出"的回归（临时回退 press 调用，
   断言须变红），警惕全程全绿中的假阳性；spike S-2 即该红证的首次真机执行。

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

### E6 USB 反向监听单用例执行转正（ws-run，2026-11-18 落地）

把 gitignored 批次目录 `out/cs-architecture/`（P0/S2–S8 已验证：模拟器三跑、真机五跑）
的反向监听 C/S 拓扑转正为 workers 包正式能力。一期 **CLI-first、单用例 per invocation**；
拓扑与协议背景见 [15](15-混合执行与人工闸门方案.md) §8，分步操作见 [03](03-语料工程与实测环境（AlarmKitSpike）.md) §3.8，术语见 [09](09-术语表（人话版）.md) v1.3。

| 项 | 契约（钉版，改动先改本文档与 handoff） |
|---|---|
| 拓扑 | Runner 内 TestPilotKit 在设备 loopback `127.0.0.1:9876` 起 RFC6455 WS 服务端（NWListener），Mac 驱动是客户端；模拟器共享 Mac loopback 直连，真机经 `iproxy`（usbmuxd）转发。协议标识 `tp-hybrid-ws-p0`，环境键 `TP_LISTEN_PORT/TP_SESSION/TP_TOKEN/TP_TCA1_STRUCTURAL` 与全部消息类型逐字冻结（Swift 按字对线，改名即断协议） |
| argv 契约 | 新增 core 纯函数 `build_xcodebuild_xctestrun_argv`（零 IO），产出：`xcodebuild test-without-building -xctestrun <注入副本> -destination platform=iOS Simulator,id=<udid> (-only-testing <OID>)… [-resultBundlePath <path>] [-maximum-test-execution-time-allowance <正整数秒>]`。与 runbook 手工 `-only-testing:$OID` 冒号形式等价，产品内统一两参数形式；only_testing 空集合直接拒（只做选择执行，不静默退化全量）；udid/平台白名单/allowance 正整数（bool 手滑值显式挡掉）复用 S3.2 校验 |
| 注入契约 | `testpilot-inject-xctestrun`：PlistBuddy 在**源同目录**生成 `.injected.xctestrun`（DerivedData 内、gitignored），FV1/FV2 plist 形态自适应，Delete-then-Add 幂等，UITargetApp 刻意不注；注入失败统一 exit 2 |
| 驱动契约 | `testpilot-ws-gate-driver`：websockets>=14 **延迟导入**（非 mac/无依赖宿主不触发）；四 outcome `complete`(rc0)/`peer_closed`(rc1)/`fail_loud`(rc2)/`connect_timeout`(rc2)，每种收口必落驱动转录 `ws-gate-driver-transcript-v1`；fail-loud = fail 帧 + close 1008 + rc2。`peer_closed` 覆盖 reject 真实链：XCTAssert 抛错 → XCTest 拆 NWConnection，驱动留痕 rejected 闸门而不是吐 traceback |
| 编排与退出码 | `testpilot-hybrid ws-run`（交互式子命令，**不进 worker 队列**：worker 无 TTY）。driver-first：驱动先起（retry 窗 300s 硬下限兜 xcodebuild 启动墙钟），xcodebuild 后起；first-exit 对账（驱动非零先退立即 terminate 对方，shield 宽限防 CancelledError 灌入子进程 wait）。退出码 **0 全绿 / 2 硬错误 / 3 产品失败**（有产物的 xcodebuild 失败，含 reject→serverFail）；**reject 永不自动冲绿**，无 reject 留痕的提前断链判硬错误（Runner 崩溃/环境故障） |
| profile 块 | `ios-local-poc-xcode.yaml` 增 `usb_relay`（13 键；老 profile 缺省 `{}` 零影响）：`relay_url_template/listen_port/session/retry_seconds(≥300 硬校验)/grace_seconds/auto_delay/structural_default/time_allowance_s(须大于 Swift 三闸硬超时之和)/gates_human/gates_structural/evidence_subdir/iproxy.{enabled,local_port,device_port}/xctestrun.output_suffix`。iproxy 默认 `enabled: false`，device 目标遇关闭直接 exit 2；二进制缺失翻译硬错误带 `brew install libimobiledevice` 指引 |
| 账本（B1） | 会话目录 `<data_dir>/ws-relay-sessions/wssess-<UTCstamp>-<hash8>/`：证据 PNG + 驱动转录 + 正式 `transcript.json`（schema `ws-gate-transcript-v1`，四 outcome passed/product_failure/protocol_error/hard_error 对 0/3/2/2；tmp+os.replace 原子写；证据经 LocalFsArtifactAdapter 取 `urn:testpilot:localfs:` URN，适配器缺失/抛错不写假 URN）。**一期不写 test_runs 表、不碰 FileGateStore**，`test_run_ref` 恒 null 给二期回填；schema 保持 v15 零 DDL |
| token 红线 | 运行期 `TESTPILOT_RELAY_TOKEN` 环境变量注入；缺省回退线协议默认 `tok-p0` 并 stderr 警告；yaml/转录/快照只许出现键名与掩码，绝不写明文 token |
| 收口对账 | best-effort `lsof -nP -iTCP:<port> -sTCP:LISTEN` 结果入转录，工具失败只转录不致命；iproxy 在 finally 回收，被杀进程账本 rc=None（编排器收口语义，非信号负数） |
| 测试与红证 | 全假件 hermetic：`test_ws_relay_session.py` 32 条（FakeProcess 事件门控 hang 模式建模真实进程时序，杜绝同 tick 集合顺序 flaky）+ 驱动 36 + 转录 30 + spawn_attached/CLI 既有套件；红证六张 A–F（terminate 摘除/无产物误分型/reject 误分型/iproxy 异常不翻译/driver-first 反序/peer_closed 兜底摘除）全部变异-复原双向，零 TEMP-RED 残留；`websockets>=14` 显式入 workers pyproject（S8 裸 python3 事故教训），uv.lock 仅 +2 行零其他包漂移 |
| 验证边界 | 模拟器 structural E2E 见 docs/03 §3.8（五验收 + 错令牌红证 + 强制复绿），**结论不外推真机**；iproxy 代码本期落地但不经真机验证，S9 真机人工验收按用户 2026-11-18 指令暂缓、四取证点在 runbook 03 待命；TestPilotKit Swift 本期零改动 |

## 5. 验收总表与红证清单

| 验收 | 方式 |
|---|---|
| 单测 | 三个新 core 纯函数模块 hostless 测试 + 三个 CLI 的夹具测试，进 pytest 全量 |
| 契约 | case-export-v1 / automation-map-v1 各一份封存样例 + schema 自校验测试 |
| 刺激契约（草案） | `stimulus-channel1-v0`（§3.4）：通道一能力圈以 docs/14 E7 头文件取证为界，转正门槛 = 真机 spike S-2（音量键红证）/S-3（解锁四守卫、连续 10 次无假绿）通过并回填 docs/14；未转正前 lock/side_button 一律 fail-loud 或挂起人工 |
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
5. 不把外部 Agent 生成的代码拷入本仓，不把演示分支合入语料仓主线；
6. （v1.6 增）不做 WS↔gate store 常驻桥接进程与 web 交互页：一期人工介入走 `testpilot-hybrid ws-run` 终端交互，web 整体留二期（与 [11](11-Web控制台功能设计-补全方案.md) §12 SSE/WS 推送禁令边界一致，不引 Node/WDA/Go 新运行时）；
7. （v1.6 增）不做多会话循环：一期一次调用一条 TC-Ax（单会话 complete 即收口）；一条 xcodebuild action 串行多用例的 gate_id 自路由多会话列二期靠前，hello 帧不加 test_id、转录 schema 加法带默认值；
8. （v1.6 增）不写 test_runs 表、不接 FileGateStore（core 四态无 rejected、gate id 闭集不同构、双时钟会给假续期）；B1 本地转录 + localfs URN 是一期账本，`ws-relay:` 幂等前缀的终态直写导入器留二期；
9. （v1.6 增）不做 build-for-testing 自动化与 DerivedData 自动发现（`--xctestrun` 必须显式）；iproxy 代码随 E6 落地但一期不启真机验证（device profile 不启用，S9 按用户指令暂缓）；TestPilotKit Swift 零改动、不进 Python 包。

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
3. 等待一律 waitForExistence(timeout:) 轮询，**禁止 `sleep(`/`usleep(`/`Thread.sleep`
   死等**——静态红线 GS-001 会直接扫红（门禁命令见下方【静态红线门禁】），注释与字符串
   遮罩后判定，写在注释里的说明文字不误伤；断言「旧页面消失」而不仅是「新标题存在」，
   防同名节点假绿；关键步骤截图存为 xcresult attachment；
4. 系统级授权弹窗由测试自己点掉（UI 测试是模拟器上唯一可靠的点屏通道），
   不假设 simctl 能 tap；
5. **接力（relay）段附着规则（H5 新增，红线 GS-002/GS-003）**：混合执行中段 B 用例
   附着的是系统接力场景里**已存活的进程**，只能 `app.activate()` 附着，禁止
   `app.launch()` 冷启动——冷启动销毁系统接力场景且让状态跨用例累积。同一 test 方法内
   activate 与 launch 并存即 GS-002 红；一个 test 方法出现两次以上 `.launch(` 即 GS-003 红。
   每个用例至多一次冷启动，且只允许出现在 setUp/首段；
6. **六类前置条件的承接模板（与 hybrid-plan-v1 前置 kind 一一对应，生成代码不得越界
   自行实现）**：

   | 前置 kind | 生成代码/运行期承接方式 |
   |---|---|
   | `device_unlocked` | 无代码承接：锁屏/侧键永久是 human:action 闸门（无密码测试机策略不立），生成代码不得尝试解锁 |
   | `session_window_materialized` | 无代码承接：DH-AX 运行期探针负责，不满足走 repair/人工闸门 |
   | `portrait_orientation` | 无代码承接：runner 侧 rotate repair（截一张判一张，像素判方向），生成代码禁止自行旋转设备 |
   | `app_not_running` | devicectl 无进程列表动词、恒 unknown：需要干净态时走 `--terminate-existing` 冷启动 + 启动参数钩子，不假设进程死活 |
   | `no_active_alarms` | 固定 launchArguments 带 `--ak-purge-dates`（清活钟），由 xcodebuild 段兜底；生成代码不自行删库 |
   | `notification_permission` | 恒 unknown、只能人工闸门确认；隐私门专项用 `--ak-reset-privacy`，授权弹窗由 UI 测试自己点（见规则 4） |

【编译门禁】（必须自行跑通后才算交付，在 AlarmKitSpike/ 子目录执行）
xcodegen generate   # 两个 target 的 sources 都是 folder group，新文件必须重新生成工程
xcodebuild build-for-testing -workspace AlarmKitSpike.xcworkspace \
  -scheme AlarmKitSpike -destination 'platform=iOS Simulator,id=<本机 udid>'
失败必须修到通过，不允许留无法编译的交付物。

【静态红线门禁】（H5 落地，编译门禁之前先过；仅管辖 AgentGenerated/ 内生成文件）
testpilot 对语料仓所有路径含 `AgentGenerated` 段的 `.swift` 文件做静态扫描
（core 纯函数 `generated_swift_lint`，零 IO 可单测；先做注释/字符串遮罩再匹配，
文档说明里的 `sleep(` 字样不误伤；圈外人工存量代码一律不扫、直接拒绝越界请求）：

| 规则 | 判定 | 修复方向 |
|---|---|---|
| GS-001 | 出现 `sleep(` / `usleep(` / `Thread.sleep` | 改 `waitForExistence(timeout:)` 轮询 |
| GS-002 | 同一 test 方法内 `app.activate()` 与 `app.launch()` 并存 | 接力段删掉 launch，只保留 activate 附着 |
| GS-003 | 同一 test 方法内两次以上 `.launch(` | 每用例至多一次冷启动，重入改用 activate |

```bash
# subsystems/ 目录下；--repo 指向语料仓根，缺省走容器 repo 坐标
uv run testpilot-scan-generated-swift --repo <语料仓根目录>
# 退出码：0 零违规；2 硬错误（仓不可达/IO 失败）；3 存在违规（JSON 报告照常输出，
# 含文件/行号/代码片段，逐条可定位）
```

红证纪律：扫描器自带 hostless 红证测试（临时植入 `sleep(2)` 必红、回退轮询写法转绿、
注释/多行字符串里的关键词不误伤、圈外路径 fail-closed），红线规则任何变更必须同步红证。

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

---

## 附录 C：外部生成偏差回流与用例变更流程（AR 流程，v1.5 新增）

> 来源：2026-11-17 TC-A1 首批外部生成（`generation-report.md` DEVICE-VERIFY 章）登记
> 三条实测偏差。本附录把「偏差如何收口、什么情况下才允许改用例」固化为流程，后续每一轮
> 外部生成均按此处理。首批实例与模板：`subsystems/out/external-generation/tca1/`
> 下 `04-corpus-hook-proposal/`（语料仓产品侧补丁提案）与 `05-amendments/`（AR 文件）。

### C.1 总原则（不可绕过）

1. **用例评审库是唯一事实源，Swift 测试是下游生成物。** 收口方向只能是「偏差 → 用例库
   变更 → 重新导出 → 重新生成」，禁止只改 Swift 断言而库内规格不动（否则 automation-map
   锚定的规格与分支上的实际断言漂移，整条导出闭环失去可信度）。
2. **外部 Agent 只有登记权，没有规格修改权。** 实测与锁定规格不符时，逐字保持规格、
   在 `generation-report.md` 登记证据并提请人工确认（与附录 A 任务书 §6.2 同口径）；
   修订必须等 AR 裁决后由新一轮生成承接。
3. **语料仓只读纪律不变。** TestPilot Agent 不向语料仓写任何文件；跨边界产物（语料仓
   补丁草稿、用例文档修订块、新版 prompt）一律落本仓 gitignored 目录，由**人工携入**。
4. **无证据不受理。** 每条 AR 必须附实测证据（xcresult/截图附件/os_log 摘录/源码行号
   至少一项），推测性偏差退回补充取证。

### C.2 偏差三分类（先分类，再定路径）

| 分类 | 判定标准 | 改用例吗 | 审批权重 | 典型收口 |
| --- | --- | --- | --- | --- |
| **product_defect（产品缺陷）** | 规格成立、产品实现与规格不符，且产品侧已有/应有修复 | **不改**，规格一字不动 | 低：确认修复合入即可 | 语料仓合入修复 commit，重跑同一规格验证转绿 |
| **fixture_change（夹具/刺激方式变更）** | 断言成立，但既有测试钩子/夹具驱动不出规格要求的状态（能力缺口） | 只改 **steps 的刺激方式**，oracle 不动 | 中：审 steps 改动与新钩子设计 | 语料仓先补测试钩子（走产品正规链路），再修订步骤文本与 Swift 启动参数 |
| **spec_amendment（规格修订）** | 规格自身与平台事实不符（SwiftUI 呈现模型、系统权限归属等），原断言在当前平台恒红或恒真 | 改 steps 和/或 oracle | **高**：强制红证方案，禁止弱化成恒真断言（与 docs/15 §9 防假绿同源） | 修订断言锚点/补条件步骤，mutate-red 证明新断言仍抓得住缺陷 |

分类有争议时从高不从低（按更严一档走审批与红证）。

### C.3 六阶段流程

1. **登记（AR）**：每条偏差复制 `05-amendments/_AR-TEMPLATE.md` 建一份
   `AR-NNN-<简短名>.md`，填证据、分类、拟议变更、影响面、红证方案；在任务 handoff 与
   本附录实例索引登记。登记不阻断其他用例的生成与执行。
2. **人工裁决**：按 C.2 权重审批。spec_amendment 必须当场确认红证方案可执行；裁决结论
   以**日期追加**写在 AR 内，原始偏差登记与证据不删不改。
3. **产品基线先冻结**：语料仓侧动作（合入缺陷修复、应用钩子补丁）全部由人工在演示分支
   完成并过 `xcodegen generate` + `build-for-testing` 门禁；产品基线未定型前不得动
   TestPilot 侧规格。
4. **库内修订（身份稳定、版本自动递增）**：
   - 用例 `external_id` **保持不变**（TC-A1 修订后仍是 TC-A1），不新建「TC-A1-r2」，
     保证 OID、automation-map、hybrid 计划接线不断；
   - 修订动作落在语料仓用例文档（演示分支），TestPilot 侧重跑 ingest 即可：
     `TestCase.version` 由文档内容 hash 自动赋值（`case_docs.py` 解析期
     `version=sha256(文档文本)[:12]`），文档一改版本自动换新，零 DDL；
   - 修订前导出的 `case-set-<UTC>-<hash8>.json` 是不可变快照，原样留存，AR 记录
     新旧 version 与两个快照文件名。
5. **下游再生成（内容寻址自动换代）**：重新导出 case-set → 出新版 prompt 包（v2/v3…）
   → 外部 Agent 按修订规格改 Swift，重跑构建门禁与 GS 扫描 → 重建 hybrid-plan。
   plan 是内容寻址制品，规格文本一变 hash 自动变；旧 plan 文件与 `hsess-*` 会话账本
   原样保留为历史证据（加日期注记，不删除不覆盖）。
6. **红证 + 真机验收**：每条获准 AR 做 mutate-red/revert-green（临时回退产品修复/钩子、
   恢复旧断言，分别证明对应测试变红后复原），零 TEMP-RED 残留，再进 hybrid E2E runbook
   的真机走查。红证记录贴回 AR。

### C.4 具体操作步骤（命令级，按角色分工）

> 路径均为相对路径；`<语料仓根>` 指 app-calendar-alarm 工作区，`<hash8>` 取当轮
> case-set 文件名中的 8 位 hash。TestPilot 侧命令在 `subsystems/` 目录执行。

**阶段 1–2：登记与裁决（TestPilot Agent 起草，人工裁决）**

```bash
# 在本仓（gitignored 工作态，不入库）
cp subsystems/out/external-generation/<批次>/05-amendments/_AR-TEMPLATE.md \
   subsystems/out/external-generation/<批次>/05-amendments/AR-001-<简短名>.md
# 填证据/分类/拟议变更/红证方案；人工在 AR 末尾追加 dated 裁决（批准/驳回/改分类）
```

**阶段 3：产品基线冻结（纯人工，语料仓演示分支）**

```bash
cd <语料仓根>
git checkout demo/agent-generated-tests && git pull --ff-only
git checkout -b fix/<AR 编号>-<简短名>
# 3a. product_defect：合入已评审的修复（如 TC-A1 偏差 2 的 853cef0）
git merge <修复分支或 commit>
# 3b. fixture_change：按 04-corpus-hook-proposal/ 的补丁草稿人工应用到 Sources/
#     （草稿含锚点说明与 .patch；行号漂移时按 README 的完整代码块手工落）
cd AlarmKitSpike
xcodegen generate && pod install
xcodebuild build-for-testing -workspace AlarmKitSpike.xcworkspace -scheme AlarmKitSpike \
  -destination 'platform=iOS Simulator,id=<本机模拟器 udid>'
cd ..
git add AlarmKitSpike/Sources AlarmKitSpike/AlarmKitSpike.xcodeproj
git commit -m "fix(hook): <AR 编号> <一句话变更>"
```

**阶段 4：用例文档修订 + 库内重灌（人工携入，TestPilot 侧人工跑命令）**

```bash
# 4a. 人工把 05-amendments/case-doc-amendment-<TC>.md 的修订块应用到
#     <语料仓根>/docs/V1测试用例文档.md（仅演示分支；主线文档不动），提交
# 4b. 回 TestPilot 重灌（profile 必须是持有评审库的 ios-local-poc-xcode；
#     四命令序列，缺步会让旧 oracle 建议 URN 原样复活）：
cd subsystems
export TESTPILOT_PROFILE=config/profiles/ios-local-poc-xcode.yaml
uv run testpilot-ingest-cases            # 增量：文档 hash 变化自动换 version
uv run testpilot-extract-requirements    # 需求 AC 未改也照跑，防幽灵 URN 复活
uv run testpilot-mine-oracles
uv run testpilot-lint-cases              # 退出码 0/1/2 语义见 docs/13；error 必须为 0
# 4c. 取证：记录修订前后 TestCase.version（文档 hash 12 位）写回 AR
```

**阶段 5：再导出与再生成（TestPilot Agent 备料，外部 Agent 执行）**

```bash
# 5a. 新快照（新 UTC 时间戳文件名，旧快照不删）
uv run testpilot-export-cases --out out/external-generation/<批次>/01-case-export
# 5b. 按 AR 裁决更新 02-prompt-package 任务书（版本号递增，变更点置顶列 diff），
#     连同新 case-set 人工交外部 Agent；Swift 只许改 AR 批准的行（类名/方法名/OID 冻结）
# 5c. 外部 Agent 在语料仓重跑：xcodegen → build-for-testing → GS 扫描
uv run testpilot-scan-generated-swift --repo <语料仓根>   # 必须 exit 0
# 5d. 重建 hybrid-plan（输出新内容寻址文件 + 覆盖稳定名副本，旧文件留存）
uv run python out/hybrid-tca1/build_tca1_plan.py
```

**阶段 6：红证与真机（人工执行为主，TestPilot Agent 只读复验）**

```bash
# 6a. 红证（在语料仓临时回退，验完立即复原，工作区零 TEMP-RED 残留）：
#     - product_defect：git revert 修复 commit → 对应测试必红 → 复原
#     - fixture_change：注释新钩子调用走旧钩子 → 首页行断言必红 → 复原
#     - spec_amendment：临时恢复旧断言 → 在新基线上验证旧断言仍红（证明改的是事实不是放水）
# 6b. 按 out/hybrid-tca1/runbook/tca1_e2e_runbook.md 跑六步真机走查（三进程同 device profile）
# 6c. AR 回填红证输出 + 真机 xcresult/os_log/录屏证据位置，标 closed
```

### C.5 版本链与证据保全对照

| 制品 | 版本机制 | 修订后的处置 |
| --- | --- | --- |
| 语料仓用例文档 | git 轨迹（演示分支） | 修订块带日期小节；主线文档不动 |
| `TestCase.version` | 文档 sha256 前 12 位，ingest 自动赋值 | 重灌即换，AR 记录新旧值 |
| case-set 导出 | 文件名带 UTC 时间戳 + hash8 | 每轮新文件，旧文件不可变留存 |
| prompt 任务书 | 包内版本号（v1/v2…） | 新版置顶列与上版 diff |
| hybrid-plan | 内容寻址 hash 文件名 | 新 hash 新文件；旧 plan、`hsess-*` 账本保留加日期注记 |
| generation-report / AR | dated 追加 | 原始偏差登记永不删改，裁决与红证向下追加 |

### C.6 首批实例索引（TC-A1，2026-11-17）

| AR | 分类 | 偏差摘要 | 裁决建议 | 状态 |
| --- | --- | --- | --- | --- |
| AR-001 | fixture_change | `--ak-schedule` 只走 `schedulePrimary` 不写 `RoutineAlarmStore`，首页无快速闹钟行 | 批准新增 `--ak-schedule-quick` 钩子（`04-corpus-hook-proposal/`），oracle 不动、steps 换夹具 | 已批准并落地（2026-11-17，语料仓 `81bc80f`，段 A 模拟器转绿 13.25s） |
| AR-002 | product_defect | 接力页标题关键词启发式误判家族，按钮文案不符 | 规格不动；人工合入 853cef0（RelayKindPolicy） | 已批准并落地（cherry-pick 为 `610a102`，hostless 97/97 绿） |
| AR-003 | spec_amendment | cover 下首页标题不离 AX 树，负向断言恒红；Debug 广告层需条件「跳过」步骤 | 断言改锚接力页专属元素 + 补条件步骤；报告「钩子仅 DEBUG 编译」说法经源码证伪，已在 AR 内更正 | 已批准（2026-11-17；注：语料仓随后合入 `8ebd07c`，钩子区当前整体 DEBUG 编译，AR-003 内有日期注记保留两态事实） |
| AR-004 | fixture_change | 每个 xcodebuild test action 重装 AUT，跨 action 一次性钟被 mobiletimerd 物理撤销，双段（s1/s6）架构结构性必红 | 2026-11-17 裁决：A/B 临时方案不采纳，定案通用 C/S 架构（TestPilot server + XCTest client 单方法全步骤 + WS/轮询中断协议），调研设计稿见 `subsystems/out/cs-architecture/`，按 P0–P3 分期落地 | 设计已裁，实现待启动 |
