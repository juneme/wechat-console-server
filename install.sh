#!/usr/bin/env bash
set -Eeuo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

command -v docker >/dev/null 2>&1 || fail "Docker 未安装。请先在宝塔 Docker 页面安装 Docker。"
docker compose version >/dev/null 2>&1 || fail "Docker Compose 插件不可用。"

random_hex() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 24
  else
    tr -d '-' </proc/sys/kernel/random/uuid
  fi
}

if [[ ! -f .env ]]; then
  cp .env.example .env
  generated_password="$(random_hex)"
  sed -i "s#^ADMIN_PASSWORD=.*#ADMIN_PASSWORD=${generated_password}#" .env
  echo "已创建 .env，并生成随机管理密码。"
fi

chmod 600 .env

admin_password="$(sed -n 's/^ADMIN_PASSWORD=//p' .env | tail -n 1)"
[[ -n "${admin_password}" ]] || fail ".env 中 ADMIN_PASSWORD 不能为空。"
[[ "${admin_password}" != replace-with-* ]] || fail "请先修改 .env 中的 ADMIN_PASSWORD。"

temp_api_key="$(sed -n 's/^TEMP_API_KEY=//p' .env | tail -n 1)"
if [[ -z "${temp_api_key}" || "${temp_api_key}" == replace-with-* ]]; then
  generated_api_key="$(random_hex)"
  if grep -q '^TEMP_API_KEY=' .env; then
    sed -i "s#^TEMP_API_KEY=.*#TEMP_API_KEY=${generated_api_key}#" .env
  else
    printf '\nTEMP_API_KEY=%s\n' "${generated_api_key}" >>.env
  fi
  echo "已生成临时图片 API Key（仅写入 .env，不在终端显示）。"
fi

ai_api_key="$(sed -n 's/^AI_API_KEY=//p' .env | tail -n 1)"
if [[ -z "${ai_api_key}" || "${ai_api_key}" == replace-with-* ]]; then
  generated_ai_api_key="$(random_hex)"
  if grep -q '^AI_API_KEY=' .env; then
    sed -i "s#^AI_API_KEY=.*#AI_API_KEY=${generated_ai_api_key}#" .env
  else
    printf '\nAI_API_KEY=%s\n' "${generated_ai_api_key}" >>.env
  fi
  echo "已生成微信公众号 AI 上传 API Key（仅写入 .env，不在终端显示）。"
fi

publish_api_key="$(sed -n 's/^PUBLISH_API_KEY=//p' .env | tail -n 1)"
if [[ -z "${publish_api_key}" || "${publish_api_key}" == replace-with-* ]]; then
  generated_publish_api_key="$(random_hex)"
  if grep -q '^PUBLISH_API_KEY=' .env; then
    sed -i "s#^PUBLISH_API_KEY=.*#PUBLISH_API_KEY=${generated_publish_api_key}#" .env
  else
    printf '\nPUBLISH_API_KEY=%s\n' "${generated_publish_api_key}" >>.env
  fi
  echo "已生成微信公众号草稿发布 API Key（仅写入 .env，不在终端显示）。"
fi

encryption_key="$(sed -n 's/^CREDENTIALS_ENCRYPTION_KEY=//p' .env | tail -n 1)"
if [[ -z "${encryption_key}" || "${encryption_key}" == replace-with-* ]]; then
  generated_encryption_key="$(random_hex)"
  if grep -q '^CREDENTIALS_ENCRYPTION_KEY=' .env; then
    sed -i "s#^CREDENTIALS_ENCRYPTION_KEY=.*#CREDENTIALS_ENCRYPTION_KEY=${generated_encryption_key}#" .env
  else
    printf '\nCREDENTIALS_ENCRYPTION_KEY=%s\n' "${generated_encryption_key}" >>.env
  fi
  echo "已生成公众号凭据加密主密钥（仅写入 .env，不在终端显示）。"
fi

if ! grep -Eq '^WECHAT_APP_ID=wx[0-9A-Za-z]+' .env || ! grep -Eq '^WECHAT_APP_SECRET=.{8,}' .env; then
  echo "提示：微信 AppID/AppSecret 可在部署后登录控制台配置。"
fi

echo "开始构建并启动服务..."
docker compose up -d --build

echo "等待健康检查..."
for _ in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:8787/healthz >/dev/null 2>&1; then
    echo "部署成功：请访问 http://服务器IP:8787"
    echo "查看 Skill 客户端配置状态：bash show-client-config.sh --url http://服务器IP:8787"
    docker compose ps
    exit 0
  fi
  sleep 2
done

echo "健康检查失败，输出最近日志：" >&2
docker compose ps >&2 || true
docker compose logs --tail=200 uploader >&2 || true
exit 1
