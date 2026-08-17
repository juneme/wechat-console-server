import asyncio
import json

import httpx

from app.wechat import WechatClient


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
