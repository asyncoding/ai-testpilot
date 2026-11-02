"""SQLite 本地存储：元数据/FTS5/outbox/审批/反馈/评测/事件/队列（docs/07 §2）。

单机 POC 用 SQLite 同时承担关系库与 BM25 全文索引（FTS5）；
生产可平移到 Postgres + OpenSearch，端口模型不变。
"""

from .sqlite_store import Store, bootstrap_store
from .sqlite_queue import SqliteQueue

__all__ = ["Store", "bootstrap_store", "SqliteQueue"]
