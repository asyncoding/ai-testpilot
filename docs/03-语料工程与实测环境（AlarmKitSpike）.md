# 语料工程与实测环境备忘（AlarmKitSpike）

> Demo 的首选语料/被测工程是本机上的真实 iOS 工程 **AlarmKitSpike**
> （`~/Documents/Code/app-calendar-alarm/`，git 分支 alarmkit-spike）。
> 本文是 M1（连接器）、M6（测试执行与归因）开发时的环境事实清单，由闹钟项目 CLAUDE.md
> 与本会话核实。**机器相关路径仅作示例，代码里一律走配置/参数，不写死。**
> ⚠️ 本文中的命令是 Demo 工具层（M6）将来实际要调用的命令，W3/W4 开发时需逐条实测校准。

## 1. 工程事实

| 项 | 值 |
|---|---|
| 工程根 | `app-calendar-alarm/AlarmKitSpike/`（xcodegen 工程，必须在**这个子目录**跑 xcodegen；根目录另有旧实验工程 CalendarStrongReminder，勿混） |
| workspace | `AlarmKitSpike.xcworkspace`（CocoaPods 集成，命令行构建用 workspace 不用 project） |
| scheme | `AlarmKitSpike`（test action 含 AlarmKitSpikeTests + AlarmKitSpikeUITests 两个 target） |
| 部署目标 | iOS 26.0+，Swift 6，仅 iPhone |
| 主模拟器 | iPhone 17（iOS 27.0）udid `5411787C-8AFF-4EBF-9000-EB5EFD1457D5`（2026-10 时在用于回归） |
| 真机 | iPhone 16 Pro / iOS 26.4（后台音频、侧边键、签名类验证只能真机，模拟器暴露不了） |
| 统一日志 subsystem | `com.thames.alarmkitspike`（共享 `Shared/AppLogger.swift`；扩展由系统进程宿主，只能 os_log 不能 print） |
| 测试分类 | AlarmKitSpikeTests = **hostless 纯逻辑 bundle**（86 个，无宿主 App，纯逻辑源文件直接编进 test target）；UITests = XCUITest（ProductFlowUITests test01–test12，是无头模拟器上唯一能点系统弹窗/跨页流程的通道） |
| 工程再生 | `xcodegen generate` 后**必须 `pod install`**（会清掉 CocoaPods 集成与自动 schemes，project.yml 已固化共享 scheme） |

## 2. 可作为语料的资产清单（M1/M2 接入对象）

```
app-calendar-alarm/
├── CLAUDE.md                                   # 高密度避坑知识（事故/根因/命令），语料金矿
├── docs/                                       # V1 三件套 + 20+ 篇真机事故复盘
│   ├── V1需求文档-强提醒闹钟.md
│   ├── V1技术设计文档.md
│   ├── V1测试用例文档.md                        # 结构化用例（部分"只有步骤"，oracle 挖掘素材）
│   └── AlarmKitSpike-*.md                      # 真机验证/事故复盘（带时间戳日志，属"历史证词"）
└── AlarmKitSpike/
    ├── project.yml
    ├── Sources/   Shared/                      # 生产代码（SwiftSyntax AST 按函数/类切块）
    ├── Tests/                                  # hostless 单测（断言本身就是 oracle 来源）
    ├── UITests/ProductFlowUITests.swift        # UI 测试（步骤+断言+快照名）
    └── scripts/                                # verify_alarmupdates_kill.sh、lunar-spike/ 等
```

另有 git 历史（空/泛 message 与详尽 message 并存，正好演示"不依赖 message、从 diff 抽变更"）、
CI 产物（xcresult）、运行日志（os_log 归档）。

## 3. M6 工具层要封装的命令（候选，开发时实测校准）

```bash
# 3.1 跑 hostless 纯逻辑测试（快，秒级~分钟级）
cd AlarmKitSpike
xcodebuild test \
  -workspace AlarmKitSpike.xcworkspace \
  -scheme AlarmKitSpike \
  -destination 'platform=iOS Simulator,id=5411787C-8AFF-4EBF-9000-EB5EFD1457D5' \
  -only-testing:AlarmKitSpikeTests \
  -resultBundlePath /tmp/hostless.xcresult

# 3.2 跑单条/整组 UI 测试（慢，单条可达数分钟~11 分钟）
xcodebuild test \
  -workspace AlarmKitSpike.xcworkspace -scheme AlarmKitSpike \
  -destination 'platform=iOS Simulator,id=<udid>' \
  -only-testing:AlarmKitSpikeUITests/ProductFlowUITests/test10_WakeForm_SoundPickedThenSwipeStillBlocked

# 3.3 抓 App 日志（模拟器流式抓取，归因用）
xcrun simctl spawn booted log stream \
  --predicate 'subsystem == "com.thames.alarmkitspike"'

# 3.4 抓系统侧进程日志（ClockAngel/mobiletimerd 等）
xcrun simctl spawn booted log show --last 20m \
  --predicate 'process == "ClockAngel" OR subsystem == "com.apple.AlarmKitCore"'

# 3.5 从 xcresult 导出失败信息/附件（截图、录屏）
xcrun xcresulttool get test-results tests --path /tmp/hostless.xcresult
xcrun xcresulttool export attachments --path <result.xcresult> --output-path /tmp/att
# 录屏抽帧取证：ffmpeg -i <attachment>.mp4 -vf fps=2 frame_%03d.png
```

