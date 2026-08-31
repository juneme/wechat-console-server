import asyncio
import json

import httpx

from app.wechat import WechatClient


def test_article_image_upload_normalizes_legacy_cdn_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/cgi-bin/stable_token":
            return httpx.Response(
                200, json={"access_token": "token", "expires_in": 7200}
            )
        if request.url.path == "/cgi-bin/media/uploadimg":
            assert request.url.params["access_token"] == "token"
            return httpx.Response(
                200,
                json={
                    "url": (
                        "http://mmecoa.qpic.cn/mmecoa_jpg/example/0?from=appmsg"
                    )
                },
            )
        raise AssertionError(f"Unexpected request: {request.url}")

    async def run() -> str:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = WechatClient("wx-test", "secret-test", http)
            return await client.upload_article_image(
                filename="body.jpg", content_type="image/jpeg", data=b"image"
            )

    assert asyncio.run(run()) == (
        "https://mmbiz.qpic.cn/mmecoa_jpg/example/0?from=appmsg"
    )


def test_create_draft_refreshes_expired_token() -> None:
    tokens = iter(("token-one", "token-two"))
    draft_tokens: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/cgi-bin/stable_token":
            return httpx.Response(
                200,
                json={"access_token": next(tokens), "expires_in": 7200},
            )
        if request.url.path == "/cgi-bin/draft/add":
            draft_tokens.append(request.url.params["access_token"])
            body = json.loads(request.content)
            assert body["articles"][0]["title"] == "测试文章"
            if len(draft_tokens) == 1:
                return httpx.Response(200, json={"errcode": 42001, "errmsg": "expired"})
            return httpx.Response(200, json={"media_id": "draft-media-id"})
        raise AssertionError(f"Unexpected request: {request.url}")

    async def run() -> str:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = WechatClient("wx-test", "secret-test", http)
            return await client.create_draft(
                {
                    "title": "测试文章",
                    "content": "<section>正文</section>",
                    "thumb_media_id": "cover-id",
                }
            )

    assert asyncio.run(run()) == "draft-media-id"
    assert draft_tokens == ["token-one", "token-two"]


def test_draft_crud_uses_official_wechat_endpoints() -> None:
    requests: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/cgi-bin/stable_token":
            return httpx.Response(
                200, json={"access_token": "token", "expires_in": 7200}
            )
        payload = json.loads(request.content)
        requests.append((request.url.path, payload))
        if request.url.path == "/cgi-bin/draft/get":
            return httpx.Response(
                200, json={"news_item": [{"title": "远端标题"}]}
            )
        if request.url.path == "/cgi-bin/draft/batchget":
            return httpx.Response(
                200,
                json={
                    "total_count": 1,
                    "item_count": 1,
                    "item": [{"media_id": "draft-media-id"}],
                },
            )
        if request.url.path in {
            "/cgi-bin/draft/update",
            "/cgi-bin/draft/delete",
        }:
            return httpx.Response(200, json={"errcode": 0, "errmsg": "ok"})
        raise AssertionError(f"Unexpected request: {request.url}")

    async def run() -> dict:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = WechatClient("wx-test", "secret-test", http)
            remote = await client.get_draft("draft-media-id")
            listing = await client.list_drafts(
                offset=20, count=10, no_content=True
            )
            await client.update_draft(
                "draft-media-id",
                {"title": "修改后", "content": "<section>正文</section>"},
            )
            await client.delete_draft("draft-media-id")
            assert listing["total_count"] == 1
            return remote

    assert asyncio.run(run())["news_item"][0]["title"] == "远端标题"
    assert requests == [
        ("/cgi-bin/draft/get", {"media_id": "draft-media-id"}),
        (
            "/cgi-bin/draft/batchget",
            {"offset": 20, "count": 10, "no_content": 1},
        ),
        (
            "/cgi-bin/draft/update",
            {
                "media_id": "draft-media-id",
                "index": 0,
                "articles": {
                    "title": "修改后",
                    "content": "<section>正文</section>",
                },
            },
        ),
        ("/cgi-bin/draft/delete", {"media_id": "draft-media-id"}),
    ]
