#!/usr/bin/env bash
# S1.5 前端构建：Next.js 静态导出到 web/out，由 FastAPI StaticFiles 同端口托管。
# 不纳入 verify_s0 自动门禁：npm 不可用/网络受限时不能拖垮 Python 链路（缺失 web/out
# 时后端只打 warning，/api 与 /health 照常服务）。
# 用法：cd subsystems && bash scripts/build_web.sh
set -eu
cd "$(dirname "$0")/../web"

if ! command -v npm >/dev/null 2>&1; then
  echo "FAIL  未找到 npm，请先安装 Node.js 18+（演示机可用 brew install node）" >&2
  exit 1
fi

# 国内网络拉 npmjs.org 慢/超时失败时，用清华镜像重试（不修改全局 npm 配置）：
#   NPM_REGISTRY=https://registry.npmmirror.com bash scripts/build_web.sh
REGISTRY="${NPM_REGISTRY:-}"
if [ -n "$REGISTRY" ]; then
  echo "==> 使用自定义 registry: $REGISTRY"
  npm install --registry="$REGISTRY"
else
  npm install
fi

# output:"export" → 纯静态产物 out/（FastAPI 挂载点，html=True 支持目录索引）
npm run build

echo
echo "✅ 构建完成：web/out（由 uvicorn 同端口托管，启动见 README「Web 对话页演示」）"
