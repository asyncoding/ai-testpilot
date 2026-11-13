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

> **S3.3 实现注记（2026-11-10 真机探针校准后，Xcode 27 / xcresulttool 25115）**：
> 上文是候选命令原型，工程实现为适配证据可复现性做了收口，括号内为真机实测事实
> （探针报告 `subsystems/out/eval/real-device-probe.md`）——
> ① 日志命令不用 `spawn booted`，改 `spawn <租约 udid>`，不依赖调用时刻的隐式
> "谁是 booted"；不用 `--last 20m` 相对时间，改 `--start/--end` 显式窗
> （`OsLogQueryAdapter`，FR-EX-006 AC1 要求查询条件可逐字重放）。真机校准：
> 工具只吃 `%Y-%m-%d %H:%M:%S` 空格格式（T 分隔 rc=64），固定 `--style ndjson`
> 行解析（`--style json` 真机是美化 JSON 数组，无法逐行解析）；每条流末尾有
> 信封尾行 `{"count":N,"finished":1}`，解析器识别后跳过、残缺形态仍 fail-closed；
> echo 层保留 ISO8601 原值，工具语法与可复现描述分层；
> ② xcresulttool 25115 链路固定**两动词**：`get test-results tests --format json`
> （用例树）+ `export attachments --output-path`（自动写 manifest.json 顶层数组，
> 旧 `get test-results attachments` 已下线 rc=64）；导出目录已含 manifest.json 时
> 工具 rc=1 拒覆盖，适配器导出前清场保证同幂等键重试安全；附件经 localfs 制品端口
> 按 sha256 内容寻址存 URN，失败用例经 `artifact_urns` 关联（FR-EX-005 AC1），
> 全绿 run 实测 manifest 为空数组、URN 全空属常态；
> ③ ffmpeg 抽帧 argv 由 `ffmpeg_commands.py` 白名单拼装（`-y` 幂等覆盖、
> fps 仅允许 1..30 整数、输出固定 `frame_%03d.png`），真机 7.1 抽帧契约零改动。
> 另两个真机事实：`simctl io recordVideo` 只认 SIGINT 优雅停录（SIGTERM 会泄漏
> SimRenderServer 录制句柄导致后续录制 Resource busy）；静态屏幕只产出 0.07s
> 空视频，录屏需先制造 UI 活动（launch App + appearance 切换）。
> 合成 JSON/NDJSON 是自造夹具，已按真机形态校准；未观测形态解析器 fail-closed。

注意：

- `-resultBundlePath` 目录已存在时 xcodebuild 拒覆盖，先删；
- `simctl erase` 必须先 shutdown；UI 测试每用例以 `--ak-clear-all` 启动互不污染；
- 上文 `-only-testing:Foo` 是冒号单参写法；Agent 实现（S3.2 `xcode_commands.py`）
  统一用 `-only-testing`、`Foo` 两个 argv 的数组写法，两者 xcodebuild 语义等价，
  数组写法配合 `create_subprocess_exec` 无 shell 解析面，命令注入面最小；
- 真机走 `xcrun devicectl device install/launch --device <udid> --console <bundle>`，
  设备必须解锁；Demo 阶段用模拟器即可，真机路径在方案里作为"生产化扩展"讲。

### 3.6 真机执行+取证 runbook（2026-11-11 据真机探针校准，**手动步骤永不进 verify_s0.sh**）

把"建机→跑测→取制品→取日志→录屏抽帧→拆机"串成一条可照跑的命令序列。环境事实：
Xcode 27.0、xcresulttool 25115（schema 0.4.0）、ffmpeg 7.1、iOS 26.4 模拟器 runtime；
探针蒸馏报告 `subsystems/out/eval/real-device-probe.{md,json}`（out/ gitignore）。

**安全纪律（先于一切命令）**：

1. 只允许操作本序列新建的**专用一次性模拟器**（命名 `testpilot-probe`，用完即 delete）；
   用户自己 Booted 的设备一条命令都不许碰。
2. **每个 simctl 调用必须带显式 udid**（用变量 `PROBE_UDID`），禁止 `spawn booted`
   之类依赖隐式"当前设备"的写法；**禁止裸 `simctl erase all`**，erase 只能
   `simctl erase "$PROBE_UDID"` 且先 shutdown。
