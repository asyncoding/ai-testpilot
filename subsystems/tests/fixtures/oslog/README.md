# os_log 合成夹具（S3.3 FR-EX-006；2026-11-10 真机探针校准）

全部为**自造合成内容**，不含语料仓 `app-calendar-alarm` 的真实日志原件
（语料仓只读，原件不入库）。行分隔 JSON（NDJSON），对齐
`xcrun simctl spawn <udid> log show --style ndjson` 的真实输出形态
（真机取证：`--style json` 是美化打印的 JSON 数组，无法逐行解析；
时间戳形如 `2026-11-10 21:40:31.506619+0800`，校准记录见
`.agent-handoff/2026-11-10-s3-real-device-probe.md`）。

## window.ndjson

查询窗口 `2026-11-10T10:00:00` ~ `2026-11-10T10:20:00`（闭区间）：

| 行 | 时间戳 | 预期处置 |
|---|---|---|
| 1 | 09:59:00 | 窗外丢弃（dropped_out_of_window） |
| 2 | 10:00:00 | 窗内保留（左边界） |
| 3 | 10:10:00 | 窗内保留（系统侧 subsystem） |
| 4 | 10:20:00 | 窗内保留（右边界） |
| 5 | 10:21:00 | 窗外丢弃 |
| 6 | （坏 JSON） | bad_lines 计数 + 样片留痕，不中断查询 |
| 7 | 无 timestamp | 工具信封尾行 `{"count":N,"finished":1}`，静默跳过不计坏行 |

尾行为真机实测形态：每次 `log show --style ndjson` 流末尾固定一行，
`count` 等于真实条目数；解析器只放行 count+finished 双键完整形态，
缺键的疑似尾行仍按坏行 fail-closed（见 test_os_log_adapter.py）。
