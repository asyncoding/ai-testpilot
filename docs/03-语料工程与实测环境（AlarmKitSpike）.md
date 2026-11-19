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

### 3.8 USB 反向监听单用例执行正式 runbook（ws-run，2026-11-18 转正，模拟器 structural）

S0–S8 在 `subsystems/out/cs-architecture/`（gitignored 批次态）验证的反向监听 C/S 架构
（Runner 设备 loopback WS 服务端 + Mac 驱动客户端 + iproxy）已于 2026-11-18 转正为
workers 包正式产品能力，入口是 console script `testpilot-hybrid ws-run`：**一条命令**
替代批次 runbook 02 的双终端手工流程，自动完成 xctestrun 注入 → driver-first 双进程
编排 → first-exit 对账 → 证据 URN 回填 → 转录落盘。线协议字面量（`tp-hybrid-ws-p0`、
`TP_*` 环境变量、`g-*` 闸门 ID、消息类型）冻结，Swift 侧 TestPilotKit 本期零改动。
批次代码制品（`40-mac-driver/`、`50-xctestrun-injector/`）保持冻结，runbook 02/03 仅
做六处 console-script 文字替换；本 runbook 是唯一的正式版操作序列。

**证据边界（硬纪律，沿用 S8）**：模拟器给不出真实响铃/侧边键/感官，structural 的
g-sensory 是夹具固定值（`AUTO structural fixture` note），**永远不能作为真机证据**；
真机人工验收 S9 已按用户 2026-11-18 指令重启，走转正后的 console script 正式序列
（见 §3.9；2026-11-19 事故后定稿为三终端人工形态，ws-run 单命令仅受支持 structural），
批次 runbook 03 降级为历史叙事。模拟器任何结论不外推真机。

#### 0. 前置与参数（全部参数化，不写机器绝对路径）

```bash
cd subsystems   # 以下命令均以 subsystems/ 为 cwd；解释器一律走 .venv（裸 python3 无 websockets）

# 0.1 当次 Booted UDID 必须实查显式传参，禁止隐式 booted：
#     profile 里的模拟器 UDID（5411787C-…）不一定处于 Booted，S8 机就是另一个 UDID。
xcrun simctl list devices | /usr/bin/grep Booted
export SIM_UDID=<当次 Booted 的 UDID>

# 0.2 build-for-testing 产物在 Xcode 默认 DerivedData（ws-run 一期不做 DerivedData 自动发现）；
#     排除历次注入副本，源 xctestrun 永不原地改。
export SRC_RUN=$(find ~/Library/Developer/Xcode/DerivedData -maxdepth 4 \
  -name 'AlarmKitSpike_iphonesimulator*-arm64.xctestrun' \
  ! -name '*.injected.xctestrun' ! -name 'injected-*' -print | head -n1)
export OID="AlarmKitSpikeUITests/AgentQuickAlarmUITests/test_TCA1_lockRelayFullFlow"

# 0.3 token 走环境变量，绝不进 yaml/转录/截图；不设则回退线协议默认 tok-p0 并 stderr 警告
#     （本地模拟器演示可接受，任何共享/真机场景必须显式设置）。
export TESTPILOT_RELAY_TOKEN=tok-p0
```

#### 1. 绿路：ws-run 单命令（driver-first 由编排器内部保证，retry-300 是硬下限不可调小）

```bash
TESTPILOT_PROFILE=config/profiles/ios-local-poc-xcode.yaml \
.venv/bin/testpilot-hybrid ws-run \
  --gates tests/fixtures/ws-gates/ws-gates.structural.json --structural \
  --xctestrun "$SRC_RUN" \
  --udid "$SIM_UDID" \
  --only-testing "$OID"
# 预期：stderr 先出现驱动 ConnectionRefused 重试行（模拟器 preflight 实测 37–65s），
# Runner listener 起来后立即 connected；三闸自动 approve，g-sensory 落夹具固定值，
# 收 PNG 后 complete；stdout 打印单个 JSON 快照（outcome=passed），exit 0。
```

编排器内部链路（代码事实，排障时对照）：注入副本写源同目录
`<stem>.injected.xctestrun`（DerivedData 内、gitignored，`__TESTROOT__` 同目录约束）→
`spawn_attached` 先起驱动（继承终端 TTY，`--retry-seconds 300`）→ 后起
`xcodebuild test-without-building`（argv 只许来自 core 纯函数
`build_xcodebuild_xctestrun_argv`，含 `-resultBundlePath` 与 600s time-allowance）→
first-exit 对账（驱动先非零退立即杀 xcodebuild；任一先正常退走 30s 有界宽限）→
lsof 端口对账 → 正式转录原子落盘。产物落点（profile `data_dir=store/ios-local-poc-xcode`）：

```
store/ios-local-poc-xcode/ws-relay-sessions/wssess-<UTCstamp>-<hash8>/
├── transcript.json          # 正式账本 ws-gate-transcript-v1（含 localfs URN、进程台账、outcome）
├── driver-transcript.json   # 驱动侧 ws-gate-driver-transcript-v1（四 outcome 原始事实）
└── evidence/                # 回流证据（PNG 等）
store/ios-local-poc-xcode/xcresult-ws/<同 sid>.xcresult
```