3. 建机前与拆机后各存一份 `xcrun simctl list` 快照，diff 必须只剩专用机的
   创建/删除两行，作为用户设备零接触的证据。
4. 语料仓只读：不注入故障、不修改被测工程；失败路径证据用官方 `--schema` 报错与
   合成夹具，不人为制造红测。
5. 本序列是人工校准手段，**不进自动门禁**；自动化层（A19–A23）全部 hermetic，
   不起模拟器、不起端口。

```bash
set -euo pipefail

# ── 0. 建机前快照（取证基线）──────────────────────────────────────────────
xcrun simctl list > /tmp/simlist-before.txt

# ── 1. 建专用机并启动（runtime id 用 `xcrun simctl list runtimes` 查）────────
PROBE_UDID=$(xcrun simctl create testpilot-probe "iPhone 17" \
  "com.apple.CoreSimulator.SimRuntime.iOS-26-4")
xcrun simctl boot "$PROBE_UDID"
xcrun simctl bootstatus "$PROBE_UDID" -b          # 等启动完成再继续

# ── 2. 录屏（可选，失败视觉取证）：先制造 UI 活动，否则静态屏只有 ~0.07s 空视频
xcrun simctl launch "$PROBE_UDID" com.apple.mobilesafari
xcrun simctl ui "$PROBE_UDID" appearance dark
xcrun simctl ui "$PROBE_UDID" appearance light
# 后台录制；停录只认 SIGINT（SIGTERM 会泄漏 SimRenderServer 句柄 → Resource busy）
xcrun simctl io "$PROBE_UDID" recordVideo /tmp/probe.mp4 &
RECORD_PID=$!

# ── 3. 跑测试：resultBundlePath 已存在会被 xcodebuild 拒覆盖，先删 ───────────
rm -rf /tmp/probe.xcresult
xcodebuild test \
  -workspace AlarmKitSpike.xcworkspace \
  -scheme AlarmKitSpike \
  -destination "platform=iOS Simulator,id=$PROBE_UDID" \
  -only-testing:AlarmKitSpikeTests \
  -resultBundlePath /tmp/probe.xcresult

# ── 4. SIGINT 优雅停录，确认 mp4 完整（Wrote video to disk）─────────────────
kill -INT "$RECORD_PID" && wait "$RECORD_PID" || true

# ── 5. xcresulttool 两动词链路（旧 get attachments 已下线 rc=64）────────────
xcrun xcresulttool get test-results tests --format json \
  --path /tmp/probe.xcresult > /tmp/probe-tests.json
rm -rf /tmp/probe-att                                 # 导出目录含 manifest 时工具拒覆盖
xcrun xcresulttool export attachments \
  --path /tmp/probe.xcresult --output-path /tmp/probe-att

# ── 6. os_log：显式 udid + 显式时间窗（可复现），ndjson 才能逐行解析 ─────────
#    工具只吃 '%Y-%m-%d %H:%M:%S' 空格格式（T 分隔 rc=64）；时间窗取 run 的
#    started_at/finished_at（生产适配器自动传 ISO→工具格式的转换）。
xcrun simctl spawn "$PROBE_UDID" log show \
  --style ndjson \
  --start "2026-11-10 21:30:00" --end "2026-11-10 21:40:00" \
  --predicate 'subsystem == "com.thames.alarmkitspike"' \
  > /tmp/probe-oslog.ndjson
#    流尾信封行 {"count":N,"finished":1} 无时间戳，解析器识别后跳过；残缺形态仍 fail-closed。

# ── 7. ffmpeg 抽帧（fps 白名单 1..30 整数，输出固定 frame_%03d.png）─────────
mkdir -p /tmp/probe-frames
ffmpeg -y -i /tmp/probe.mp4 -vf fps=2 /tmp/probe-frames/frame_%03d.png

# ── 8. 拆机：shutdown→delete 专用机，快照 diff 对账用户设备零变化 ───────────
xcrun simctl shutdown "$PROBE_UDID"
xcrun simctl delete "$PROBE_UDID"
xcrun simctl list > /tmp/simlist-after.txt
diff /tmp/simlist-before.txt /tmp/simlist-after.txt   # 期望：无输出（列表还原）
```

