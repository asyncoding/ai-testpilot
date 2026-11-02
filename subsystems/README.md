# TestPilot subsystems

研发测试质量智能体平台的工程实现。架构与需求权威见 `../docs/07-最终版总体设计-v2.md`、
`../docs/08-需求说明书.md`；本文件是 S0 骨架的**运行手册**。

## 目录

```
subsystems/
├── packages/
│   ├── core/          # testpilot-core：统一领域模型 + 端口 Protocol + 策略（零 IO/零 SDK）
│   ├── adapters/      # testpilot-adapters：文件/JSON/SQLite-FTS/录制型 等适配器
│   ├── orchestrator/  # testpilot-orchestrator：profile 装配 + FastAPI（S1+ 放 LangGraph）
│   └── workers/       # testpilot-workers：ingest / runner / eval 独立进程入口
├── config/profiles/   # offline-demo.yaml（默认，零外部依赖）/ ios-local-poc.yaml
├── tests/fixtures/    # 工单/运行结果/模型应答/文档/事件 夹具（语言中立契约的证据）
├── deploy/            # docker-compose（redis/chroma + observability profile 的 Langfuse）
└── scripts/           # import_lint.py（核心层防腐门禁）、smoke_trace.py（S0 trace 门禁）
```

## 环境准备（venv 强制隔离，禁止污染系统 Python）

