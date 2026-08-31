from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import httpx

from .article import normalize_article_image_url

TOKEN_ERROR_CODES = {40001, 40014, 42001}


class WechatAPIError(RuntimeError):
    def __init__(
        self, errcode: int | None, errmsg: str, *, ambiguous: bool = False
    ):
        self.errcode = errcode
        self.errmsg = errmsg
        self.ambiguous = ambiguous
        detail = f"微信接口错误 {errcode}: {errmsg}" if errcode is not None else errmsg
        super().__init__(detail)


@dataclass
class CachedToken:
    value: str
    expires_at: float


class WechatClient:
    def __init__(self, app_id: str, app_secret: str, http: httpx.AsyncClient):
        self.app_id = app_id
        self.app_secret = app_secret
        self.http = http
        self._token: CachedToken | None = None
        self._token_lock = asyncio.Lock()

    async def get_token(self, *, refresh: bool = False) -> str:
        if not refresh and self._token and self._token.expires_at > time.monotonic():
            return self._token.value
        async with self._token_lock:
            if (
                not refresh
                and self._token
                and self._token.expires_at > time.monotonic()
            ):
                return self._token.value
            try:
                response = await self.http.post(
                    "https://api.weixin.qq.com/cgi-bin/stable_token",
                    json={
                        "grant_type": "client_credential",
                        "appid": self.app_id,
                        "secret": self.app_secret,
                        "force_refresh": False,
                    },
                )
                response.raise_for_status()
                payload = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                raise WechatAPIError(
                    None, "获取微信 access_token 时网络请求失败"
                ) from exc
            if not payload.get("access_token"):
                raise WechatAPIError(
                    payload.get("errcode"), payload.get("errmsg", "获取 token 失败")
                )
            expires_in = max(int(payload.get("expires_in", 7200)), 60)
            self._token = CachedToken(
                value=payload["access_token"],
                expires_at=time.monotonic() + max(expires_in - 300, 30),
            )
            return self._token.value

    async def _upload(
        self,
        endpoint: str,
        *,
        filename: str,
        content_type: str,
        data: bytes,
    ) -> dict[str, Any]:
        for attempt in range(2):
            token = await self.get_token(refresh=attempt > 0)
            try:
                response = await self.http.post(
                    endpoint,
                    params={"access_token": token},
                    files={"media": (filename, data, content_type)},
                )
                response.raise_for_status()
                payload = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                raise WechatAPIError(None, "上传图片到微信时网络请求失败") from exc
            errcode = payload.get("errcode")
            if errcode in TOKEN_ERROR_CODES and attempt == 0:
                self._token = None
                continue
            if errcode not in (None, 0):
                raise WechatAPIError(errcode, payload.get("errmsg", "上传失败"))
            return payload
        raise WechatAPIError(None, "刷新 access_token 后上传仍然失败")

    async def _post_json(
        self, endpoint: str, *, payload: dict[str, Any]
    ) -> dict[str, Any]:
        for attempt in range(2):
            token = await self.get_token(refresh=attempt > 0)
            try:
                response = await self.http.post(
                    endpoint,
                    params={"access_token": token},
                    json=payload,
                )
                response.raise_for_status()
                result = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                raise WechatAPIError(
                    None,
                    "调用微信接口时网络请求失败，远端是否已执行无法确认",
                    ambiguous=True,
                ) from exc
            errcode = result.get("errcode")
            if errcode in TOKEN_ERROR_CODES and attempt == 0:
                self._token = None
                continue
            if errcode not in (None, 0):
                raise WechatAPIError(
                    errcode, result.get("errmsg", "微信接口调用失败")
                )
            return result
        raise WechatAPIError(None, "刷新 access_token 后调用微信接口仍然失败")

    async def upload_permanent_image(
        self, *, filename: str, content_type: str, data: bytes
    ) -> dict[str, str]:
        payload = await self._upload(
            "https://api.weixin.qq.com/cgi-bin/material/add_material?type=image",
            filename=filename,
            content_type=content_type,
            data=data,
        )
        media_id = payload.get("media_id")
        if not media_id:
            raise WechatAPIError(None, "微信未返回永久素材 media_id")
        return {"media_id": media_id, "url": payload.get("url", "")}

    async def upload_article_image(
        self, *, filename: str, content_type: str, data: bytes
    ) -> str:
        payload = await self._upload(
            "https://api.weixin.qq.com/cgi-bin/media/uploadimg",
            filename=filename,
            content_type=content_type,
            data=data,
        )
        url = payload.get("url")
        if not url:
            raise WechatAPIError(None, "微信未返回正文图片 URL")
        return normalize_article_image_url(str(url))

    async def delete_permanent_material(self, media_id: str) -> None:
        await self._post_json(
            "https://api.weixin.qq.com/cgi-bin/material/del_material",
            payload={"media_id": media_id},
        )

    async def create_draft(self, article: dict[str, Any]) -> str:
        result = await self._post_json(
            "https://api.weixin.qq.com/cgi-bin/draft/add",
            payload={"articles": [article]},
        )
        media_id = result.get("media_id")
        if not media_id:
            raise WechatAPIError(None, "微信未返回草稿 media_id")
        return str(media_id)

    async def get_draft(self, media_id: str) -> dict[str, Any]:
        return await self._post_json(
            "https://api.weixin.qq.com/cgi-bin/draft/get",
            payload={"media_id": media_id},
        )

    async def list_drafts(
        self, *, offset: int = 0, count: int = 20, no_content: bool = False
    ) -> dict[str, Any]:
        return await self._post_json(
            "https://api.weixin.qq.com/cgi-bin/draft/batchget",
            payload={
                "offset": offset,
                "count": count,
                "no_content": 1 if no_content else 0,
            },
        )

    async def update_draft(
        self, media_id: str, article: dict[str, Any], *, index: int = 0
    ) -> None:
        await self._post_json(
            "https://api.weixin.qq.com/cgi-bin/draft/update",
            payload={"media_id": media_id, "index": index, "articles": article},
        )

    async def delete_draft(self, media_id: str) -> None:
        await self._post_json(
            "https://api.weixin.qq.com/cgi-bin/draft/delete",
            payload={"media_id": media_id},
        )