与生产代码的对应：argv 不是手拼的，core 纯函数是唯一契约源——`xcode_commands.py`
（xcodebuild/simctl）、`xcresult_tool.py`（两动词）、`oslog_commands.py`
（ndjson/空格时间窗）、`ffmpeg_commands.py`（fps 白名单）；适配器经
`SubprocessProcess` 起进程，普通终止走 SIGTERM、录屏停录走 `interrupt()` 的 SIGINT。
模拟器环境的能力边界（签名/后台音频/真机设备层验不了）在 TestRun `uncovered_layers`
显式声明，页面与报告均如实展示"哪层已验、哪层只能生产验"。

### 3.7 物理真机 .logarchive 两步取证 runbook（2026-11-13 切片一，**手动步骤永不进 verify_s0.sh**）

真机没有 `simctl spawn` 通道，os_log 链路拆成 collect（设备→本机归档）+ show（本机
归档→窗口过滤）两步。环境事实：Xcode 27.0、真机 iPhone 16 Pro（iOS 27.2，硬件 UDID
`00008140-001A39590162801C`）、`/usr/bin/log` 系统自带。**注意双标识陷阱**：
`devicectl list` 顶层 `identifier` 是 CoreDevice UUID（形如 `9D3A93E6-…`），硬件
UDID 在 `properties.hardware.udid`；本序列所有命令只认硬件 UDID。

**安全纪律（先于一切命令）**：

1. 真机不属本进程：禁止任何 erase/reset 自动化（平台层 `IosDeviceEnvironment.reset`
   无条件 PolicyDeniedError）；抹机只能人工经系统设置或 MDM。
2. `log collect` 在部分机器需要 sudo，而 worker 进程没有 TTY——**默认 manual 模式：
   平台不代跑 collect，由人工执行下面的命令预采集**；需要提权时人工自行在命令前加
   `sudo`（交互输入密码），平台侧的 `sudo -n` 仅在凭据已缓存时可用，未缓存立即失败、
   绝不挂死等密码。**本机已实测（2026-11-13，E-A）：免 sudo collect 直接 rc=77
   `log: Must be root to collect logs from attached device`，故本机 manual 是唯一
   可行默认，auto 模式仅在换机器且证实免提权后启用**。
3. 只对显式 UDID 操作，禁止任何隐式"当前设备"写法；本序列不进自动门禁，
   `scripts/device_chain_probe.py` 仅人工显式执行。
4. 真机连接态会实时抖动（实测同一设备两轮探针间 connected→unavailable）：探针/
   health_check 对 unavailable 必须 fail-closed，重连后重跑即可，禁止把失败改写成
   缓存结论。

```bash
set -euo pipefail
DEVICE_UDID=00008140-001A39590162801C          # properties.hardware.udid，非 CoreDevice UUID
RUN_ID=run-$(date +%Y%m%d-%H%M%S)              # 生产环境用 run_id_for(idempotency_key)
ARCHIVE=store/ios-local-poc-device/logarchive-work/${RUN_ID}.logarchive
mkdir -p "$(dirname "$ARCHIVE")"

# ── 0. 预检：盘点必须表态 physical + connected（JSON 走 stdout，core 纯函数同口径解析）──
xcrun devicectl list devices --json-output - \
  | jq '.result.devices[] | {udid:.properties.hardware.udid,
       reality:.properties.hardware.reality, state:.properties.connection.state}'

# ── 1. collect：设备日志 → 本机 .logarchive（注意：collect 没有 --end，
#      采集从 --start 到当前时刻，窗口收口在第 2 步 show）。需要提权就人工加 sudo ──
/usr/bin/log collect --device-udid "$DEVICE_UDID" \
  --start "$(date -v-5M '+%Y-%m-%d %H:%M:%S')" \
  --output "$ARCHIVE"
# 平台 auto 模式等价 argv（use_sudo=true 时前置 sudo -n，非交互失败语义）：
#   sudo -n /usr/bin/log collect --device-udid <UDID> --start "<空格格式>" --output <ARCHIVE>

# ── 2. show：对本机归档做窗口/谓词过滤，ndjson 一行一条（json style 是美化数组不可逐行解析）──
/usr/bin/log show "$ARCHIVE" \
  --style ndjson \
  --start "$(date -v-5M '+%Y-%m-%d %H:%M:%S')" --end "$(date '+%Y-%m-%d %H:%M:%S')" \
  --predicate 'subsystem == "com.thames.alarmkitspike"' \
  > /tmp/${RUN_ID}-device.ndjson
#    流尾信封行 {"count":N,"finished":1} 与模拟器同构，parse_ndjson 识别后跳过；
#    服务端窗外条目再经 filter_window 二次剔除（闭区间，与 log show 语义一致）。
```