> **S3.3 实现注记（2026-11-10，hostless 已落地，真实输出待手动探针校准）**：
> 上文是候选命令原型，工程实现为适配证据可复现性做了三处收口——
> ① 日志命令不用 `spawn booted`，改 `spawn <租约 udid>`，不依赖调用时刻的隐式
> "谁是 booted"；不用 `--last 20m` 相对时间，改 `--start/--end` 显式 ISO8601 窗
> （`OsLogQueryAdapter`，FR-EX-006 AC1 要求查询条件可逐字重放），并固定
> `--style json` 走 NDJSON 行解析；
> ② xcresulttool 链路固定三命令：`get test-results tests --format json`（用例树）、
> `get test-results attachments --format json`（附件归属清单）、有附件才
> `export attachments --output-path`（字节导出）；附件经 localfs 制品端口按
> sha256 内容寻址存 URN，失败用例经 `artifact_urns` 关联（FR-EX-005 AC1）；
> ③ ffmpeg 抽帧 argv 由 `ffmpeg_commands.py` 白名单拼装（`-y` 幂等覆盖、
> fps 仅允许 1..30 整数、输出固定 `frame_%03d.png`），真实 ffmpeg 仅手动探针调用。
> 合成 JSON 形态是自造夹具，真实 Xcode 16 字段名以手动探针实测为准（未知形态
> 解析器 fail-closed，不会静默吞）。

注意：

- `-resultBundlePath` 目录已存在时 xcodebuild 拒覆盖，先删；
- `simctl erase` 必须先 shutdown；UI 测试每用例以 `--ak-clear-all` 启动互不污染；
- 上文 `-only-testing:Foo` 是冒号单参写法；Agent 实现（S3.2 `xcode_commands.py`）
  统一用 `-only-testing`、`Foo` 两个 argv 的数组写法，两者 xcodebuild 语义等价，
  数组写法配合 `create_subprocess_exec` 无 shell 解析面，命令注入面最小；
- 真机走 `xcrun devicectl device install/launch --device <udid> --console <bundle>`，
  设备必须解锁；Demo 阶段用模拟器即可，真机路径在方案里作为"生产化扩展"讲。

## 4. 对 Demo 各模块的喂料方式

| 模块 | 怎么用这个工程 |
|---|---|
| M1 连接器 | 文件连接器扫 docs/Sources/Tests/UITests；Git 连接器读 log/show/diff/blame；CI 连接器解析 xcresult（本地文件模拟） |
| M2 加工管线 | 文档 400–600 字切块；Swift 代码 AST（SwiftSyntax）按函数/类切；测试按 test 方法切；实体：函数/类/用例 ID/文档章节/事故 |
| M3 L1 关系 | 用例↔符号（用例名/注释中的机制关键词）、事故↔修复 commit↔符号（事故文档里有 commit/hash 与日志） |
| M4 问答 | 示例问题库："侧边键停钟的四层兜底是什么""kill backup 为什么从 90s 改 30s，波及哪些常量""为什么不能用 CNContactStore" |
| M5 用例生成 | 喂一份需求（如 docs 里的新 FR），对照 V1测试用例文档风格生成测试点/用例；oracle 从 Tests/ 断言与事故文档挖 |
| M6 执行归因 | 真实跑 3.1/3.2；归因类别用本工程真实历史：用例期望过期（90s→30s 断言改动）、模拟器不暴露真机行为、授权竞态、flaky |
| M8 保鲜 | 现场重命名一个符号（如 schedulePrimary）演示 stale/reindex；事故文档作"历史证词"不改写只加版本标注 |

## 5. 隔离与合规（演示叙事）

- 语料在本机处理，本地 embedding/Ollama 不出域——对应私有化部署与安全合规要求；
- 演示用"缺陷工单"用 JSON 夹具模拟，不接真实工单系统；
- 评测集与入库语料严格分开：从 docs 事故文集中留 10–20 篇做留出问答集，**不入库、不进 prompt 示例**。