绿路五项验收（逐项取证，缺一项不算过）：

1. stdout 快照 `outcome=passed`、进程 exit 0；转录 processes 台账里 xcodebuild rc=0，
   xcodebuild 日志 `Executed 1 test, with 0 failures`；
2. `transcript.json` schema 合法（`ws-gate-transcript-v1`），三闸时序齐全
   g-lock → g-sidebutton → g-sensory；g-sensory values 与夹具逐字一致：
   `ringtone_heard=true / vibration_felt=true / confidence=high`，
   note 为 `AUTO structural fixture (simulator, not human sensory evidence)`；
3. `evidence/` 内 PNG 真实非空（`file` 认 PNG、`stat -f%z` 记字节数，p0 同形态 73KB 量级），
   正式转录证据带 localfs URN（经 LocalFsArtifactAdapter 回填，非裸路径）；
4. `lsof -nP -iTCP:9876 -sTCP:LISTEN` 无残留，且转录 `port_cleanup_verified=true`；
5. 注入副本只在 DerivedData（与源 xctestrun 同目录），仓内 `git status` 零生成物、
   `store/` 与 `out/` 均不入库。

#### 2. 红证：错令牌必须 fail-loud（独立 console script 制造错配，随后强制复绿）

**为什么不能直接用 ws-run 走红证**：ws-run 的 token 是单源的——`resolve_token()`
取到的同一个值同时写进注入 plist 的 `TP_TOKEN` 和驱动 `--token`，结构上无法自构错配
（这是防呆属性，不是缺口）。错配要用两个独立 console script 手工制造，等价复现 S8
红证；ws-run 自身的 fail-loud 分型（驱动 rc=2 → `protocol_error` → exit 2）由
`tests/test_ws_relay_session.py` 全假件红证钉住，不依赖真机/模拟器。

```bash
# 2.1 注入 9877 独占端口副本：Runner plist 内 TP_TOKEN 仍是缺省 tok-p0
.venv/bin/testpilot-inject-xctestrun \
  --input "$SRC_RUN" \
  --output "$(dirname "$SRC_RUN")/injected-wsrun-nodriver.xctestrun" \
  --port 9877 --env TP_TCA1_STRUCTURAL=1
```

```bash
# 终端 1：持错令牌的驱动连 9877（鉴权主体是驱动：它校验入站 hello 的 token）
.venv/bin/testpilot-ws-gate-driver \
  --gates tests/fixtures/ws-gates/ws-gates.structural.json \
  --structural --token WRONG-TOKEN \
  --url ws://127.0.0.1:9877/ws/hybrid/hsess-p0 \
  --evidence-dir /tmp/tp-wsrun-red/evidence \
  --transcript-out /tmp/tp-wsrun-red/driver-transcript.json \
  --retry-seconds 300 ; echo "driver rc=$?"
# 预期：WS 能连上，Runner 主动发 hello（token=注入真值 tok-p0），驱动校验不匹配 →
# 回 fail 帧并 close 1008、rc=2；转录 outcome=fail_loud；hello 阶段早夭，
# /tmp/tp-wsrun-red/evidence 目录不生成属预期（不是缺陷）。
```

```bash
# 终端 2：同一 9877 副本跑 OID（这是预期红，不是产品缺陷）
xcodebuild test-without-building \
  -xctestrun "$(dirname "$SRC_RUN")/injected-wsrun-nodriver.xctestrun" \
  -destination "platform=iOS Simulator,id=$SIM_UDID" \
  "-only-testing:$OID" \
  -maximum-test-execution-time-allowance 600
# 预期：Runner awaitMessage(hello_ack) 收到 fail 帧 → TPChannelError.serverFail
# （reason 逐字 "auth rejected, hello={…真 token…}"），测试 failed，
# xcodebuild 打印 ** TEST EXECUTE FAILED **（S8 实测 10.142s）。
```

红证取证后**必须原样重跑 §1 绿路**确认 exit 0 / outcome=passed，零 TEMP-RED 残留
（复演 S8 纪律；红证副本与 /tmp 转录随 DerivedData/tmp 自然清理，不入库）。

#### 3. 退出码、失败分型与已知 flake

| 退出码 | 含义 | 转录 outcome |
| --- | --- | --- |
| 0 | 全绿 | `passed` |
| 2 | 硬错误：注入失败/工具链缺失/重试窗耗尽/驱动 fail-loud（rc=2）/证据应到未到/现场矛盾 | `protocol_error` 或 `hard_error` |
| 3 | 产品侧响亮失败：xcodebuild 非零且有产物，**含人工 reject**；快照照打、转录已落盘 | `product_failure` |

reject 链路：Swift `XCTAssertEqual(verdict, "approve")` 抛错 → XCTest 拆连接 →
驱动收到 peer closed（rc=1），ws-run 据驱动转录的 rejected 留痕分型 `product_failure`
exit 3——**reject 在任何链路下都不存在自动冲绿路径**（structural 模式全程自动裁决，
reject 只可能发生在人工闸门形态）。完整四 outcome 映射与 B1 账本边界见 docs/15 §8.5。

