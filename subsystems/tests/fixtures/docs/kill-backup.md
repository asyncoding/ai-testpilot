# Kill Backup 兜底机制

## 背景

强提醒闹钟响铃时，裸按侧边键只触发系统第一层停钟；若接管页未出现，
铃声必须继续。项目通过 kill-backup 机制在 30 秒后兜底。

## 历史变更

- 早期版本 killBackupInterval = 90s；
- 2026-09 调整为 **30s**，事故复盘见 AlarmKitSpike-*.md（历史证词）。

## 相关符号

- `MissionCoordinator.schedulePrimary`
- `MissionCoordinator.killBackupInterval`
