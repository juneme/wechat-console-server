#!/usr/bin/env bash
set -Eeuo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

command -v docker >/dev/null 2>&1 || fail "Docker 未安装。请先在宝塔 Docker 页面安装 Docker。"
docker compose version >/dev/null 2>&1 || fail "Docker Compose 插件不可用。"

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "已创建空白 .env。"
fi

chmod 600 .env

echo "开始构建并启动服务..."
docker compose up -d --build

echo "等待健康检查..."
for _ in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:8791/healthz >/dev/null 2>&1; then
    setup_code="$(docker compose exec -T uploader sh -c 'cat /data/.wechat-setup-token 2>/dev/null || true' | tr -d '\r\n')"
    echo "部署成功。服务默认只监听服务器本机 127.0.0.1:8791。"
    if [[ -n "$setup_code" ]]; then
      echo "一次性初始化码：$setup_code"
      echo "请通过已配置的 HTTPS 反向代理打开控制台并完成初始化。"
    fi
    docker compose ps
    exit 0
  fi
  sleep 2
done

echo "健康检查失败，输出最近日志：" >&2
docker compose ps >&2 || true
docker compose logs --tail=200 uploader >&2 || true
exit 1
