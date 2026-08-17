# 微信公众号控制台

一个部署在服务器上的微信公众号管理控制台。它集中管理公众号凭据、微信连接、图片素材、AI 接口和草稿任务；AppSecret 仅在服务端加密保存，浏览器和 AI 客户端都无法读取。

配套的 [`wechat-article-designer`](https://github.com/juneme/wechat-article-designer) Codex Skill 独立维护。完整链路是：Codex 设计文章，控制台代管微信凭据并上传图片，用户确认后写入微信公众号草稿箱。

## 十分钟快速开始

### 1. 部署控制台

```bash
cp .env.example .env
bash install.sh
```

打开 `http://服务器IP:8787`，使用 `.env` 中的管理员账号登录，在“公众号设置”保存 AppID 和 AppSecret，并把服务器出口 IPv4 加入微信公众平台白名单。进入“API 接口”后点击“开始诊断”，确认数据库、微信连接和两类 Skill API Key 均正常。

### 2. 获取 Skill 客户端配置

```bash
bash show-client-config.sh --url http://服务器IP:8787
bash show-client-config.sh --url http://服务器IP:8787 --show-secrets
```

第二条命令需要人工确认，随后输出 `WECHAT_CONSOLE_URL`、`WECHAT_IMAGE_API_KEY` 和 `WECHAT_PUBLISH_API_KEY`。不要把输出提交到 Git、Issue、截图或聊天记录。

### 3. 安装 Skill

在运行 Codex 的 Windows 电脑执行：

```powershell
git clone https://github.com/juneme/wechat-article-designer.git "$env:USERPROFILE\.codex\skills\wechat-article-designer"
```

将上一步的三个变量配置到运行 Codex 的用户环境，重启 Codex 后使用：

```text
使用 $wechat-article-designer 制作这篇公众号文章，先预览，不要写入草稿箱。
```

确认内容后再明确回复“写入微信公众号草稿箱”。Skill 不会因为预览、排版或上传图片而自动创建草稿。

服务器详细部署见 [INSTALL.zh-CN.md](INSTALL.zh-CN.md)，API 契约见 [API.zh-CN.md](API.zh-CN.md)，Skill 直发流程见独立仓库的 [direct-publishing.md](https://github.com/juneme/wechat-article-designer/blob/main/references/direct-publishing.md)。

## 控制台能力

- 在网页中配置公众号名称、类型、AppID 和 AppSecret，无需重启容器。
- 统一查看微信连接、素材、草稿任务和 AI API 状态。
- 上传永久素材、正文图片，或使用服务器临时托管作为备选。
- 通过独立 Bearer Key 向 AI 提供图片上传和草稿写入接口。
- 对草稿请求执行 HTML、图片来源、字符数和幂等校验。
- 在“API 接口”页面执行不回传密钥的数据库、微信连接和 API 配置诊断。

## 上传模式

| 模式 | 微信接口 | 返回 | 适用场景 |
|---|---|---|---|
| 公众号素材（默认） | `/cgi-bin/material/add_material?type=image` | `media_id` + 素材 URL | 进入公众号素材库；BMP/PNG/JPEG/GIF，小于 10MB |
| 正文图片 | `/cgi-bin/media/uploadimg` | 正文图片 URL | 公众号文章正文；JPG/PNG，小于 1MB |
| 素材 + 正文 | 依次调用以上两个接口 | 两套结果 | 同一张图片既要进素材库，也要用于正文 |
| 服务器托管（备选） | 无需微信接口 | 公网图片 URL + JSON | 微信接口暂不可用或临时给 AI 读取图片 |

临时托管和正文模式会剥离图片元数据，并在需要时缩小尺寸、压缩到 1MB 以下。上传记录保存在 SQLite，刷新页面后会自动恢复。页面支持单条删除、勾选批量删除和全部删除：永久素材会同步调用微信删除接口；服务器托管会删除本地文件；正文图片没有微信撤销接口，只删除本地历史记录。

服务器临时托管默认最多占用 5GB，可通过 `TEMP_STORAGE_MAX_BYTES` 调整。超过 4000 万像素的图片会被拒绝，防止异常图片耗尽内存。

## 临时图片 API

在 `.env` 设置独立的 `TEMP_API_KEY`。上传与列表接口使用 Bearer 认证，公开图片 URL 本身不需要认证。批量上传时重复使用 `images` 字段：

```bash
curl -X POST 'https://photos.example.com/api/v1/temp-images' \
  -H 'Authorization: Bearer YOUR_TEMP_API_KEY' \
  -F 'images=@photo-01.jpg' \
  -F 'images=@photo-02.png'

curl 'https://photos.example.com/api/v1/temp-images?limit=500' \
  -H 'Authorization: Bearer YOUR_TEMP_API_KEY'
```

每条数据包含 `filename`、`url`、`kind`、`width`、`height`、`processed_bytes`、`sha256`、`created_at` 和 `expires_at`，AI 可直接用文件名对应图片 URL。`PUBLIC_BASE_URL` 建议填写站点 HTTPS 根地址；留空时按当前请求的域名生成 URL。

## 微信侧准备

以下配置只影响三个微信上传模式，临时托管不需要微信密钥：

1. 在微信开发者平台取得公众号 `AppID` 和 `AppSecret`，部署后登录控制台的“公众号设置”保存。
2. 把服务器的固定公网出口 IPv4 加到公众号的 IP 白名单。
3. 确认该公众号具备素材管理接口权限。

`AppSecret` 可以由控制台加密写入 SQLite，也可继续通过服务器 `.env` 提供。不得放进网页 JavaScript、截图或公开仓库。

## Docker 部署

```bash
cd wechat-material-uploader
cp .env.example .env
# 编辑 .env；ADMIN_PASSWORD 必填，微信密钥可在控制台补填
docker compose up -d --build
docker compose ps
curl http://127.0.0.1:8787/healthz
```

应用绑定服务器所有 IPv4 接口的 `8787` 端口，可通过 `http://服务器IP:8787` 访问。缺少微信密钥时，临时托管和临时图片 API 仍可用，三个微信上传模式会提示未配置。生产环境建议修改 [nginx/wechat-uploader.conf](nginx/wechat-uploader.conf) 中的域名和证书路径，再由 Nginx 提供 HTTPS，并在防火墙中限制 `8787` 端口的访问来源。打开页面后，使用 `.env` 中的管理账号和密码登录。

更新：

```bash
docker compose up -d --build
```

更新不会覆盖 `.env`，也不会删除 Docker 数据卷。数据库带有显式 schema 版本；发现旧版数据库时，程序会先在数据库同目录生成带时间戳的迁移前备份，再执行升级。部署前仍建议手工备份数据库；不要执行 `docker compose down -v`。

日志：

```bash
docker compose logs -f --tail=200 uploader
```

备份数据库：

```bash
docker compose cp uploader:/data/uploader.sqlite3 ./uploader-$(date +%F).sqlite3
```

轮换 Skill 使用的图片和草稿 API Key：

```bash
bash rotate-api-keys.sh
# 同时轮换临时图片 API Key
bash rotate-api-keys.sh --include-temp
```

脚本要求在交互终端输入 `ROTATE`，不会打印新密钥；容器未恢复健康时会还原旧 `.env`。轮换成功后，用 `show-client-config.sh --show-secrets` 在私密终端重新配置 Skill 客户端。

## 本地开发与测试

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env
set -a; source .env; set +a
uvicorn app.main:app --reload
pytest -q
ruff check .
node --check app/static/app.js
```

Windows PowerShell 可用：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
$env:WECHAT_APP_ID="wx..."
$env:WECHAT_APP_SECRET="..."
$env:ADMIN_PASSWORD="至少十二位随机密码"
$env:DATABASE_PATH=".\data\uploader.sqlite3"
uvicorn app.main:app --reload
```

构建并二次校验服务端发布包：

```bash
python scripts/build_release.py
python scripts/build_release.py --verify-only artifacts/wechat-console-server-v3.1.0-$(date +%Y%m%d).zip
```

服务端包只包含控制台、部署脚本和服务端文档，并使用白名单排除 `.env`、SQLite、密钥、缓存、旧产物及 Skill 源码；ZIP 内的 `RELEASE-MANIFEST.sha256` 可校验每个文件。Skill 在独立仓库中发布。

## 官方依据

- [新增草稿](https://developers.weixin.qq.com/doc/service/api/draftbox/draftmanage/api_draft_add)
- [上传永久素材](https://developers.weixin.qq.com/doc/service/api/material/permanent/api_addmaterial)
- [上传发表内容中的图片](https://developers.weixin.qq.com/doc/service/api/material/permanent/api_uploadimage)
- [获取稳定版接口调用凭据](https://developers.weixin.qq.com/doc/service/api/base/api_getstableaccesstoken)
- [AppID / AppSecret 官方说明](https://developers.weixin.qq.com/doc/oplatform/developers/dev/appid.html)
- [API 调用 IP 白名单](https://developers.weixin.qq.com/doc/oplatform/developers/basic_func/ip_whitelist.html)

微信官方明确要求这些接口在服务器端调用。永久素材图片 URL 主要用于腾讯系域名；文章正文应优先使用 `uploadimg` 返回的 URL。