| 现象 | 判定 | 处置 |
| --- | --- | --- |
| `App event loop idle notification not received`，随后驱动 `keepalive ping timeout`（rc=1） | 模拟器 XCTest 调度层 flake，S4 四跑对照已证与 TP_ 变量无因果 | **先原样重跑**，不查产品代码；连续两次同签名才升级排查并记录，不得充产品缺陷 |
| 驱动长时间刷 `ConnectionRefused` | preflight 期 Runner 未起监听；retry-300 内连上即正常（p0 真机实测最长 53s） | 等；超过 300s 才查注入位置 |
| `nw_listener_socket ... SO_NECP_LISTENUUID failed [2]` | 模拟器良性警告，p0/S2–S4 每次都出现 | 忽略 |

#### 4. 边界与红线（一期定案，二期清单见 docs/12 E6 / docs/15 §8.5）

- 一期是**单用例形态**：一次调用一条 OID、单会话 complete 即收口；一条 action 串行
  多用例的多会话循环列二期靠前。`--target device` 在本模拟器 profile 下仍直接硬错误
  （`usb_relay.iproxy.enabled=false`，文案指向 §3.9）：模拟器共享 Mac loopback 本就
  不需要 iproxy；真机 profile `ios-local-poc-device.yaml` 已为 S9 启用
  iproxy（`enabled: true`、900s 人工窗口），正式验收序列见 §3.9。
- 账本走 B1（本地转录 JSON + localfs URN），**不写 FileGateStore、不碰 test_runs 表**，
  转录 `test_run_ref` 恒 null；web 交互页/WS↔gate store 桥接整体留二期。
- `retry_seconds` 300s 硬下限（profile 调小即启动报错）；token 只走
  `TESTPILOT_RELAY_TOKEN` 环境变量；`websockets>=14` 是 workers 包显式依赖
  （S8 裸 python3 ModuleNotFoundError 事故的根治措施）。
- 配套文档：docs/12 §E6（argv 契约与转正边界）、docs/15 §8.2/§8.5（ws-run 走查与
  B1 账本）、docs/09（反向监听/闸门驱动/iproxy 五术语）、subsystems/README
  「USB 反向监听单用例执行」；批次原貌留 runbook 02（S8 双终端叙事，已换 console script）。

### 3.9 USB 反向监听真机人工验收正式 runbook（三终端 console script 形态，S9，2026-11-19 重启/同日修订）

用户 2026-11-18 指令：**S9 直接用转正流程验收，不再用 `out/` 批次三终端**。本节是 S9
唯一正式操作序列；`subsystems/out/cs-architecture/tca1-carryin/60-runbook/03-device-acceptance-runbook.md`
降级为 P0–S8 批次历史叙事（四取证点原文保留备查）。「转正流程」指 workers 包的正式
console script（`testpilot-ws-gate-driver` / `testpilot-inject-xctestrun`，随
commit 76f5e9f 入产品包），不是 `out/` 下的批次 `.py`；人工 TTY 下这些入口的受支持
编排方式是本节的**三终端形态**。与 §3.8 的分工：§3.8 是模拟器 structural 单命令
（ws-run，唯一可全自动形态），本节是真机人工 gates——**必须在真实终端 TTY 里跑**
（worker 等无 TTY 环境 `input()` 物理不成立）。模拟器任何结论不外推真机，
侧边键/真实响铃/震动/锁屏保活只认本节四取证点。

> **2026-11-19 修订（事故实证，带时间戳保留）**：本节初版曾写「ws-run 单命令内化
> 人工 gates」，当日两次真机试跑证伪——`wssess-20261119T015207Z-10c331b3`（OID 未设
> 人工 Ctrl-C，xcresult `Testing was canceled`）与 `wssess-20261119T015245Z-10c331b3`
> （完整重跑 736.5s，xcresult `等待超时：gate_result 120.0s`，失败在第一道闸 g-lock）。
> 根因：`spawn_attached` 给驱动继承了 stdin 但 stdout/stderr 走 PIPE，`wait()` 调
> `communicate()` 排空即弃，`[闸门 g-lock]` 提示语永远到不了终端，人工无法作答
> （`process_executor.py` 该函数 docstring 转正时即写死：全 TTY 人工会话应在独立终端
> 运行 testpilot-ws-gate-driver）。协议/iproxy/设备无故障（能到 g-lock 说明
> hello 握手已成功）。**ws-run 单命令人工形态不再作为受支持路径**；产品化修复
> （驱动 stdout 转发/继承 + hermetic 测试，恢复单命令与正式 B1 账本）列二期 backlog，
> 需用户显式批准。两次红跑 xcresult 与会话目录保留不销毁。

> **设备态（2026-11-19 本会话复查）**：`xcrun devicectl list devices` 表态
> iPhone 16 Pro（硬件 UDID `00008140-001A39590162801C`）physical + **connected**
> （同机 iproxy 2.1.1 在位）。连接态会实时抖动，每次开跑前实查；回到 unavailable
> 即 fail-closed 停跑，**禁止把失败改写成缓存结论**。

#### 0. 开工 checklist（逐项打勾）