manual 模式的人机衔接：平台查询时若归档不在位，`DeviceOsLogQueryAdapter` 以
AdapterUnavailableError 收口，报错原文携带**可照跑的完整 collect 命令与目标归档路径**；
人工采集到同一 `<run_id>.logarchive` 后重跑查询即接上 show，证据链里保留"本应执行的
命令"。一键探针：`.venv/bin/python scripts/device_chain_probe.py`（首跑 exit 2 打印
人工命令，同 `--run-id` 重跑接上 show；`--collect-mode auto` 仅在确认本机免提权时用），
报告落 `out/eval/device-chain-probe.{json,md}`。

与生产代码的对应：argv 全部由 core 纯函数拼装——`devicectl_commands.py`（盘点 argv/
JSON 解析/UDID 白名单）、`oslog_commands.py` 的 `build_device_log_collect_argv` /
`build_archive_log_show_argv`（两步 argv、auto/manual 模式、时间窗与谓词校验）；
进程 IO 在 `adapters/ios_device_env.py`（租约/health_check/reset 守卫）与
`adapters/device_os_log.py`（两步采集、陈旧归档先删后采、NDJSON 复用模拟器同构解析器）；
装配见 `config/profiles/ios-local-poc-device.yaml`（默认 `collect_mode: manual`）。
真机 run 的 TestRun `provider=ios-device`、`uncovered_layers=[]`（三层设备独占能力物理
可验），与模拟器 run 的三层未覆盖声明互不为默认值。

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

## 6. 事件喂料与定时兜底 runbook（S4，2026-11-11）

S4 起保鲜/CI 闭环由事件驱动，进程内**没有定时器**：事件 JSON 先落事件账本再派发，
定时扫描只作为外部 cron 挂接的兜底入口（docs/04 #14：不用定时器扫描冒充事件）。

```bash
# 6.1 事件重放（主路径）：目录里每个 .json 是一个事件信封，坏件进死信不堵好件
.venv/bin/testpilot-ingest --replay-events tests/fixtures/events
# 可选按 source_system 过滤（json-git=CommitPushed，json-ci=PipelineFinished）
.venv/bin/testpilot-ingest --replay-events tests/fixtures/events --event-source json-ci

# 6.2 定时全量兜底（外部 cron 挂这个，不要自己写 while-sleep 轮询）
#     批次 trigger='scheduled' 留痕，与事件驱动的 'event-reindex' 可区分
.venv/bin/testpilot-ingest --scheduled

# 6.3 摄入延迟 P95 日报（只读不入库；无样本段输出 null 而非 0，不放假成绩）
.venv/bin/testpilot-ingest --latency-report /tmp/ingest-latency.json
```

crontab 挂接示例（机器相关路径走 profile/环境变量，不写死在代码里；避开整点拥塞）：

```cron
7 * * * * cd /opt/testpilot/subsystems && TESTPILOT_PROFILE=config/profiles/ios-local-poc.yaml .venv/bin/testpilot-ingest --scheduled >> logs/ingest-scheduled.log 2>&1
13 9 * * * cd /opt/testpilot/subsystems && .venv/bin/testpilot-ingest --latency-report logs/latency-$(date +\%F).json
```

巡检要点：① `ingestion_runs.trigger` 区分 event-reindex / scheduled / 手动扫描；
② 延迟报告门限 P95<300s，任一段样本为空会显式 `gate_passed=false`，需先查事件链路
而不是改阈值；③ 死信在 `dead_letters` 表可查可补偿，修复后重放同 event_id 幂等不重复。
