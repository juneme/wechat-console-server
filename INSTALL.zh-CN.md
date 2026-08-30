# 云浪控制台服务器安装

适用于已安装 Docker Compose 的 Linux 服务器。容器默认只绑定 `127.0.0.1:8791`，可由反向代理对外提供服务。

## 1. 安装

```bash
git clone https://github.com/juneme/wechat-console-server.git /www/docker/wechat-console-server
cd /www/docker/wechat-console-server
cp .env.example .env
bash install.sh
```

`.env` 不需要任何客户端 API Key。主要配置如下：

```ini
WECHAT_APP_ID=
WECHAT_APP_SECRET=
CREDENTIALS_ENCRYPTION_KEY=
ADMIN_USERNAME=
ADMIN_PASSWORD=
PUBLIC_BASE_URL=
TEMP_STORAGE_MAX_BYTES=5000000000
TEMP_USER_STORAGE_MAX_BYTES=500000000
TEMP_RETENTION_DAYS=30
MAX_USERS=100
MAX_ACCOUNTS_PER_USER=20
```

首次启动时，`install.sh` 会在当前私密终端显示一次性初始化码。用它创建管理员账号，然后在网页添加公众号。AppSecret 使用服务器加密密钥保存，客户端无法读取。

## 2. 反向代理

将域名反向代理到 `http://127.0.0.1:8791`，请求体限制至少 32MB，代理超时至少 120 秒。

HTTP 可用于可信的个人环境，页面会显示明文传输提醒。公网服务建议启用 HTTPS。

## 3. AI 配对

1. 管理员登录控制台。
2. 在“API 接入”生成 1 分钟验证码。
3. AI 客户端在本地输入服务器地址与验证码。
4. 服务器返回统一客户端令牌；数据库只保存摘要。

静态图片、草稿和临时图片 Key 已全部移除。重新配对会撤销上一枚客户端令牌，修改密码也会撤销令牌。

## 4. 验证

```bash
docker compose ps
curl -fsS http://127.0.0.1:8791/healthz
```

然后在控制台运行诊断，确认数据库和微信连接正常。微信接口还要求将服务器出口 IPv4 加入公众号白名单。

拿到配对令牌后可验证：

```bash
curl 'http://服务器:8791/api/v1/temp-images?limit=1' \
  -H 'Authorization: Bearer CLIENT_TOKEN'
```

完整接口见 [API.zh-CN.md](API.zh-CN.md)。

## 5. 运维

```bash
cd /www/docker/wechat-console-server
docker compose ps
docker compose logs -f --tail=200 uploader
docker compose up -d --build
```

不要执行 `docker compose down -v`，否则会删除数据卷。备份：

```bash
docker compose cp uploader:/data/uploader.sqlite3 ./uploader-$(date +%F-%H%M%S).sqlite3
docker compose cp uploader:/data/.wechat-credentials.key ./wechat-credentials-$(date +%F-%H%M%S).key
```

程序发现旧数据库时会先生成迁移备份。v4 迁移会删除旧静态服务凭据表并创建客户端令牌表。

## 6. 常见问题

- 页面可访问但微信接口失败：检查公众号权限、AppID/AppSecret、出口 IP 白名单和容器日志。
- 临时图片 URL 地址不对：将 `PUBLIC_BASE_URL` 设为实际根地址，不要附加路径。
- 反向代理 502：先检查 `curl http://127.0.0.1:8791/healthz`，再检查代理目标和防火墙。
- 构建下载慢：保留 Dockerfile 中的腾讯云 PyPI 镜像配置。