```bash
cd subsystems   # 以下命令均以 subsystems/ 为 cwd；解释器一律走 .venv（裸 python3 无 websockets）

# 0.1 真机盘点：必须表态 physical + connected，取 properties.hardware.udid（不是顶层 CoreDevice UUID）
#     jq 路径与 §3.7、core devicectl 纯函数解析口径一致，换机以本机实查为准
xcrun devicectl list devices --json-output - \
  | jq '.result.devices[] | {udid:.properties.hardware.udid,
       reality:.properties.hardware.reality, state:.properties.connection.state}'
export DEVICE_UDID=00008140-001A39590162801C

# 0.2 iproxy 存在性（本形态由终端 1 手工前台常驻；ws-run 自动 spawn 仅 structural 单命令形态使用）
which iproxy && iproxy --version    # P0 机实测 2.1.1；缺失 brew install libimobiledevice

# 0.3 真机 build-for-testing 产物（iphoneos 形态；排除历次注入副本，源 xctestrun 永不原地改）
export DEVICE_RUN=$(find ~/Library/Developer/Xcode/DerivedData -maxdepth 4 \
  -name 'AlarmKitSpike_iphoneos*-arm64.xctestrun' \
  ! -name '*.injected.xctestrun' ! -name 'injected-*' -print | head -n1)
export OID="AlarmKitSpikeUITests/AgentQuickAlarmUITests/test_TCA1_lockRelayFullFlow"

# 0.4 token 走环境变量，绝不进 yaml/转录/截图；不设则回退线协议默认 tok-p0 并 stderr 警告
export TESTPILOT_RELAY_TOKEN=tok-p0
```

人工项（不可脚本化）：① 手机未锁屏（preflight 遇锁屏报 `Unlock iPhone to Continue`
原地等待，解锁自动续跑）；② **取证点 A 基线**——设置 → 隐私与安全性 → 本地网络，
截图记录列表当前内容（P0 基线为完全为空）；③ §3 取证点 B 的黑帧量化脚本就位。

#### 1. 真机 build-for-testing（一次性手工前置；ws-run 一期不做 DerivedData 自动发现）

```bash
cd "$TESTPILOT_REPO_PATH/AlarmKitSpike"   # 语料仓在另一个仓库，路径走环境变量，禁止写死
xcodegen generate && pod install
xcodebuild build-for-testing -workspace AlarmKitSpike.xcworkspace \
  -scheme AlarmKitSpike \
  -destination "platform=iOS,id=$DEVICE_UDID" 2>&1 | tee /tmp/tp-s9-build.log
# 验收：** BUILD SUCCEEDED **；未传 -derivedDataPath，产物在 Xcode 默认 DerivedData
# （~/Library/Developer/Xcode/DerivedData/AlarmKitSpike-*/Build/Products/，iphoneos 形态）。
```

FV1 双字典（`EnvironmentVariables` + `TestingEnvironmentVariables`）注入在 §2
终端 3 用正式 console script `testpilot-inject-xctestrun` 手工执行；注入器打印
「注入点 2 个」才合格（真机双字典必要性的实测点保留在终端回显里），并以
PlistBuddy 复核 `TP_TCA1_STRUCTURAL` 为 Does Not Exist。本 runbook 不带
`--structural`，即不注入该键，走真机完整 oracle[12] 断言链。

#### 2. 绿路：三终端正式序列（iproxy → 交互驱动 → 注入+xcodebuild，全部走产品 console script）

> 为什么不是 ws-run 单命令：见本节开头 2026-11-19 事故注记。ws-run 的
> `spawn_attached` 把驱动 stdout/stderr 接 PIPE 后排空丢弃，人工提示不可见；
> ws-run 单命令的受支持形态只有 `--structural`（零提问，模拟器演示/CI，见 §3.8）。
> 人工 gates 的受支持形态是独立 TTY 里的 `testpilot-ws-gate-driver`——它与
> `testpilot-inject-xctestrun` 都是转正后的正式 console script，本序列不调用
> 任何 `out/` 批次脚本。

**公共参数（三个终端各 export 一次；cwd 均为 `subsystems/`）**

```bash
cd subsystems   # 解释器一律走 .venv（裸 python3 无 websockets）
export DEVICE_UDID=00008140-001A39590162801C
export OID="AlarmKitSpikeUITests/AgentQuickAlarmUITests/test_TCA1_lockRelayFullFlow"
# token 走环境变量，绝不进 yaml/转录/截图；本地缺省回退线协议默认 tok-p0 并 stderr 警告
export TESTPILOT_RELAY_TOKEN=tok-p0
# 真机 build-for-testing 产物（iphoneos 形态；排除历次注入副本，源 xctestrun 永不原地改）
export DEVICE_RUN=$(find ~/Library/Developer/Xcode/DerivedData -maxdepth 4 \
  -name 'AlarmKitSpike_iphoneos*-arm64.xctestrun' \
  ! -name '*.injected.xctestrun' ! -name 'injected-*' -print | head -n1)
# 手工会话目录：与 ws-run 的 wssess-* 同位但 manual- 前缀，账本边界一眼可辨
export S9_DIR="$PWD/store/ios-local-poc-device/ws-relay-sessions/manual-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$S9_DIR/evidence" "$PWD/store/ios-local-poc-device/xcresult-manual"
echo "$S9_DIR"   # 记录下来，§3 取证点 B/D 复用
# 注意：三个终端必须用同一个 S9_DIR 值（手工照抄 echo 结果，勿各自重跑本块，
# 否则转录/证据/xcresult 会散落到不同 manual-* 目录，2026-11-19 已踩过一次）。
```