需要 [uv](https://docs.astral.sh/uv/) 与 Python 3.12+（本机网络装不了 3.12 时用 3.14 亦可，
仓内 `.python-version` 当前为 3.14）。

```bash
cd subsystems
uv sync                       # 创建 .venv 并以 editable 方式安装四个包
```

> 网络备注：若默认 PyPI 源 TLS 失败，用清华镜像（只作为本机环境变量，不写进仓内配置）：
> `UV_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple uv sync`
>
> 若曾在包目录尚未建全时执行过 `uv sync`，editable 安装可能是空壳（import 报
> ModuleNotFoundError），修复：
> `uv sync --reinstall-package testpilot-core --reinstall-package testpilot-adapters \
>  --reinstall-package testpilot-orchestrator --reinstall-package testpilot-workers`

## 常用命令

```bash
# 全量契约测试（28 个：URN/策略、四类适配器契约、SQLite+FTS5、队列幂等、装配、核心防腐）
.venv/bin/python -m pytest -q

# 核心层防腐门禁（core 禁止 import fastapi/sqlite/yaml/各兄弟包等）
.venv/bin/python scripts/import_lint.py

# API（默认 offline-demo profile）
.venv/bin/python -m uvicorn testpilot_orchestrator.app:app --port 8000
curl -s http://127.0.0.1:8000/health | jq .

# 三个 worker 的装配自检（独立进程能装起整套容器即 S0 达标）
.venv/bin/testpilot-ingest
.venv/bin/testpilot-runner
.venv/bin/testpilot-eval

# 切 profile：环境变量覆盖，不改文件
TESTPILOT_PROFILE=config/profiles/ios-local-poc.yaml testpilot-ingest
TESTPILOT_REPO_PATH=/path/to/repo .venv/bin/python -m pytest   # 覆盖被测仓路径
```

## 一键验收 S0（已闭合的 6 个自动门禁）

```bash
cd subsystems
bash scripts/verify_s0.sh    # 6 PASS / 0 FAIL；日志在 /tmp/tp_*.log
```

覆盖：venv 四包可 import、28 个契约测试、core 防腐 import-lint、`/health` 真实 200、
三个 worker 独立进程 ready、noop 空链路 trace 调用面。

## 基础设施（Docker）

```bash
# S1+ 用：redis（arq 队列）、chroma（向量检索）。S0 门禁不需要它俩（redis 只在容器网内被 langfuse 用）
docker compose -f deploy/docker-compose.yml up -d

# S0 trace 门禁：Langfuse v3 自托管栈（pg/clickhouse/minio 占位/langfuse web + worker）
# 必须同时起 langfuse 和 langfuse-worker（v3 拆分：web 入队，worker 消费写 ClickHouse）
docker compose -f deploy/docker-compose.yml --profile observability up -d langfuse langfuse-worker
# 健康判定（约 1–2 分钟，首次要跑迁移）
curl -s http://127.0.0.1:3030/api/public/health   # 期望 {"status":"OK","version":"3.x"}
# worker 没有端口，看日志确认起来
docker logs testpilot-langfuse-worker-1 --tail 5
```

镜像拉取（受限网络实测：docker.io 直连 TLS EOF；**docker.1ms.run 可用**，daocloud blob CDN 不稳；
镜像源不写进 compose，拉完 `tag` 回标准名保持文件可移植）：

```bash
for img in library/postgres:16-alpine clickhouse/clickhouse-server:24.8-alpine \
           langfuse/langfuse:3 langfuse/langfuse-worker:3 chromadb/chroma:0.5.20; do
  docker pull docker.1ms.run/$img && docker tag docker.1ms.run/$img ${img#library/}
done
```

### 无 MinIO 镜像时的备用路径（2026-11 实测打通）

各镜像站均不代理 `minio/*` 老标签。改用 brew 在宿主机直接跑 MinIO（bottle 走 ghcr.io）：

```bash
HOMEBREW_NO_AUTO_UPDATE=1 brew install minio minio-mc
# 起宿主机 MinIO 并建桶（幂等；内存紧张被系统回收时重跑此脚本即可）
bash scripts/start_host_minio.sh
# 起栈时叠加 host-minio 覆盖（S3 指向 host.docker.internal，不发布容器 redis 端口避免与宿主 redis 冲突）
docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.host-minio.yml \
  --profile observability up -d langfuse langfuse-worker
```

> 内存提醒：观测栈容器约占 2.2 GiB（ClickHouse ~1.1G、Langfuse ~1G，机器 8G 时偏紧），
> 宿主机 MinIO 可能被系统回收；症状是 trace 导出失败，重跑 `scripts/start_host_minio.sh` 即可。

踩坑记录（compose 已修，别再踩）：①ClickHouse alpine 内 `localhost` 走 IPv6 而 CH 没绑
IPv6，健康检查必须用 `127.0.0.1`；②Langfuse v3 的 `ENCRYPTION_KEY` 必须 **64 位 hex**
（`openssl rand -hex 32`，compose 里是仅本地开发的固定全零值），且除 `S3_*` 媒体桶外
还必须配 `LANGFUSE_S3_EVENT_UPLOAD_*` 事件桶变量，否则启动 500（ZodError）；
③Langfuse 镜像内 Node 服务只绑容器网卡 IP（不绑 loopback/::1），它的 healthcheck
必须用 `http://$(hostname):3000`；④**v3.225 的 BullMQ 队列只认 `REDIS_HOST/REDIS_PORT`
（或 `REDIS_CONNECTION_STRING`），旧文档里的 `LANGFUSE__QUEUE__REDIS__CONNECTION`
被静默忽略——现象是 OTLP 返回 500、日志只有 "Failed to instantiate otel ingestion queue"、
事件已写 S3 但 ClickHouse traces 表为空、Redis 无队列键。compose 已改。
⑤修了④之后任务进了队列但**只入队不消费**（wait 堆积）：v3.225 的 node-redis worker
连接在 Redis 无密码时仍会发 AUTH（未设置时发的是字符串 `"undefined"`，空串也会发），
被 Redis 拒连。解法：容器 Redis 加 `--requirepass tp-dev-redis`，langfuse 配
`REDIS_AUTH=tp-dev-redis`（仅本地开发固定值）。排查命令：
`redis-cli -a tp-dev-redis LLEN bull:otel-ingestion-queue:wait`（正常应为 0）。
⑥**v3 还必须起独立的 `langfuse-worker` 容器**（镜像 langfuse/langfuse-worker:3）：
web 只把 OTel 事件写 S3+入队，读 S3→写 ClickHouse 的消费者是 worker。少了它的症状是
OTLP 不报错（或 500）但 trace 永不入库、`bull:otel-ingestion-queue:wait` 只增不减。
已实测：补齐 worker 后积压任务立即被消费，3 条 smoke trace 入库。

## S0 第 7 个门禁：空链路 trace 出现在 Langfuse（手动，约 2 分钟）

前置：上面的 observability 栈 `health=OK`。

1. 浏览器打开 http://localhost:3030 → 注册首个本地账号 → 建 project
   （Settings → Projects，默认 project 也可）→ Project Settings 里创建 API keys，复制
   `pk-lf-...` / `sk-lf-...`；
2. SDK（v5，OTel API；LangfuseTracer 不兼容旧 v2）：
   `UV_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple uv pip install --python .venv/bin/python 'langfuse>=5.0'`；
3. 发一条空链路 trace，脚本**回查 Langfuse API 确认可见**（不是只信 SDK flush）：

```bash
LANGFUSE_PUBLIC_KEY=pk-lf-... LANGFUSE_SECRET_KEY=sk-lf-... \
TESTPILOT_TRACER=langfuse \
.venv/bin/python scripts/smoke_trace.py --expect langfuse
# 成功：{"verified": true, "tracer": "langfuse", ...}，退出码 0；同时 UI Traces 出现 smoke.empty_chain
```

判定标准与排障：

- 退出码 0 + UI 可见 → 门禁闭合。
- `30s 内未查询到`：先 `curl -u pk:sk 'http://127.0.0.1:3030/api/public/traces?sessionId=<输出里的 run_id>'`；
  401=key 错；空数组看 `docker logs testpilot-langfuse-1` 与 S3 连通（host 路径下
  `mc ls local/langfuse/events/` 应有对象）。
- 无 Docker 环境：`scripts/smoke_trace.py`（默认 noop）只能证明调用面，**不算闭合此门禁**。

## 写入安全默认值

所有 profile 默认 `external_write_enabled: false`；JSON 工单适配器的写操作经
`policy.assert_writable` 守卫（R2/R3 需审批、R4 禁止、全局 kill switch），
outbox 先落库后外发。这是演示环境的安全底线，换真实适配器时不得绕过。
