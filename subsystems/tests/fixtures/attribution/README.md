# 失败归因盲标夹具（S3.4 FR-EX-007）

全部为**自造合成内容**，不含语料仓 `app-calendar-alarm` 的真实代码/日志/事故文档
（语料仓只读，原件不入库）。历史案例只取"模式"：90s→30s 期望过期、授权竞态、
模拟器不暴露真机行为、flaky 时过时不过（docs/08 FR-EX-007 AC1 四原型），
符号名、消息文本、commit sha、run id 均为捏造。

## 文件

| 文件 | 用途 |
|---|---|
| `labeled_cases.json` | 40 例专家盲标清单（LabeledManifest），每例含 ground_truth、archetype 与完整 FailureEvidence（含证据账本 ledger） |

## 分布与门禁余量

| ground_truth | 数量 | archetype |
|---|---|---|
| real_defect | 20 | diff 堆栈命中 12（含 2 例环境噪声诱饵）+ 同代码重复复现 8（含 2 例日志噪声诱饵） |
| flaky | 5 | fail→pass / 双 fail→pass / pass→fail / 环境噪声诱饵 / error→pass |
| case_outdated | 4 | 90→30 原型 + 常量/布尔/环境噪声诱饵 |
| environment | 4 | 授权竞态×2（原型，1 例带同族 os_log）+ 真机能力缺失原型 + 双信号 |
| product_change | 4 | 断言失配 + 覆盖该符号的变更记录 CR-101..104 |
| needs_human | 3 | 薄证据 / flaky↔缺陷同置信冲突 / 复现缺陷↔环境顶格护栏冲突 |

实测（`test_s34_fixtures.py` 与 A22 探针双重钉版）：准确率 1.0（门 ≥0.7）、
真缺陷 recall 1.0（门 ≥0.95）、误误导率 0.0（门 ≤0.02）、证据充分率 0.925
（门 ≥0.9，3/40 弃权）。封存哈希随文件内容变化，由 `seal_manifest` 计算，
测试只钉稳定性不钉字面值，避免改注释就得改测试。

## 处置纪律

- 夹具是**评测输入**，引擎规则不许针对夹具个案写特例；新增规则先在本清单回测。
- 误误导口径：GT=real_defect 却判进 flaky/case_outdated/environment/product_change
  才算误导；判 needs_human 是弃权（人审兜底），单独计弃权率。
- 红证记录见 `.agent-handoff/2026-11-10-s3.4-attribution.md`（红证改动均已回退，代码零残留）。