**终端 1：USB 端口转发，前台常驻**

```bash
iproxy 9876 9876    # usbmuxd 把 Mac 127.0.0.1:9876 转进设备同端口；iproxy 2.1.1，P0 已验
# 跑毕人工 Ctrl-C 收口。预检（可选，另开终端）：Runner 起监听前 nc -v 127.0.0.1 9876
# 应 connection refused，证明转发链路在等设备侧。
```

**终端 2：Mac 交互驱动（driver-first：先于 xcodebuild 启动，不要加 --structural）**

```bash
.venv/bin/testpilot-ws-gate-driver \
  --gates tests/fixtures/ws-gates/ws-gates.human.json \
  --url ws://127.0.0.1:9876/ws/hybrid/hsess-p0 \
  --token "$TESTPILOT_RELAY_TOKEN" \
  --evidence-dir "$S9_DIR/evidence" \
  --transcript-out "$S9_DIR/driver-transcript.json" \
  --retry-seconds 300 2>&1 | tee /tmp/tp-s9-driver.log
# 先刷 ConnectionRefused/InvalidMessage 重试是预期（P0 实测最长 53s；retry-300 硬下限不许调小），
# Runner listener 起来后立即 connected。三闸交互：acknowledge 输 y/n；g-sensory 逐字段录入
# 布尔/confidence(high|medium|low)/note（可空回车）；任意提示处输 /reject 可整闸退回。
```

闸门时间线（与 Swift 方法内顺序严格对应，executionTimeAllowance=900）：
1. **g-lock（120s）——顺序是硬规则：锁屏观察 → 先解锁 → 再输 y**。提示出现后在手机上
   按电源键真锁屏，保持锁屏观察一段（通道在锁屏下仍可达，2026-11-19 实测锁屏态
   gate_result/heartbeat 正常穿链），**然后必须先解锁手机回到主屏/App，最后才在终端
   输 `y`**。原因：y 之后 Swift 立即执行 Step 4 `XCUIDevice.shared.press(.home)` +
   `app.activate()`（QuickAlarmAgentUITests.swift:90），若设备仍锁屏，SpringBoard
   以 `SBMainWorkspace reason=Locked ("Unable to launch … because the device was
   not … unlocked")` 拒绝激活，测试当场抛错、Runner 拆连接，驱动收 peer_closed
   （2026-11-19 红跑 manual-20261119T022951Z.xcresult 在案，见 §5 故障表）。
   g-lock 窗口有 120s，解锁后再输 y 时间充裕，严禁锁屏态回 y；
2. Swift 自测 `press(.home)` + `app.activate()` 保活；120s 响铃窗口内闹钟触发，
   **人工按真实硬件侧边键**处置（取证点 C，禁止软件事件冒充）；
3. **g-sidebutton（240s）**：确认侧边键动作与响铃/静音表现，输 `y`；
4. **g-sensory（240s）**：如实录听感/触感/置信度；silence.caf 漂移（见取证点 C）下
   若确实没听到铃声，ringtone_heard 填 false，不许凑绿；
5. 驱动收 PNG 证据后 complete、rc=0；Swift 继续 oracle[12] 接力页断言与 finish。

**终端 3：注入 + 真机执行（驱动已在等，Runner 一起来就被接住）**

```bash
# 3.1 注入：同目录副本，源 xctestrun 永不原地改；验收打印「注入点 2 个」
.venv/bin/testpilot-inject-xctestrun \
  --input "$DEVICE_RUN" \
  --output "$(dirname "$DEVICE_RUN")/injected-s9-manual.xctestrun"
# 3.2 防误用复核：两个字典都应 Does Not Exist
/usr/libexec/PlistBuddy -c \
  "Print :AlarmKitSpikeUITests:EnvironmentVariables:TP_TCA1_STRUCTURAL" \
  "$(dirname "$DEVICE_RUN")/injected-s9-manual.xctestrun" 2>&1
# 3.3 执行（resultBundlePath 父目录已在公共参数建好；-path 类工具后续取摘要必须绝对路径）
xcodebuild test-without-building \
  -xctestrun "$(dirname "$DEVICE_RUN")/injected-s9-manual.xctestrun" \
  -destination "platform=iOS,id=$DEVICE_UDID" \
  "-only-testing:$OID" \
  -maximum-test-execution-time-allowance 900 \
  -resultBundlePath "$PWD/store/ios-local-poc-device/xcresult-manual/$(basename "$S9_DIR").xcresult" \
  2>&1 | tee /tmp/tp-s9-device.log
# 验收：** TEST SUCCEEDED **，日志 Executed 1 test, with 0 failures
```

产物落点（device profile `data_dir=store/ios-local-poc-device`，与模拟器 profile 物理隔离）：

```
store/ios-local-poc-device/ws-relay-sessions/manual-<UTCstamp>/
├── driver-transcript.json   # 驱动侧 ws-gate-driver-transcript-v1（四 outcome 原始事实，不含 token）
└── evidence/                # 回流证据（PNG 等，驱动直接落盘）
store/ios-local-poc-device/xcresult-manual/manual-<UTCstamp>.xcresult
```

