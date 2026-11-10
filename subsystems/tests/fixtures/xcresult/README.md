# xcresult 合成夹具（S3.3 FR-EX-005）

全部为**自造合成内容**，不含语料仓 `app-calendar-alarm` 的真实 xcresult/日志原件
（语料仓只读，原件不入库）。字段形态按 Xcode 16
`xcrun xcresulttool get test-results tests/attachments --format json` 文档形态捏造，
真实字段以手动探针实测校准（见 `.agent-handoff/2026-11-10-s3.3-xcresult-oslog.md`）。

| 文件 | 用途 |
|---|---|
| `test-results.json` | passed/skipped/failed 三态用例树，失败用例带 failureText |
| `attachments.json` | 失败用例挂两附件（png 截图 + mp4 录屏），case_ref 与树的 external_id 同源 |
| `malformed-test-results.json` | 未知 nodeType，验证解析器 fail-closed 不静默吞 |
