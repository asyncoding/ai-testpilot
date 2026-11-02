#!/usr/bin/env bash
# 受限网络备用路径：在宿主机直接跑 MinIO（配合 deploy/docker-compose.host-minio.yml）。
# 内存紧张时系统可能回收该进程；重跑本脚本即可（数据在 store/minio，幂等，桶不存在才建）。
set -u
cd "$(dirname "$0")/.."

if curl -sf http://127.0.0.1:9000/minio/health/live >/dev/null 2>&1; then
  echo "minio 已在运行"
else
  mkdir -p store/minio
  nohup env MINIO_ROOT_USER=minioadmin MINIO_ROOT_PASSWORD=minioadmin \
    /opt/homebrew/bin/minio server store/minio --address ':9000' --console-address ':9001' \
    > /tmp/tp_minio.log 2>&1 &
  for _ in $(seq 1 20); do curl -sf http://127.0.0.1:9000/minio/health/live >/dev/null 2>&1 && break; sleep 1; done
fi
mc alias set local http://127.0.0.1:9000 minioadmin minioadmin >/dev/null 2>&1
mc mb --ignore-existing local/langfuse
curl -sf http://127.0.0.1:9000/minio/health/live && echo "minio live, bucket=langfuse, console=http://127.0.0.1:9001 (minioadmin/minioadmin)"
