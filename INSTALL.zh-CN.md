# 微信公众号控制台 - 服务器安装说明

本包适用于已安装 Docker 的 Linux 服务器和宝塔面板。应用仅监听服务器本机 `127.0.0.1:8787`，外部访问必须通过宝塔或 Nginx HTTPS 反向代理，不能直接从公网访问容器端口。

## 1. 从 GitHub 拉取

推荐目录：

```bash
mkdir -p /www/docker/wechat-material-uploader
git clone https://github.com/juneme/wechat-console-server.git /www/docker/wechat-material-uploader
cd /www/docker/wechat-material-uploader
```

拉取后应能直接看到 `docker-compose.yml`、`Dockerfile`、`.env.example`、`install.sh` 和 `app/`。配套 Skill 位于独立的 [`juneme/wechat-article-designer`](https://github.com/juneme/wechat-article-designer) 仓库，不需要部署到服务器。仓库不会包含 `.env`；首次安装由 `install.sh` 根据 `.env.example` 创建，更新时保留服务器原有 `.env`。

## 2. 准备运行配置

当前目录的 `.env` 允许保持为空白配置：

```ini
WECHAT_APP_ID=
WECHAT_APP_SECRET=
CREDENTIALS_ENCRYPTION_KEY=
ADMIN_USERNAME=
ADMIN_PASSWORD=
PUBLIC_BASE_URL=
AI_API_KEY=
PUBLISH_API_KEY=
TEMP_API_KEY=
TEMP_STORAGE_MAX_BYTES=5000000000
TEMP_USER_STORAGE_MAX_BYTES=500000000
TEMP_RETENTION_DAYS=30
MAX_USERS=100
MAX_ACCOUNTS_PER_USER=20
```

首次启动会在数据卷生成一次性初始化码，`install.sh` 会将其显示在当前私密终端。初始化界面必须同时填写该码、管理员账号和密码；成功后初始化码立即删除，三类 API Key 自动生成并加密写入数据库。随后在“公众号设置”添加首个公众号；每个用户都可添加多个订阅号或服务号。登录页在初始化完成后开放自行注册，普通用户只能访问自己的公众号、素材、临时图片和草稿，不能读取管理员专属 API Key。`PUBLIC_BASE_URL` 留空时按上传请求的域名生成 URL；如通过环境变量提供管理员或 API Key，旧版部署仍可兼容，并优先使用非空的环境变量。

界面默认上传到公众号永久素材；“服务器托管”放在最后，仅作为公众号接口不可用时的备选方案。临时图片会处理到严格小于 1MB，保存在 Docker 数据卷的 `/data/temp-images`，默认最多占用 5GB，到期 30 天后由程序自动删除。

用户密码哈希、角色、API Key、公众号配置、素材记录和草稿任务保存在 Docker 数据卷中的 SQLite 数据库。密码使用 Argon2 哈希；API Key 和 AppSecret 使用 `CREDENTIALS_ENCRYPTION_KEY` 或数据卷内的本地密钥加密。备份数据库时必须同时备份 `/data/.wechat-credentials.key`，否则无法恢复加密凭据。

如需使用微信上传模式，在微信开发者平台“我的业务 → 公众号/服务号 → 详情”取得 `AppID`/`AppSecret`。在服务器执行 `curl -4 https://api.ipify.org; echo` 查询当前公网出口 IPv4，把返回值加入 API 调用 IP 白名单，并确认公众号具有素材管理接口权限。未填微信密钥时，临时托管仍正常工作。

## 3. 一键启动

在宝塔“终端”中执行：

```bash
cd /www/docker/wechat-material-uploader
bash install.sh
```

脚本会检查 Docker、构建镜像、启动容器并等待健康检查。首次构建需要下载 Python 镜像和依赖；Dockerfile 已默认使用腾讯云 PyPI 镜像。请记录脚本最后输出的一次性初始化码，不要把它发送到聊天、日志或工单。

手工检查：

```bash
docker compose ps
curl http://127.0.0.1:8787/healthz
docker compose logs --tail=100 uploader
```

健康接口应返回 HTTP 200 和 JSON。容器状态应为 `Up` 或 `healthy`。

登录控制台后进入“API 接口”，点击“开始诊断”。诊断会检查数据库、微信 access token、三类 API Key 和公网地址配置，但不会返回 AppSecret 或任何 API Key。

## 4. 宝塔反向代理和 HTTPS

1. 在宝塔“网站”中添加用于本工具的域名，不要覆盖现有站点。
2. 打开该站点的“反向代理”，目标 URL 填 `http://127.0.0.1:8787`。
3. 发送域名保持默认开启；代理超时建议设为 `120` 秒。
4. 在“SSL”中申请 Let's Encrypt 证书，验证正常后开启强制 HTTPS。
5. 网站配置中的请求体限制设为至少 `32m`。示例 Nginx 配置见 `nginx/wechat-uploader.conf`。

配置完成后，用域名打开页面。全新安装会显示初始化界面；输入 `install.sh` 输出的一次性初始化码并创建管理员后，会自动登录并进入公众号配置页。此后登录页提供注册入口，新用户注册成功后也会直接进入自己的首个公众号配置页。

自行注册默认对所有能访问登录页的人开放。用于内部团队时，应通过反向代理访问控制、VPN 或防火墙限制站点访问范围；不要把只供内部使用的控制台直接暴露到不受控公网。

## 5. AI API 调用

上传到微信公众号（图片文件不保存到本服务器）：

```bash
curl -X POST 'https://你的图片站点域名/api/v1/wechat-images' \
  -H 'Authorization: Bearer 你的AI_API_KEY' \
  -F 'mode=material' \
  -F 'images=@/path/photo-01.jpg' \
  -F 'images=@/path/photo-02.png'
```

返回 JSON 包含 `filename`、`url`、`size`、`uploaded_at`、上传状态和微信 `media_id`。完整字段、上传模式及错误码见 [API.zh-CN.md](API.zh-CN.md)。

以下接口用于服务器临时托管：

同一个请求可重复提交 `images` 字段来批量上传：

```bash
curl -X POST 'https://你的图片站点域名/api/v1/temp-images' \
  -H 'Authorization: Bearer 你的TEMP_API_KEY' \
  -F 'images=@/path/photo-01.jpg' \
  -F 'images=@/path/photo-02.png'
```

读取 30 天内仍有效的图片与 URL：

```bash
curl 'https://你的图片站点域名/api/v1/temp-images?limit=500' \
  -H 'Authorization: Bearer 你的TEMP_API_KEY'
```

API 返回的每条记录含文件名、URL、尺寸、压缩后字节数、SHA-256、创建时间和过期时间。图片 URL `/temp/{token}` 可公开读取；上传和列表接口必须提供 API Key。

## 6. 常用运维命令

```bash
cd /www/docker/wechat-material-uploader

# 查看状态
docker compose ps

# 查看日志
docker compose logs -f --tail=200 uploader

# 修改 .env 后重启
docker compose up -d

# 更新代码后重建
docker compose up -d --build

# 停止服务（不会删除数据库卷）
docker compose down
```

轮换图片和草稿 API Key：

```bash
bash rotate-api-keys.sh

# 如需同时轮换 TEMP_API_KEY
bash rotate-api-keys.sh --include-temp
```

脚本要求人工输入 `ROTATE`，只更新 `.env` 并重建容器，不会在终端打印新密钥。健康检查失败时会还原原配置。轮换成功后，使用 `bash show-client-config.sh --url https://你的控制台域名 --show-secrets` 在私密终端取得新的 Skill 配置。

备份 SQLite 数据库：

```bash
docker compose cp uploader:/data/uploader.sqlite3 ./uploader-$(date +%F-%H%M%S).sqlite3
```

数据库使用显式 schema 版本。程序发现旧版数据库时，会先在 `/data` 中生成 `uploader.schema-v旧版本-to-v新版本-时间.sqlite3`，再执行迁移；诊断结果会显示本次启动是否创建了迁移备份。这不能替代异机备份。

恢复前先停止容器，并保留当前数据库备份。不要执行 `docker compose down -v`，该命令会删除数据卷。

## 7. 构建和校验发布包

项目维护者在仓库根目录执行：

```bash
python scripts/build_release.py
python scripts/build_release.py --verify-only artifacts/wechat-console-server-v3.2.1-$(date +%Y%m%d).zip
```

构建器只收集白名单中的服务端运行文件、开源文档和运维脚本，并在 ZIP 中生成 `RELEASE-MANIFEST.sha256`。`.env`、SQLite、密钥文件、缓存、已有 `artifacts` 和独立 Skill 源码不会进入发布包。

## 8. 故障排查

### 端口被占用

```bash
ss -lntp | grep ':8787'
```

如果已有其他程序占用端口，修改 `docker-compose.yml` 左侧端口，例如 `127.0.0.1:8789:8000`，并同步修改宝塔反向代理目标。

### 构建下载慢

确认 Dockerfile 中包含：

```dockerfile
ARG PIP_INDEX_URL=https://mirrors.cloud.tencent.com/pypi/simple
```

然后重新执行 `docker compose up -d --build`。若 Docker Hub 镜像拉取仍慢，需要在宝塔 Docker 设置中配置镜像加速器。

### 页面正常但无法上传微信

依次检查：公众号类型与接口权限、`.env` 中 AppID/AppSecret、微信 IP 白名单、服务器时间、容器日志。修改 `.env` 后执行 `docker compose up -d`。

### 临时 URL 域名不正确

把 `.env` 中 `PUBLIC_BASE_URL` 改为实际 HTTPS 根地址，例如 `https://photos.example.com`，然后执行 `docker compose up -d`。不要填写 `/temp` 路径。

### 反向代理出现 502

先在服务器执行 `curl http://127.0.0.1:8787/healthz`。本机健康但域名 502 时，检查宝塔反向代理目标、站点 Nginx 配置和防火墙；本机也失败时查看容器日志。