**账本边界（诚实声明）**：手工三终端形态**只有驱动侧转录**，没有 ws-run 编排器写的
正式 `ws-gate-transcript-v1`（localfs URN 回填、iproxy/xcodebuild 进程台账、
`port_cleanup_verified`、stdout JSON 快照均不存在），证据 PNG 是裸路径而非
`urn:testpilot:localfs:` URN。二期 stdout 转发切片落地前，S9 报告必须注明
「人工三终端形态，B1 正式账本缺口」；这不影响四取证点的原始证据效力，但不得
冒充 ws-run 正式账本。

绿路五项验收（逐项取证，缺一项不算过）：

1. 终端 2 驱动 rc=0、终端 3 xcodebuild `Executed 1 test, with 0 failures`
   （** TEST SUCCEEDED **）；
2. `driver-transcript.json` schema 合法（`ws-gate-driver-transcript-v1`），三闸时序齐全
   g-lock → g-sidebutton → g-sensory；**g-sensory values 必须是人工录入值**——
   若出现 `AUTO structural fixture` note 即误用 structural，本次验收作废重跑；
3. `evidence/` 内 PNG 真实非空（`file` 认 PNG、`stat -f%z` 记字节数）；
4. 终端 1 Ctrl-C 后 `lsof -nP -iTCP:9876 -sTCP:LISTEN` 无残留（手工形态人工取证，
   无编排器自动台账）；
5. 注入副本只在 DerivedData（与源 xctestrun 同目录），仓内 `git status` 零生成物、
   `store/` 与 `out/` 均不入库。

#### 3. 四个 DEVICE-VERIFY 取证点（从批次 runbook 03 迁移，证据路径换正式产物落点）

方法内时序（time-allowance=900）：通道建立 → 排钟行可见 → **g-lock** →
home/activate 保活 → 120s 响铃窗口 → **g-sidebutton** → **g-sensory** →
oracle[12] 接力页断言 → 证据回流 → finish。

**取证点 A：loopback 零授权（设置普查 + 同进程对照日志）**

- 全程观察：手机**不应出现**任何「本地网络」权限弹窗；跑完回 设置 → 隐私与安全性 →
  本地网络，截图与 §0 基线对照：**无新增条目**（P0 2026-11-18 实测列表完全为空）；
- xcodebuild 日志若出现 `NWBrowser DNSServiceBrowse failed: NoAuth(-65555)` 是
  **预期且无害**（Bonjour 探针被本地网络策略无弹窗拒绝），与同窗口
  `WS server established on 127.0.0.1:9876` 构成 lo0 豁免 vs LAN 拦截的同进程对照，
  一并贴入验收报告。

**取证点 B：真锁屏保活 + 黑帧量化（2026-11-19 绿路后修订口径）**

- g-lock 提示出现后人工按电源键锁屏，保持锁屏观察一段后**先解锁再回 y**（顺序是
  硬规则，理由见 §2 时间线 1 与 §5 故障表）；锁屏期间通道必须不断——
  `$S9_DIR/driver-transcript.json` 的 gate/heartbeat 时序无断连重连。**协议层旁证**：
  2026-11-19 第三次红跑中 gate_result(approve) 与 heartbeat 是在设备仍锁屏时成功穿链
  的（随后的失败发生在 activate 而非通道），直接证明 NWListener 锁屏存活；
- **黑帧来源澄清（纠正批次 runbook 与本节初版的错误假设）**：用例唯一的回流截图
  是 Swift Step 9 `sendEvidence` 在接力页完成后采的（QuickAlarmAgentUITests.swift:155），
  **不可能是锁屏帧**——2026-11-19 绿路实测该 PNG avgRGBA=[244,239,245,255]（亮屏）。
  锁屏黑帧的量化证据沿用 **P0 五跑专采**（SpringBoard/devicectl 锁屏窗口手工采屏，
  avgRGBA=[0,0,0,255]，亮屏对照 [254,254,254,…]，见 P0 收口 handoff）；S9 若要补
  当次黑帧，必须在锁屏窗口内**另开终端手工采屏**（`xcrun devicectl device screenshot`），
  不要把 Step 9 亮屏帧当黑帧上报。量化工具（系统自带 swift 工具链，无新依赖）：

```bash
cat > /tmp/tp_avg_rgba.swift <<'EOF'
import CoreImage
let url = URL(fileURLWithPath: CommandLine.arguments[1])
guard let img = CIImage(contentsOf: url) else { fatalError("not an image: \(url)") }
let f = CIFilter(name: "CIAreaAverage", parameters: [
    kCIInputImageKey: img, kCIInputExtentKey: CIVector(cgRect: img.extent)])!
var bitmap = [UInt8](repeating: 0, count: 4)
CIContext().render(f.outputImage!, toBitmap: &bitmap, rowBytes: 4,
    bounds: CGRect(x: 0, y: 0, width: 1, height: 1),
    format: .RGBA8, colorSpace: CGColorSpaceCreateDeviceRGB())
print("avgRGBA=\(bitmap)")
EOF
# ① Step 9 回流帧（亮屏对照，期望高值；2026-11-19 实测 [244,239,245,255]）
swift /tmp/tp_avg_rgba.swift "$S9_DIR/evidence/evidence-01-screenshot.png"
# ② 锁屏窗口手工采屏帧（期望 avgRGBA=[0,0,0,255]；文件以实际 devicectl screenshot 输出为准）
swift /tmp/tp_avg_rgba.swift /tmp/tp-s9-lockframe.png
```

