# xcresult 合成夹具（S3.3 FR-EX-005；2026-11-10 真机探针校准）

全部为**自造合成内容**，不含语料仓 `app-calendar-alarm` 的真实 xcresult/日志原件
（语料仓只读，原件不入库；设备名用 `testpilot-probe-synthetic` 自造，非真机 UDID 名）。

字段形态以 **Xcode 27 / xcresulttool 25115（schema 0.4.0）真实探针**为准：
真跑 `AlarmKitSpikeTests/HomeListBuilderTests`（6 用例全绿）+ 官方
`xcresulttool get test-results tests --schema` 与
`export attachments --schema` 输出校准，校准记录见
`.agent-handoff/2026-11-10-s3-real-device-probe.md`。失败用例的 Failure Message
结构来自官方 schema（语料仓只读禁注故障，未经真实失败 run 观测）。

| 文件 | 用途 |
|---|---|
| `test-results.json` | Test Plan→bundle→suite→case 四层真实形态；passed/skipped/failed 三态，duration 字符串 + durationInSeconds 双源，失败用例带 Failure Message 子节点 |
| `attachments.json` | `export attachments` 的 manifest.json 形态（**顶层数组**）：失败用例挂 png+mp4 两附件，testIdentifier 无 bundle 前缀，无 mime 字段 |
| `malformed-test-results.json` | 未知 nodeType，验证解析器 fail-closed 不静默吞 |
