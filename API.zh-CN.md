# 微信公众号控制台 - AI 接口调用文档

## 1. 接口用途

AI 客户端可通过本接口把图片上传到微信公众号，并取得结构化 JSON。图片文件不会保存到本服务器；服务器只在 SQLite 中保存文件名、微信返回结果和上传时间等记录。

调用前需在服务器 `.env` 配置：

```ini
WECHAT_APP_ID=wx开头的公众号AppID
WECHAT_APP_SECRET=公众号AppSecret
AI_API_KEY=至少24位的随机密钥
PUBLISH_API_KEY=至少24位的独立草稿密钥
```

服务器公网出口 IP 必须已加入微信公众号 API 调用 IP 白名单。修改 `.env` 后执行 `docker compose up -d`。

## 2. 上传图片

```text
POST /api/v1/wechat-images
Authorization: Bearer <AI_API_KEY>
Content-Type: multipart/form-data
```

表单字段：

| 字段 | 必填 | 说明 |
|---|---|---|
| `images` | 是 | 图片文件；重复该字段可批量上传，单次最多 20 张 |
| `mode` | 否 | `material`、`article` 或 `both`，默认 `material` |

上传模式：

| 模式 | 微信存储位置 | URL 字段 |
|---|---|---|
| `material` | 公众号永久素材库 | `material_url` |
| `article` | 公众号文章正文图片 | `article_url` |
| `both` | 同时上传以上两处 | 顶层 `url` 优先使用 `article_url` |

### curl 示例

```bash
curl -X POST 'https://photos.example.com/api/v1/wechat-images' \
  -H 'Authorization: Bearer YOUR_AI_API_KEY' \
  -F 'mode=material' \
  -F 'images=@/path/photo-01.jpg' \
  -F 'images=@/path/photo-02.png'
```

### Python 示例

```python
import requests

url = "https://photos.example.com/api/v1/wechat-images"
headers = {"Authorization": "Bearer YOUR_AI_API_KEY"}
files = [
    ("images", ("photo-01.jpg", open("photo-01.jpg", "rb"), "image/jpeg")),
    ("images", ("photo-02.png", open("photo-02.png", "rb"), "image/png")),
]

response = requests.post(
    url,
    headers=headers,
    data={"mode": "material"},
    files=files,
    timeout=120,
)
response.raise_for_status()
print(response.json())
```

## 3. JSON 响应

```json
{
  "items": [
    {
      "filename": "photo-01.jpg",
      "url": "https://mmbiz.qpic.cn/example",
      "size": 824315,
      "uploaded_at": "2026-08-17T09:30:00+00:00",
      "status": "complete",
      "media_id": "MEDIA_ID",
      "material_url": "https://mmbiz.qpic.cn/example",
      "article_url": null,
      "width": 1600,
      "height": 1200,
      "sha256": "...",
      "errors": []
    }
  ],
  "count": 1,
  "success_count": 1,
  "error_count": 0,
  "mode": "material"
}
```

核心字段：

| 字段 | 说明 |
|---|---|
| `filename` | 原照片名称 |
| `url` | 可用的微信图片 URL |
| `size` | 上传图片字节数；正文模式需要压缩时为处理后的字节数 |
| `uploaded_at` | UTC 时区的 ISO 8601 上传时间 |
| `status` | `complete`、`partial` 或 `failed` |
| `errors` | 当前图片的错误信息数组 |

批量请求中单张图片失败不会丢失其他图片的成功结果。调用方应同时检查 HTTP 状态码、`error_count` 和每项的 `status`。

## 4. 写入草稿箱

```text
POST /api/v1/wechat-drafts
Authorization: Bearer <PUBLISH_API_KEY>
Content-Type: application/json
```

请求正文中的 `content` 必须是最终公众号 HTML。所有正文图片 URL 必须来自图片接口 `mode=article` 返回的 `article_url`，封面 `thumb_media_id` 必须来自 `mode=material` 返回的永久素材 ID。

```json
{
  "request_id": "article-20260817-001",
  "title": "文章标题",
  "author": "作者",
  "digest": "文章摘要",
  "content": "<section>最终 HTML</section>",
  "content_source_url": "",
  "thumb_media_id": "COVER_MEDIA_ID",
  "need_open_comment": 0,
  "only_fans_can_comment": 0
}
```

`request_id` 是调用方生成的幂等标识。已确认创建成功的相同请求会直接返回已有草稿 `media_id`；相同 `request_id` 对应不同内容时返回 `409`。如果调用微信接口时发生响应超时，服务器会把任务标记为 `unknown` 并阻止自动重试，因为此时微信可能已经创建草稿。请先在控制台和微信公众号草稿箱中人工核对。

```json
{
  "status": "created",
  "media_id": "DRAFT_MEDIA_ID",
  "request_id": "article-20260817-001",
  "cached": false,
  "validation": {"characters": 6820, "bytes": 9012, "images": 4}
}
```

## 5. 常见错误

| HTTP 状态 | 原因 |
|---|---|
| `401` | 缺少 Bearer Key，或图片/草稿接口对应的 API Key 不正确 |
| `413` | 单次超过 20 张，或源文件超过服务器读取上限 |
| `422` | 缺少 `images`、模式值无效或表单格式错误 |
| `409` | 草稿 `request_id` 已对应其他内容 |
| `502` | 微信接口请求失败 |
| `503` | API Key 或微信公众号 AppID/AppSecret 未配置 |

`502` 可能代表远端执行结果无法确认。调用方不得自动更换 `request_id` 或重试，必须先核对草稿箱和控制台任务状态。

`AI_API_KEY` 和 `PUBLISH_API_KEY` 只应保存在调用方的密钥配置中，不要写入网页 JavaScript、公开仓库、截图或聊天消息。

## 6. 管理员诊断

控制台“API 接口”页面通过以下接口运行服务端诊断：

```text
POST /api/diagnostics/run
Cookie: 已登录的管理员会话
X-Requested-With: WechatUploader
```

该接口不接受 AI Bearer Key，也不返回任何密钥。响应包含当前版本、整体就绪状态、数据库 schema/可写性、微信连接、各 API Key 配置状态、公网地址状态，以及本次启动产生的迁移备份文件名。微信连接检查会请求 access token，因此需要服务器出口 IPv4 已加入公众号白名单。