**取证点 C：侧边键 + 感官裁决 + os_log 两步法事件链**

- 120s 响铃窗口内闹钟触发后，人工执行**真实硬件侧边键**处置（不得用软件事件冒充，
  两类事件 os_log 因果链不同，docs/12、docs/14 S-5）；在 g-sidebutton 提示处 y 确认；
- g-sensory 如实录入听感/触感与置信度：**人判与 os_log 客观证据冲突时不静默覆盖**
  （两份结论并排留痕，转人工复核）；
- os_log 两步法（§3.7 逐字形态；本机实测 `log collect` 需 sudo，**纯人工执行，
  自动化永不代跑 collect**，worker 无 TTY）：

```bash
set -euo pipefail
RUN_ID=run-$(date +%Y%m%d-%H%M%S)
ARCHIVE=/tmp/${RUN_ID}.logarchive
# ① collect：设备 → 本机归档（需要提权就人工前置 sudo；collect 没有 --end，
#    采集从 --start 到当前时刻，窗口收口在第 ② 步 show）
sudo /usr/bin/log collect --device-udid "$DEVICE_UDID" \
  --start "$(date -v-10M '+%Y-%m-%d %H:%M:%S')" \
  --output "$ARCHIVE"
# ② show：本机归档谓词过滤，ndjson 一行一条
/usr/bin/log show "$ARCHIVE" --style ndjson \
  --start "$(date -v-10M '+%Y-%m-%d %H:%M:%S')" --end "$(date '+%Y-%m-%d %H:%M:%S')" \
  --predicate 'subsystem == "com.thames.alarmkitspike"' \
  > /tmp/${RUN_ID}-device.ndjson
```

事件链验收（TC-A1 oracle 期望文案，按时间序 grep）：

| 阶段 | 期望事件（message 子串） |
| --- | --- |
| T0 排钟武装 | `KEEPALIVE armed (silent mixable loop)`；接管后 `KEEPALIVE handed off live audio session (kept active)` |
| 系统闹钟投递 | `SYS-LA alert delivery ... appState=2` 与 `TAKEOVER [sys-la] ... appState=2` |
| 自有铃声启动 | `RINGER start attempt=1 ... sound=<实测值> appState=2` |
| 静音门建立 | `RINGER gate armed: loop muted until the system alarm leaves .alerting` |
| 侧边键后 | `RINGER gate unmuted (state left .alerting): our loop continues alone` |
| 硬顶（正常流程不得触发） | `RINGER gate unmuted (120s hard cap)` |

> **2026-11-19 manual-rerun-1 实测偏离（开放项，未冲绿）**：侧边键处置在闸内确认
> （g-sidebutton 11:13:32 approve），但全 76 行设备 ndjson 中 `state left .alerting`
> 计数为 0，硬顶在 gate armed 后 120.76s 实际触发（11:14:08.529）——期望行 MISSING、
> 「不得触发」行 PRESENT。UI 测试仍 passed（硬顶是内建安全网，接力页断言不受影响），
> 取证点 C 按**条件通过**签收。候选根因 H1=iOS 26 AlarmKit 音量键仅静音不移除 alert
> 呈现态、H2=操作偏差且 acknowledge 闸表不记录按键类型；受控重跑方案与 AR 去向见
> `out/cs-architecture/tca1-carryin/20-generated-test/generation-report.md` §7 取证点 C
> 与 §8 第 5 条。重跑前本表期望文案保留不改（历史 oracle 不篡改）。

**silence.caf 漂移注记（诚实边界）**：quick 测试钩子现码铃音为 `silence.caf`
（App.swift DEBUG 钩子），V1 文档/oracle[6] 的 GoodMorning.m4a 描述在 quick 夹具下
失效。RINGER 行的 `sound=` 以**真机实测值**为准记录，若为 silence.caf 则感官裁决
ringtone_heard 可能为 false——如实填，不改 oracle 文本凑绿，在报告漂移段注明。

**取证点 D：oracle[12] 接力页（解锁后直接接管 + 点完成全停）**

- 解锁进 App 必须直接是接力页：断言 `Kill/Launch Test` 标题与 `完成计时，关闭闹钟`
  主按钮存在且 isHittable（AR-003：fullScreenCover 下底层首页标题仍在 AX 树，
  不断言首页标题不存在）；点完成后接力页消失、回首页 `重要提醒`
  （Debug 假插屏 `跳过` 按钮 3s 超时即放行）；
- 附件在正式 xcresult 内（手工形态落点 `xcresult-manual/`，不是 ws-run 的 `xcresult-ws/`）：
  `xcrun xcresulttool get test-results summary --path /绝对路径/store/ios-local-poc-device/xcresult-manual/$(basename "$S9_DIR").xcresult`
  （`--path` 必须绝对路径，相对路径会报 File or directory doesn't exist）；
- 标签实测漂移：文案与规格不符测试会失败——**保留 xcresult 与失败日志，禁止改断言**，
  在报告登记实测标签走 AR 流程（docs/12 附录 C）。

#### 4. 退出码、reject 与收口纪律

