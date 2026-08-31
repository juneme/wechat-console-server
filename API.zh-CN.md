# 云浪控制台 API

## 1. 认证

业务 API 只接受验证码兑换得到的统一客户端令牌：

```http
Authorization: Bearer <CLIENT_TOKEN>
```

旧的静态图片、草稿和临时图片 Key 均不再读取或接受。

管理员先在登录后的控制台调用 `POST /api/pairing-code` 生成验证码。验证码有效期 60 秒、仅可兑换一次；同一管理员生成新验证码会作废尚未使用的旧验证码。

客户端兑换：

```http
POST /api/v1/pairing/exchange
Content-Type: application/json

{"code":"ABCD-EFGH"}
```

成功响应：

```json
{
  "console_url": "http://console.example.test",
  "client_token": "仅此响应返回",
  "active_account_id": 1,
  "transport_secure": false,
  "warning": "当前使用 HTTP，客户端令牌已通过明文连接返回，请勿在不可信网络中使用。"
}
```

服务端只保存令牌摘要。每次兑换签发独立令牌，每个用户保留最近使用的 16 枚令牌；修改密码会撤销该用户的全部客户端令牌。生成和兑换响应均带 `Cache-Control: no-store, private`。

## 2. 公众号选择

令牌归属于生成验证码的用户。默认操作该用户当前公众号；可用请求头指定其名下其他公众号：

```http
X-Wechat-Account-ID: 2
```

## 3. 图片

```text
POST /api/v1/wechat-images
Content-Type: multipart/form-data
```

重复 `images` 字段可批量上传，单次最多 20 张。模式如下：

| mode | 用途 | 关键返回 |
|---|---|---|
| `article` | 正文图片 | `article_url` |
| `material` | 永久封面素材 | `media_id` |
| `both` | 同时上传两种 | 两者均返回 |

```bash
curl -X POST 'http://服务器:8791/api/v1/wechat-images' \
  -H 'Authorization: Bearer CLIENT_TOKEN' \
  -F 'mode=material' \
  -F 'images=@cover.jpg'
```

临时图片使用 `POST /api/v1/temp-images` 上传，使用 `GET /api/v1/temp-images?limit=500` 列表。两者需要同一客户端令牌，返回的 `/temp/{token}` 图片 URL 可公开读取。

微信可能为正文图返回 `http://mmecoa.qpic.cn` 或 `http://mmbiz.qpic.cn`。服务端统一将两者规范为 `https://mmbiz.qpic.cn`，并保留路径和查询参数。

## 4. 草稿

```text
POST /api/v1/wechat-drafts
Content-Type: application/json
```

```json
{
  "request_id": "article-20260830-001",
  "title": "文章标题",
  "author": "作者",
  "digest": "摘要",
  "content": "<section>最终 HTML</section>",
  "content_source_url": "",
  "thumb_media_id": "COVER_MEDIA_ID",
  "need_open_comment": 0,
  "only_fans_can_comment": 0
}
```

正文图片必须使用 `mode=article` 返回的 URL，封面必须使用 `mode=material` 返回的 `media_id`。为兼容旧客户端，草稿正文中的 `mmecoa.qpic.cn` 或非 HTTPS `mmbiz.qpic.cn` 图片地址会在校验和提交前自动规范化。相同 `request_id` 和内容不会重复创建；相同标识对应不同内容返回 `409`。

| 方法与路径 | 作用 |
|---|---|
| `GET /api/v1/wechat-drafts` | 分页查询草稿任务 |
| `GET /api/v1/wechat-drafts/{id}` | 读取并核对微信草稿 |
| `PUT /api/v1/wechat-drafts/{id}` | 修改微信草稿与本地记录 |
| `DELETE /api/v1/wechat-drafts/{id}` | 删除微信草稿与本地记录 |
| `GET /api/v1/wechat-drafts/wechat-box` | 分页读取真实微信草稿箱 |
| `GET /api/v1/wechat-drafts/wechat-box/{media_id}` | 读取指定微信草稿 |
| `PUT /api/v1/wechat-drafts/wechat-box/{media_id}` | 修改指定微信草稿 |
| `DELETE /api/v1/wechat-drafts/wechat-box/{media_id}` | 删除指定微信草稿并清理本地记录 |

`PUT` 只需提交变化字段。服务端会先读取微信端最新内容再合并，减少覆盖人工编辑的风险。

## 5. 删除语义

- 永久素材调用微信 `/cgi-bin/material/del_material`，成功后删除本地记录；微信返回 `40007` 时也清理本地记录。
- 正文图片没有微信删除接口，只清理本地历史记录，原 URL 可能继续可访问。
- 临时图片删除服务器文件和数据库记录。
- 草稿调用微信 `/cgi-bin/draft/delete` 后删除本地任务；远端已不存在时仍清理本地记录。

## 6. 错误处理

| HTTP | 含义 |
|---|---|
| `401` | 验证码无效、过期、已使用，或客户端令牌无效 |
| `404` | 公众号、素材或草稿不存在 |
| `409` | 幂等冲突或远端执行结果待核对 |
| `413` | 数量或文件大小超限 |
| `422` | 请求字段或文章内容无效 |
| `502` | 微信接口失败或结果无法确认 |
| `503` | 公众号凭据尚未配置 |

写操作遇到超时、`502`、`unknown` 或其他无法确认的结果时不得自动重试；先检查控制台和真实微信草稿箱。