手工三终端形态没有 ws-run 的 0/2/3 聚合退出码，按两个进程分别判读（驱动侧四
outcome 契约不变，见 §3.8 §3 与 docs/15 §8.5）：

- **驱动 `testpilot-ws-gate-driver`**：`complete`(rc0) 全绿；`peer_closed`(rc1)
  覆盖 reject 链——Swift `XCTAssertEqual(verdict, "approve")` 抛错 → XCTest 拆
  NWConnection → 驱动收连接关闭并落带 rejected 闸门的转录；`fail_loud`/
  `connect_timeout`(rc2) 协议/连接硬错误；
- **xcodebuild**：自身 rc 与 xcresult 为准，reject 时用例 Failed、
  `等待超时：gate_result` 或 XCTAssert 失败进 failureText。
- **任何链路下都不存在自动冲绿路径**；reject 早夭本就无 evidence 目录，属预期不是
  缺陷。ws-run 的 0/2/3 聚合分型（含 product_failure exit 3）只在 §3.8
  structural 单命令形态生效，二期 stdout 转发切片后人工形态才恢复该收口。

错令牌红证**不在 S9 真机重复**：fail-loud 已由 §3.8 §2 模拟器错令牌红证
+ `test_ws_relay_session.py` 32 条 hermetic 红证钉住；真机只跑人工绿路，除非用户
显式要求追加红证（手工形态可用独立 `--token WRONG` 终端自构错配）。失败处置：
保留完整 xcresult、驱动转录与 xcodebuild 日志，按 AR 流程收口，**不允许重跑冲绿后
销毁红跑证据**（2026-11-19 两次红跑即按此保留）。全程不 commit/push；语料仓改动
仍受白名单约束，TestPilotKit Swift 零改动。

#### 5. 故障速查（P0/S2–S4 真机踩坑档案）

| 现象 | 根因/处置 |
| --- | --- |
| （2026-11-19 绿路实证）测试已 passed，终端却出现 `Password:` 且 xcodebuild 不退出，约 10 分钟后才打 `Failure collecting diagnostics from devices: Timed out after 600.0 seconds` 然后 `** TEST EXECUTE SUCCEEDED **` | CoreDevice 测试后自动起 `devicectl diagnose --timeout 600` 打包设备诊断，内部 `sudo -- /usr/bin/true` 提权要 Mac 密码（进程树可见，状态 T 为终端读密码挂起）。诊断 zip **不是四取证点所需证据**：不输密码等满 600s 超时即可，xcodebuild 自行有序收口、xcresult 完整可读（2026-11-19 实测 result=Passed）；也可输 Mac 密码让其采完提前退出。**勿在超时前 Ctrl-C**：bundle 此刻只有 Data/Staging 无 Info.plist，强杀会留下不可读的半成品 |
| （2026-11-19 实证）g-lock 回 y 后约 1 秒驱动 `PEER CLOSED before complete`，xcodebuild 报 `Failed to launch … reason: Locked`，随后卡 `Unlock iPhone to Continue` 预检 | 锁屏态回了 y：Swift Step 4 `app.activate()` 被 SpringBoard 以 SBMainWorkspace Locked 拒绝（Swift:90），测试抛错拆连接。**处置：g-lock 必须先解锁再输 y**；夹具 prompt 已加硬规则。通道本身无故障（锁屏下 gate_result/heartbeat 仍穿链，恰是取证点 B 的旁证）。xcodebuild 预检会等人解锁后重启 Runner 但失败用例不重跑，再卡 600s 诊断超时才退出 |
| （2026-11-19 实证）ws-run 单命令人工 gates 终端无任何闸门提示，xcresult `等待超时：gate_result 120.0s` | `spawn_attached` 驱动 stdout/stderr 走 PIPE 被 `communicate()` 排空丢弃，提示语不可见；**ws-run 人工形态不受支持**，改本节 §2 三终端形态（独立 TTY 跑 testpilot-ws-gate-driver），勿再单命令硬跑 |
| 驱动持续 `InvalidMessage: did not receive a valid HTTP response` | 设备侧无监听时 iproxy 提前 accept 后 usbmux 零字节断开；1s 重试兜住，P0 实测最长 53s，超 retry-300 才排查注入位置 |
| 驱动 `keepalive ping timeout` exit 1（非 fail-loud 的 2） | 对端 30s 无响应（websockets ping 20/timeout 10）；真机先排查 Runner 是否被系统挂起，保留日志，不得直接充产品缺陷 |
| preflight `Unlock iPhone to Continue` | 真机锁屏启动测试的预期等待，解锁自动续跑 |
| `devicectl` 表态 unavailable / ws-run 起 iproxy 后连不上 | USB 物理连接/信任态丢失；fail-closed 重插重连后重跑，禁止缓存结论 |
| `log collect` rc=77 `Must be root` | 本机实测必须人工 sudo collect，无 TTY 自动化不要尝试 |
| 安装报 0xe8008018 签名错 | Runner 未被开发证书重签（Team 未选）；核对两个 target 的 DEVELOPMENT_TEAM |
| 黑帧量化得到非全黑值 | 先确认截图时刻仍在锁屏窗口；亮屏帧本就不该全黑，对照时间线重采，不要改阈值 |

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
