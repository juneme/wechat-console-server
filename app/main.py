from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import secrets
import sqlite3
import time
from collections import deque
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Annotated, Literal

import httpx
from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse, Response
from fastapi.security import (
    HTTPAuthorizationCredentials,
    HTTPBearer,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__
from .article import (
    ArticleValidationError,
    normalize_article_content,
    normalize_article_image_url,
    validate_article_content,
)
from .config import Settings, get_settings
from .credentials import CredentialCipher, CredentialError
from .database import AssetStore
from .image_tools import (
    ImageValidationError,
    inspect_image,
    prepare_article_image,
    prepare_temporary_image,
)
from .passwords import hash_password, verify_password
from .wechat import WechatAPIError, WechatClient

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
bearer_security = HTTPBearer(auto_error=False)
SESSION_COOKIE = "wechat_uploader_session"
SESSION_MAX_AGE_SECONDS = 7 * 24 * 60 * 60
LOGIN_IP_FAILURE_LIMIT = 10
LOGIN_IP_WINDOW_SECONDS = 5 * 60
LOGIN_ACCOUNT_FAILURE_LIMIT = 20
LOGIN_ACCOUNT_WINDOW_SECONDS = 15 * 60
REGISTRATION_IP_LIMIT = 5
REGISTRATION_GLOBAL_LIMIT = 50
REGISTRATION_WINDOW_SECONDS = 60 * 60
SETUP_TOKEN_FILENAME = ".wechat-setup-token"
PAIRING_CODE_TTL_SECONDS = 60
PAIRING_CODE_LENGTH = 8
PAIRING_CODE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
PAIRING_FAILURE_LIMIT = 10
PAIRING_FAILURE_WINDOW_SECONDS = 5 * 60
_login_failure_buckets: dict[str, deque[float]] = {}
_registration_buckets: dict[str, deque[float]] = {}
_login_failure_lock = Lock()


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=256)


class InitialSetupRequest(BaseModel):
    setup_token: str = Field(min_length=16, max_length=256)
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=12, max_length=256)


class RegistrationRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=12, max_length=256)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=12, max_length=256)


class DeleteAssetItem(BaseModel):
    kind: Literal["wechat", "temporary"]
    id: int = Field(gt=0)


class DeleteAssetsRequest(BaseModel):
    items: list[DeleteAssetItem] = Field(min_length=1, max_length=500)


class WechatAccountUpdate(BaseModel):
    display_name: str = Field(min_length=1, max_length=80)
    account_type: Literal["subscription", "service"] = "subscription"
    app_id: str = Field(min_length=3, max_length=64)
    app_secret: str | None = Field(default=None, max_length=256)


class DraftArticleRequest(BaseModel):
    request_id: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    title: str = Field(min_length=1, max_length=32)
    author: str = Field(default="", max_length=16)
    digest: str = Field(default="", max_length=120)
    content: str = Field(min_length=1, max_length=19_999)
    content_source_url: str = Field(default="", max_length=1024)
    thumb_media_id: str = Field(min_length=1, max_length=256)
    need_open_comment: Literal[0, 1] = 0
    only_fans_can_comment: Literal[0, 1] = 0


class DraftArticleUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=32)
    author: str | None = Field(default=None, max_length=16)
    digest: str | None = Field(default=None, max_length=120)
    content: str | None = Field(default=None, min_length=1, max_length=19_999)
    content_source_url: str | None = Field(default=None, max_length=1024)
    thumb_media_id: str | None = Field(default=None, min_length=1, max_length=256)
    need_open_comment: Literal[0, 1] | None = None
    only_fans_can_comment: Literal[0, 1] | None = None


class PairingCodeExchange(BaseModel):
    code: str = Field(min_length=1, max_length=32)


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _pairing_code_hash(code: str) -> str:
    return hashlib.sha256(code.encode("ascii")).hexdigest()


def _normalize_pairing_code(code: str) -> str:
    return code.replace("-", "").replace(" ", "").upper()


def _prune_pairing_state(app_state: object, now: float) -> None:
    expired = [
        code_hash
        for code_hash, item in app_state.draft_pairing_codes.items()
        if item["expires_at"] <= now
    ]
    for code_hash in expired:
        app_state.draft_pairing_codes.pop(code_hash, None)

    cutoff = now - PAIRING_FAILURE_WINDOW_SECONDS
    empty_keys = []
    for client_key, bucket in app_state.pairing_failure_buckets.items():
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        if not bucket:
            empty_keys.append(client_key)
    for client_key in empty_keys:
        app_state.pairing_failure_buckets.pop(client_key, None)


def _pairing_client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _redeem_pairing_code(request: Request, code: str) -> int:
    normalized = _normalize_pairing_code(code)
    now = time.monotonic()
    app_state = request.app.state
    client_key = _pairing_client_key(request)
    with app_state.draft_pairing_lock:
        _prune_pairing_state(app_state, now)
        failures = app_state.pairing_failure_buckets.setdefault(client_key, deque())
        if len(failures) >= PAIRING_FAILURE_LIMIT:
            raise HTTPException(
                status_code=429,
                detail="验证码尝试次数过多，请稍后再试",
                headers={"Retry-After": str(PAIRING_FAILURE_WINDOW_SECONDS)},
            )
        valid_format = len(normalized) == PAIRING_CODE_LENGTH and all(
            character in PAIRING_CODE_ALPHABET for character in normalized
        )
        item = (
            app_state.draft_pairing_codes.pop(_pairing_code_hash(normalized), None)
            if valid_format
            else None
        )
        if item is None:
            failures.append(now)
            raise HTTPException(status_code=401, detail="验证码无效、已过期或已使用")
        app_state.pairing_failure_buckets.pop(client_key, None)
        return int(item["user_id"])


def _login_rate_keys(request: Request, username: str) -> tuple[str, str]:
    client_host = request.client.host if request.client else "unknown"
    username_hash = hashlib.sha256(username.encode("utf-8")).hexdigest()
    return f"ip:{client_host}", f"account:{username_hash}"


def _prune_login_bucket(bucket: deque[float], cutoff: float) -> None:
    while bucket and bucket[0] <= cutoff:
        bucket.popleft()


def _enforce_login_rate_limit(request: Request, username: str) -> None:
    now = time.monotonic()
    ip_key, account_key = _login_rate_keys(request, username)
    limits = (
        (ip_key, LOGIN_IP_FAILURE_LIMIT, LOGIN_IP_WINDOW_SECONDS),
        (
            account_key,
            LOGIN_ACCOUNT_FAILURE_LIMIT,
            LOGIN_ACCOUNT_WINDOW_SECONDS,
        ),
    )
    retry_after = 0
    with _login_failure_lock:
        for key, limit, window in limits:
            bucket = _login_failure_buckets.get(key)
            if not bucket:
                continue
            _prune_login_bucket(bucket, now - window)
            if not bucket:
                _login_failure_buckets.pop(key, None)
                continue
            if len(bucket) >= limit:
                retry_after = max(retry_after, int(bucket[0] + window - now) + 1)
    if retry_after:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="登录失败次数过多，请稍后再试",
            headers={"Retry-After": str(retry_after)},
        )


def _record_login_failure(request: Request, username: str) -> None:
    now = time.monotonic()
    with _login_failure_lock:
        for key in _login_rate_keys(request, username):
            _login_failure_buckets.setdefault(key, deque()).append(now)


def _clear_login_failures(request: Request, username: str) -> None:
    with _login_failure_lock:
        for key in _login_rate_keys(request, username):
            _login_failure_buckets.pop(key, None)


def _consume_registration_attempt(request: Request) -> None:
    now = time.monotonic()
    client_host = request.client.host if request.client else "unknown"
    limits = (
        ("global", REGISTRATION_GLOBAL_LIMIT),
        (f"ip:{client_host}", REGISTRATION_IP_LIMIT),
    )
    with _login_failure_lock:
        for key, limit in limits:
            bucket = _registration_buckets.setdefault(key, deque())
            _prune_login_bucket(bucket, now - REGISTRATION_WINDOW_SECONDS)
            if len(bucket) >= limit:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="注册请求过于频繁，请稍后再试",
                    headers={"Retry-After": str(REGISTRATION_WINDOW_SECONDS)},
                )
        for key, _ in limits:
            _registration_buckets[key].append(now)


def _load_or_create_setup_token(database_path: Path) -> str:
    token_path = database_path.parent / SETUP_TOKEN_FILENAME
    try:
        existing = token_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        existing = ""
    if len(existing) >= 16:
        return existing
    token = secrets.token_urlsafe(24)
    token_path.write_text(token + "\n", encoding="utf-8")
    token_path.chmod(0o600)
    return token


def _clear_setup_token(app: FastAPI) -> None:
    app.state.setup_token = None
    token_path = app.state.settings.database_path.parent / SETUP_TOKEN_FILENAME
    with suppress(FileNotFoundError, OSError):
        token_path.unlink()


def _require_auth(
    request: Request,
) -> dict:
    token = request.cookies.get(SESSION_COOKIE, "")
    session = None
    if token:
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        store: AssetStore = request.app.state.store
        session = store.get_admin_session(token_hash)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="请先登录",
        )
    user_id = session.get("user_id")
    store: AssetStore = request.app.state.store
    user = store.get_user_by_id(int(user_id)) if user_id is not None else None
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="登录已失效")
    request.state.current_user = user
    return user


def _require_admin(
    user: Annotated[dict, Depends(_require_auth)],
) -> dict:
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可执行此操作")
    return user


def _create_admin_session(
    request: Request, response: Response, *, user: dict
) -> None:
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    expires_at = (
        datetime.now(UTC) + timedelta(seconds=SESSION_MAX_AGE_SECONDS)
    ).isoformat(timespec="seconds")
    store: AssetStore = request.app.state.store
    store.create_admin_session(
        token_hash=token_hash,
        user_id=int(user["id"]),
        username=str(user["username"]),
        expires_at=expires_at,
    )
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        max_age=SESSION_MAX_AGE_SECONDS,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="lax",
        path="/",
    )


def _require_ajax(request: Request) -> None:
    if (
        request.headers.get("X-Requested-With") != "WechatUploader"
        and not getattr(request.state, "api_key_verified", False)
    ):
        raise HTTPException(
            status_code=403, detail="Missing request verification header"
        )


def _require_client_token(
    request: Request,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(bearer_security)
    ],
) -> str:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="缺少或无效的客户端令牌",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token_hash = hashlib.sha256(credentials.credentials.encode("utf-8")).hexdigest()
    token = request.app.state.store.get_client_token(token_hash)
    if token is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="客户端令牌无效，请使用新的验证码重新配对",
            headers={"WWW-Authenticate": "Bearer"},
        )
    request.state.api_key_verified = True
    request.state.api_user_id = int(token["user_id"])
    return "client-token"


def _ai_upload_result(result: dict) -> dict:
    asset = result.get("asset") or {}
    article_url = (
        normalize_article_image_url(str(asset["article_url"]))
        if asset.get("article_url")
        else None
    )
    return {
        "filename": result.get("filename"),
        "url": article_url or asset.get("url"),
        "size": asset.get("processed_bytes") or asset.get("original_bytes"),
        "uploaded_at": asset.get("updated_at") or asset.get("created_at"),
        "status": result.get("status"),
        "media_id": asset.get("media_id"),
        "material_url": asset.get("material_url"),
        "article_url": article_url,
        "width": asset.get("width"),
        "height": asset.get("height"),
        "sha256": asset.get("sha256"),
        "errors": result.get("errors", []),
    }


def _public_asset(row: dict) -> dict:
    result = {
        key: row.get(key)
        for key in (
            "id",
            "sha256",
            "filename",
            "content_type",
            "original_bytes",
            "processed_bytes",
            "width",
            "height",
            "media_id",
            "material_url",
            "article_url",
            "last_error",
            "created_at",
            "updated_at",
        )
    }
    article_url = (
        normalize_article_image_url(str(row["article_url"]))
        if row.get("article_url")
        else None
    )
    result["article_url"] = article_url
    result["kind"] = "wechat"
    result["url"] = article_url or row.get("material_url")
    return result


def _csv_safe(value: object) -> object:
    if not isinstance(value, str):
        return value
    return f"'{value}" if value.startswith(("=", "+", "-", "@")) else value


def _safe_upload_filename(value: str | None) -> str:
    normalized = (value or "image").replace("\\", "/")
    return normalized.rsplit("/", 1)[-1] or "image"


def _temporary_url(request: Request, settings: Settings, token: str) -> str:
    if settings.public_base_url:
        return f"{settings.public_base_url}/temp/{token}"
    return str(request.url_for("get_temporary_image", token=token))


def _public_temporary_asset(row: dict, request: Request, settings: Settings) -> dict:
    url = _temporary_url(request, settings, row["token"])
    return {
        "id": row.get("id"),
        "kind": "temporary",
        "token": row.get("token"),
        "sha256": row.get("sha256"),
        "filename": row.get("filename"),
        "content_type": row.get("content_type"),
        "original_bytes": row.get("original_bytes"),
        "processed_bytes": row.get("processed_bytes"),
        "width": row.get("width"),
        "height": row.get("height"),
        "temporary_url": url,
        "url": url,
        "created_at": row.get("created_at"),
        "updated_at": row.get("created_at"),
        "expires_at": row.get("expires_at"),
    }


def _temporary_asset_path(row: dict, storage_path: Path) -> Path | None:
    storage_root = storage_path.resolve()
    candidate = (storage_root / row["stored_name"]).resolve()
    return candidate if candidate.parent == storage_root else None


def _delete_temporary_row(
    store: AssetStore, row: dict, storage_path: Path
) -> None:
    candidate = _temporary_asset_path(row, storage_path)
    if candidate is None:
        raise RuntimeError("临时图片存储路径无效")
    try:
        candidate.unlink()
    except FileNotFoundError:
        pass
    store.delete_temporary_asset(row["id"])


def _cleanup_expired_temporary_assets(store: AssetStore, storage_path: Path) -> int:
    storage_path.mkdir(parents=True, exist_ok=True)
    expired = store.list_expired_temporary_assets()
    deleted = 0
    for row in expired:
        try:
            _delete_temporary_row(store, row, storage_path)
        except OSError:
            continue
        deleted += 1
    return deleted


async def _temporary_cleanup_loop(app: FastAPI) -> None:
    settings: Settings = app.state.settings
    while True:
        async with app.state.temporary_storage_lock:
            await asyncio.to_thread(
                _cleanup_expired_temporary_assets,
                app.state.store,
                settings.temp_storage_path,
            )
        await asyncio.sleep(settings.temp_cleanup_interval_seconds)


async def _store_temporary_image(
    *,
    request: Request,
    settings: Settings,
    filename: str,
    data: bytes,
) -> dict:
    prepared = await asyncio.to_thread(
        prepare_temporary_image,
        data,
        filename,
        max_bytes=settings.temp_max_bytes,
        max_dimension=settings.article_max_dimension,
    )
    token = secrets.token_urlsafe(24)
    extension = Path(prepared.filename).suffix.lower() or ".jpg"
    stored_name = f"{token}{extension}"
    store: AssetStore = request.app.state.store
    user_id = _request_user_id(request)
    async with request.app.state.temporary_storage_lock:
        used_bytes = await asyncio.to_thread(store.temporary_storage_bytes)
        if used_bytes + len(prepared.data) > settings.temp_storage_max_bytes:
            raise ImageValidationError("服务器临时图片存储空间已达到上限，请先删除旧图片")
        user_used_bytes = await asyncio.to_thread(
            store.temporary_storage_bytes, user_id=user_id
        )
        if user_used_bytes + len(prepared.data) > (
            settings.temp_user_storage_max_bytes
        ):
            raise ImageValidationError("当前用户临时图片存储空间已达到上限，请先删除旧图片")
        storage_path = settings.temp_storage_path
        storage_path.mkdir(parents=True, exist_ok=True)
        target = storage_path / stored_name
        await asyncio.to_thread(target.write_bytes, prepared.data)
        expires_at = (
            datetime.now(UTC) + timedelta(days=settings.temp_retention_days)
        ).isoformat(timespec="seconds")
        try:
            row = store.create_temporary_asset(
                user_id=user_id,
                token=token,
                sha256=hashlib.sha256(data).hexdigest(),
                filename=filename,
                stored_name=stored_name,
                content_type=prepared.content_type,
                original_bytes=len(data),
                processed_bytes=len(prepared.data),
                width=prepared.width,
                height=prepared.height,
                expires_at=expires_at,
            )
        except Exception:
            with suppress(FileNotFoundError, OSError):
                target.unlink()
            raise
    return _public_temporary_asset(row, request, settings)


def _request_user_id(request: Request) -> int:
    user = getattr(request.state, "current_user", None)
    if user:
        return int(user["id"])
    api_user_id = getattr(request.state, "api_user_id", None)
    if api_user_id is not None:
        return int(api_user_id)
    raise HTTPException(status_code=401, detail="无法确定当前用户")


def _request_account(request: Request, *, required: bool = False) -> dict | None:
    store: AssetStore = request.app.state.store
    user_id = _request_user_id(request)
    header = request.headers.get("X-Wechat-Account-ID", "").strip()
    if header:
        try:
            account_id = int(header)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="公众号 ID 格式不正确") from exc
        row = store.get_official_account(account_id, user_id=user_id)
        if row is None:
            raise HTTPException(status_code=404, detail="公众号不存在")
    else:
        row = store.get_active_official_account(user_id)
    if row is None and required:
        raise HTTPException(
            status_code=503,
            detail="微信 AppID/AppSecret 尚未配置，请先在公众号设置中完成配置",
        )
    return row


def _wechat_client_for_account(request: Request, row: dict) -> WechatClient:
    override = getattr(request.app.state, "wechat", None)
    if override is not None:
        return override
    account_id = int(row["id"])
    cache: dict[int, tuple[str, WechatClient]] = request.app.state.wechat_clients
    cached = cache.get(account_id)
    if cached and cached[0] == row["updated_at"]:
        return cached[1]
    cipher: CredentialCipher = request.app.state.credential_cipher
    try:
        app_secret = cipher.decrypt(row["app_secret_ciphertext"])
    except CredentialError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    client = WechatClient(row["app_id"], app_secret, request.app.state.http)
    cache[account_id] = (row["updated_at"], client)
    return client


def _require_wechat_config(request: Request) -> WechatClient:
    row = _request_account(request, required=True)
    assert row is not None
    return _wechat_client_for_account(request, row)


def _public_account(request: Request, row: dict | None = None) -> dict:
    row = row if row is not None else _request_account(request)
    config_error = None
    if row:
        try:
            request.app.state.credential_cipher.decrypt(row["app_secret_ciphertext"])
        except CredentialError as exc:
            config_error = str(exc)
    return {
        "id": row.get("id") if row else None,
        "display_name": row.get("display_name", "未命名公众号") if row else "未命名公众号",
        "account_type": row.get("account_type", "subscription") if row else "subscription",
        "app_id": row.get("app_id", "") if row else "",
        "app_id_suffix": row["app_id"][-6:] if row and row.get("app_id") else "",
        "secret_configured": bool(row and row.get("app_secret_ciphertext")),
        "source": "console" if row else "none",
        "updated_at": row.get("updated_at") if row else None,
        "encryption": request.app.state.credential_cipher.source,
        "config_error": config_error,
    }


def _save_official_account(
    payload: WechatAccountUpdate,
    request: Request,
    user: dict,
    *,
    account_id: int | None = None,
) -> dict:
    display_name = payload.display_name.strip()
    app_id = payload.app_id.strip()
    supplied_secret = (payload.app_secret or "").strip()
    if not display_name or not app_id:
        raise HTTPException(status_code=422, detail="公众号名称和 AppID 不能为空")
    if supplied_secret and len(supplied_secret) < 8:
        raise HTTPException(status_code=422, detail="AppSecret 格式不正确")
    store: AssetStore = request.app.state.store
    user_id = int(user["id"])
    current = (
        store.get_official_account(account_id, user_id=user_id)
        if account_id is not None
        else None
    )
    if account_id is not None and current is None:
        raise HTTPException(status_code=404, detail="公众号不存在")
    if supplied_secret:
        ciphertext = request.app.state.credential_cipher.encrypt(supplied_secret)
    elif current:
        ciphertext = current["app_secret_ciphertext"]
    else:
        raise HTTPException(status_code=422, detail="首次保存时必须填写 AppSecret")
    if current:
        saved = store.update_official_account(
            account_id,
            user_id=user_id,
            display_name=display_name,
            account_type=payload.account_type,
            app_id=app_id,
            app_secret_ciphertext=ciphertext,
        )
    else:
        saved = store.create_official_account(
            user_id=user_id,
            display_name=display_name,
            account_type=payload.account_type,
            app_id=app_id,
            app_secret_ciphertext=ciphertext,
        )
    if saved is None:
        raise HTTPException(status_code=409, detail="该 AppID 已存在")
    request.app.state.wechat_clients.pop(int(saved["id"]), None)
    return saved


def _public_draft(
    row: dict,
    *,
    include_content: bool = False,
    current_user_id: int | None = None,
) -> dict:
    result = {
        key: row.get(key)
        for key in (
            "id",
            "request_id",
            "title",
            "author",
            "digest",
            "content_source_url",
            "thumb_media_id",
            "media_id",
            "status",
            "last_error",
            "created_at",
            "updated_at",
            "owner_username",
            "account_display_name",
        )
    }
    result["content_characters"] = len(row.get("content") or "")
    result["can_delete"] = (
        current_user_id is not None
        and int(row.get("user_id") or 0) == current_user_id
    )
    if include_content:
        result["content"] = row.get("content") or ""
    return result


def _draft_article_data(source: dict) -> dict:
    return {
        "title": source.get("title") or "",
        "author": source.get("author") or "",
        "digest": source.get("digest") or "",
        "content": source.get("content") or "",
        "content_source_url": source.get("content_source_url") or "",
        "thumb_media_id": source.get("thumb_media_id") or "",
        "need_open_comment": int(source.get("need_open_comment") or 0),
        "only_fans_can_comment": int(source.get("only_fans_can_comment") or 0),
    }


def _draft_content_hash(article_data: dict) -> str:
    return hashlib.sha256(
        json.dumps(
            article_data, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def _wechat_draft_article(
    article_data: dict,
    *,
    preserve_from: dict | None = None,
    include_empty_optional: bool = False,
) -> dict:
    article = {
        "article_type": "news",
        "title": article_data["title"],
        "content": article_data["content"],
        "thumb_media_id": article_data["thumb_media_id"],
        "need_open_comment": article_data["need_open_comment"],
        "only_fans_can_comment": article_data["only_fans_can_comment"],
    }
    if preserve_from:
        for key in (
            "show_cover_pic",
            "pic_crop_235_1",
            "pic_crop_1_1",
            "image_info",
            "product_info",
        ):
            if key in preserve_from:
                article[key] = preserve_from[key]
    for key in ("author", "digest", "content_source_url"):
        if article_data[key] or include_empty_optional:
            article[key] = article_data[key]
    return article


def _first_remote_draft_article(payload: dict) -> dict:
    news_items = payload.get("news_item")
    if not isinstance(news_items, list):
        content = payload.get("content")
        news_items = content.get("news_item") if isinstance(content, dict) else None
    if not news_items or not isinstance(news_items[0], dict):
        raise HTTPException(status_code=502, detail="微信草稿未返回文章内容")
    return news_items[0]


async def _get_remote_draft_or_404(
    client: WechatClient, media_id: str
) -> dict:
    try:
        return await client.get_draft(media_id)
    except WechatAPIError as exc:
        if exc.errcode == 40007:
            raise HTTPException(status_code=404, detail="微信草稿不存在") from exc
        raise HTTPException(status_code=502, detail=str(exc)) from exc


async def _update_remote_draft_article(
    *,
    client: WechatClient,
    media_id: str,
    changes: dict,
) -> tuple[dict, dict]:
    remote = await _get_remote_draft_or_404(client, media_id)
    remote_article = _first_remote_draft_article(remote)
    if remote_article.get("article_type", "news") != "news":
        raise HTTPException(status_code=409, detail="当前仅支持修改普通图文草稿")
    article_data = _draft_article_data(remote_article)
    article_data.update(changes)
    article_data["content"] = normalize_article_content(article_data["content"])
    try:
        validation = validate_article_content(article_data["content"])
    except ArticleValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        await client.update_draft(
            media_id,
            _wechat_draft_article(
                article_data,
                preserve_from=remote_article,
                include_empty_optional=True,
            ),
            index=0,
        )
    except WechatAPIError as exc:
        if exc.errcode == 40007:
            raise HTTPException(status_code=404, detail="微信草稿不存在") from exc
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return article_data, validation


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    settings.validate_runtime()
    store = AssetStore(settings.database_path)
    store.initialize()
    store.purge_deleted_draft_jobs()
    store.delete_expired_admin_sessions()
    cipher = CredentialCipher.create(
        secret=settings.credentials_encryption_key,
        key_path=settings.database_path.parent / ".wechat-credentials.key",
    )
    if store.get_admin_credentials() is None and settings.admin_password:
        password_hash = await asyncio.to_thread(
            hash_password, settings.admin_password
        )
        store.initialize_admin_credentials(
            settings.admin_username or "admin", password_hash
        )
    settings.temp_storage_path.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(
        _cleanup_expired_temporary_assets, store, settings.temp_storage_path
    )
    http = httpx.AsyncClient(timeout=httpx.Timeout(90.0, connect=15.0))
    app.state.store = store
    app.state.settings = settings
    app.state.http = http
    app.state.credential_cipher = cipher
    app.state.temporary_storage_lock = asyncio.Lock()
    app.state.draft_pairing_codes = {}
    app.state.draft_pairing_lock = Lock()
    app.state.pairing_failure_buckets = {}
    if store.get_admin_credentials() is None:
        app.state.setup_token = _load_or_create_setup_token(settings.database_path)
    else:
        app.state.setup_token = None
        stale_token_path = settings.database_path.parent / SETUP_TOKEN_FILENAME
        with suppress(FileNotFoundError, OSError):
            stale_token_path.unlink()
    admin = store.get_admin_user()
    if admin and not store.list_official_accounts(int(admin["id"])) and settings.wechat_configured:
        store.create_official_account(
            user_id=int(admin["id"]),
            display_name="环境配置公众号",
            account_type="subscription",
            app_id=settings.wechat_app_id,
            app_secret_ciphertext=cipher.encrypt(settings.wechat_app_secret),
        )
    app.state.wechat = None
    app.state.wechat_clients = {}
    cleanup_task = asyncio.create_task(_temporary_cleanup_loop(app))
    try:
        yield
    finally:
        cleanup_task.cancel()
        with suppress(asyncio.CancelledError):
            await cleanup_task
        await http.aclose()


app = FastAPI(
    title="云浪控制台",
    version=__version__,
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/healthz", include_in_schema=False)
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/setup/status")
async def setup_status(request: Request, response: Response) -> dict[str, bool]:
    response.headers["Cache-Control"] = "no-store"
    store: AssetStore = request.app.state.store
    configured = store.get_admin_credentials() is not None
    return {"configured": configured, "requires_token": not configured}


@app.post("/api/setup")
async def initial_setup(
    payload: InitialSetupRequest,
    request: Request,
    response: Response,
) -> dict:
    _require_ajax(request)
    store: AssetStore = request.app.state.store
    if store.get_admin_credentials() is not None:
        raise HTTPException(status_code=409, detail="控制台已经完成初始化")
    expected_token = request.app.state.setup_token
    supplied_token = payload.setup_token.strip()
    if not expected_token or not secrets.compare_digest(
        supplied_token, expected_token
    ):
        raise HTTPException(status_code=403, detail="初始化码不正确")
    username = payload.username.strip()
    if not username:
        raise HTTPException(status_code=422, detail="管理员账号不能为空")
    if payload.password.startswith("replace-with-"):
        raise HTTPException(status_code=422, detail="不能使用示例占位密码")

    password_hash = await asyncio.to_thread(hash_password, payload.password)
    created = store.initialize_admin_credentials(username, password_hash)
    if not created:
        raise HTTPException(status_code=409, detail="控制台已经完成初始化")

    user = store.get_user_by_username(username)
    if user is None:
        raise HTTPException(status_code=500, detail="管理员初始化失败")
    _create_admin_session(request, response, user=user)
    _clear_setup_token(request.app)
    return {
        "configured": True,
        "user": {"username": user["username"], "role": user["role"]},
    }


@app.post("/api/auth/register")
async def register(
    payload: RegistrationRequest,
    request: Request,
    response: Response,
) -> dict:
    _require_ajax(request)
    store: AssetStore = request.app.state.store
    if store.get_admin_user() is None:
        raise HTTPException(status_code=409, detail="请先完成控制台初始化")
    username = payload.username.strip()
    if not username:
        raise HTTPException(status_code=422, detail="用户名不能为空")
    if payload.password.startswith("replace-with-"):
        raise HTTPException(status_code=422, detail="不能使用示例占位密码")
    _consume_registration_attempt(request)
    if store.count_users() >= request.app.state.settings.max_users:
        raise HTTPException(status_code=403, detail="用户数量已达到服务器上限")
    password_hash = await asyncio.to_thread(hash_password, payload.password)
    user = store.create_user(
        username=username,
        password_hash=password_hash,
        max_users=request.app.state.settings.max_users,
    )
    if user is None:
        if store.count_users() >= request.app.state.settings.max_users:
            raise HTTPException(status_code=403, detail="用户数量已达到服务器上限")
        raise HTTPException(status_code=409, detail="用户名已被注册")
    _create_admin_session(request, response, user=user)
    return {"user": {"username": user["username"], "role": user["role"]}}


@app.post("/api/auth/login")
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
) -> dict:
    _require_ajax(request)
    store: AssetStore = request.app.state.store
    if store.get_admin_user() is None:
        raise HTTPException(status_code=409, detail="请先完成控制台初始化")
    user = store.get_user_by_username(payload.username.strip())
    rate_username = str(user["username"]) if user else "<invalid>"
    _enforce_login_rate_limit(request, rate_username)
    verification_user = user or store.get_admin_user()
    password_valid = await asyncio.to_thread(
        verify_password, str(verification_user["password_hash"]), payload.password
    )
    if user is None or not password_valid:
        _record_login_failure(request, rate_username)
        raise HTTPException(status_code=401, detail="用户名或密码不正确")
    _clear_login_failures(request, rate_username)

    _create_admin_session(request, response, user=user)
    return {"user": {"username": user["username"], "role": user["role"]}}


@app.get("/api/auth/me")
async def current_user(
    user: Annotated[dict, Depends(_require_auth)],
) -> dict:
    return {"user": {"username": user["username"], "role": user["role"]}}


@app.post("/api/auth/logout")
async def logout(
    request: Request,
    response: Response,
    _: Annotated[dict, Depends(_require_auth)],
) -> dict[str, bool]:
    _require_ajax(request)
    token = request.cookies.get(SESSION_COOKIE, "")
    if token:
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        store: AssetStore = request.app.state.store
        store.delete_admin_session(token_hash)
    response.delete_cookie(SESSION_COOKIE, path="/", samesite="lax")
    return {"logged_out": True}


@app.post("/api/auth/password")
async def change_password(
    payload: ChangePasswordRequest,
    request: Request,
    response: Response,
    user: Annotated[dict, Depends(_require_auth)],
) -> dict[str, int | bool]:
    _require_ajax(request)
    if payload.new_password.startswith("replace-with-"):
        raise HTTPException(status_code=422, detail="不能使用示例占位密码")
    store: AssetStore = request.app.state.store
    username = str(user["username"])
    _enforce_login_rate_limit(request, username)
    current_valid = await asyncio.to_thread(
        verify_password, str(user["password_hash"]), payload.current_password
    )
    if not current_valid:
        _record_login_failure(request, username)
        raise HTTPException(status_code=400, detail="当前密码不正确")
    _clear_login_failures(request, username)
    if secrets.compare_digest(
        payload.current_password.encode("utf-8"),
        payload.new_password.encode("utf-8"),
    ):
        raise HTTPException(status_code=422, detail="新密码不能与当前密码相同")

    password_hash = await asyncio.to_thread(hash_password, payload.new_password)
    revoked = store.change_user_password_hash(int(user["id"]), password_hash)
    response.delete_cookie(SESSION_COOKIE, path="/", samesite="lax")
    return {"password_changed": True, "sessions_revoked": revoked}


@app.get("/api/status")
async def api_status(
    request: Request,
    _: Annotated[dict, Depends(_require_auth)],
    settings: Annotated[Settings, Depends(_settings)],
) -> dict:
    account = _public_account(request)
    return {
        "ready": bool(account["app_id"] and account["secret_configured"])
        and not account["config_error"],
        "app_id_suffix": account["app_id_suffix"],
        "account": account,
        "temporary_ready": True,
        "client_api_ready": True,
        "temporary_retention_days": settings.temp_retention_days,
        "limits": {
            "article_bytes": settings.article_max_bytes,
            "permanent_bytes": settings.permanent_max_bytes,
            "temporary_bytes": settings.temp_max_bytes,
        },
    }


@app.post("/api/pairing-code")
async def create_pairing_code(
    request: Request,
    response: Response,
    user: Annotated[dict, Depends(_require_admin)],
    settings: Annotated[Settings, Depends(_settings)],
) -> dict:
    _require_ajax(request)
    raw_code = "".join(
        secrets.choice(PAIRING_CODE_ALPHABET) for _ in range(PAIRING_CODE_LENGTH)
    )
    now = time.monotonic()
    expires_at = datetime.now(UTC) + timedelta(seconds=PAIRING_CODE_TTL_SECONDS)
    app_state = request.app.state
    with app_state.draft_pairing_lock:
        _prune_pairing_state(app_state, now)
        for code_hash, item in list(app_state.draft_pairing_codes.items()):
            if item["user_id"] == int(user["id"]):
                app_state.draft_pairing_codes.pop(code_hash, None)
        app_state.draft_pairing_codes[_pairing_code_hash(raw_code)] = {
            "expires_at": now + PAIRING_CODE_TTL_SECONDS,
            "user_id": int(user["id"]),
        }

    console_url = settings.public_base_url or str(request.base_url).rstrip("/")
    secure = request.url.scheme == "https"
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["Pragma"] = "no-cache"
    return {
        "code": f"{raw_code[:4]}-{raw_code[4:]}",
        "expires_in": PAIRING_CODE_TTL_SECONDS,
        "expires_at": expires_at.isoformat(timespec="seconds"),
        "exchange_url": f"{console_url}/api/v1/pairing/exchange",
        "transport_secure": secure,
        "warning": None
        if secure
        else "当前使用 HTTP，验证码和客户端令牌将以明文传输，建议配置 HTTPS。",
    }


@app.post("/api/v1/pairing/exchange")
async def exchange_pairing_code(
    payload: PairingCodeExchange,
    request: Request,
    response: Response,
    settings: Annotated[Settings, Depends(_settings)],
) -> dict:
    user_id = _redeem_pairing_code(request, payload.code)
    store: AssetStore = request.app.state.store
    user = store.get_user_by_id(user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="验证码所属用户不存在")
    client_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(client_token.encode("utf-8")).hexdigest()
    store.create_client_token(token_hash=token_hash, user_id=user_id)
    console_url = settings.public_base_url or str(request.base_url).rstrip("/")
    secure = request.url.scheme == "https"
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["Pragma"] = "no-cache"
    return {
        "console_url": console_url,
        "client_token": client_token,
        "transport_secure": secure,
        "warning": None
        if secure
        else "当前使用 HTTP，客户端令牌已通过明文连接返回，请勿在不可信网络中使用。",
    }


@app.get(
    "/api/v1/account",
    summary="读取客户端令牌当前的公众号上下文",
)
async def api_get_account_context(
    request: Request,
    response: Response,
    _: Annotated[str, Depends(_require_client_token)],
) -> dict:
    store: AssetStore = request.app.state.store
    user_id = _request_user_id(request)
    user = store.get_user_by_id(user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="客户端令牌所属用户不存在")
    account = _request_account(request)
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["Pragma"] = "no-cache"
    return {
        "active_account_id": user.get("active_account_id"),
        "account": _public_account(request, account),
    }


@app.get("/api/account")
async def get_account(
    request: Request,
    _: Annotated[dict, Depends(_require_auth)],
) -> dict:
    return {"account": _public_account(request)}


@app.get("/api/accounts")
async def list_accounts(
    request: Request,
    user: Annotated[dict, Depends(_require_auth)],
) -> dict:
    store: AssetStore = request.app.state.store
    user_id = int(user["id"])
    refreshed = store.get_user_by_id(user_id) or user
    items = [
        _public_account(request, row)
        for row in store.list_official_accounts(user_id)
    ]
    return {
        "items": items,
        "count": len(items),
        "active_account_id": refreshed.get("active_account_id"),
    }


@app.post("/api/accounts", status_code=status.HTTP_201_CREATED)
async def create_account(
    payload: WechatAccountUpdate,
    request: Request,
    user: Annotated[dict, Depends(_require_auth)],
) -> dict:
    _require_ajax(request)
    store: AssetStore = request.app.state.store
    if len(store.list_official_accounts(int(user["id"]))) >= (
        request.app.state.settings.max_accounts_per_user
    ):
        raise HTTPException(status_code=403, detail="公众号数量已达到当前用户上限")
    saved = _save_official_account(payload, request, user)
    store.set_active_official_account(int(user["id"]), int(saved["id"]))
    return {"account": _public_account(request, saved)}


@app.put("/api/accounts/{account_id}")
async def update_official_account(
    account_id: int,
    payload: WechatAccountUpdate,
    request: Request,
    user: Annotated[dict, Depends(_require_auth)],
) -> dict:
    _require_ajax(request)
    saved = _save_official_account(
        payload, request, user, account_id=account_id
    )
    return {"account": _public_account(request, saved)}


@app.post("/api/accounts/{account_id}/activate")
async def activate_official_account(
    account_id: int,
    request: Request,
    user: Annotated[dict, Depends(_require_auth)],
) -> dict:
    _require_ajax(request)
    store: AssetStore = request.app.state.store
    if not store.set_active_official_account(int(user["id"]), account_id):
        raise HTTPException(status_code=404, detail="公众号不存在")
    row = store.get_official_account(account_id, user_id=int(user["id"]))
    return {"account": _public_account(request, row)}


@app.delete("/api/accounts/{account_id}")
async def delete_official_account(
    account_id: int,
    request: Request,
    user: Annotated[dict, Depends(_require_auth)],
) -> dict[str, bool]:
    _require_ajax(request)
    store: AssetStore = request.app.state.store
    if not store.delete_official_account(account_id, user_id=int(user["id"])):
        raise HTTPException(status_code=404, detail="公众号不存在")
    request.app.state.wechat_clients.pop(account_id, None)
    return {"deleted": True}


@app.put("/api/account")
async def update_account(
    payload: WechatAccountUpdate,
    request: Request,
    user: Annotated[dict, Depends(_require_auth)],
) -> dict:
    _require_ajax(request)
    store: AssetStore = request.app.state.store
    current = store.get_active_official_account(int(user["id"]))
    saved = _save_official_account(
        payload,
        request,
        user,
        account_id=int(current["id"]) if current else None,
    )
    store.set_active_official_account(int(user["id"]), int(saved["id"]))
    return {"account": _public_account(request, saved)}


@app.get("/api/overview")
async def overview(
    request: Request,
    user: Annotated[dict, Depends(_require_auth)],
    settings: Annotated[Settings, Depends(_settings)],
) -> dict:
    store: AssetStore = request.app.state.store
    account = _request_account(request)
    account_id = int(account["id"]) if account else None
    user_id = int(user["id"])
    counts = store.overview_counts(user_id=user_id, account_id=account_id)
    draft_filters = {"user_id": user_id, "account_id": account_id}
    if user["role"] == "admin":
        global_counts = store.overview_counts()
        for key in ("drafts", "failed_drafts", "unknown_drafts"):
            counts[key] = global_counts[key]
        draft_filters = {}
    return {
        "account": _public_account(request, account),
        "counts": counts,
        "apis": {
            "wechat": bool(account and account.get("app_secret_ciphertext")),
            "client": True,
        },
        "recent_drafts": [
            _public_draft(row, current_user_id=user_id)
            for row in store.list_draft_jobs(limit=5, **draft_filters)
        ],
    }


@app.post("/api/account/test")
@app.post("/api/test-connection")
async def test_connection(
    request: Request,
    _: Annotated[dict, Depends(_require_auth)],
) -> dict[str, bool]:
    _require_ajax(request)
    client = _require_wechat_config(request)
    try:
        await client.get_token()
    except WechatAPIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"connected": True}


@app.post("/api/diagnostics/run")
async def run_diagnostics(
    request: Request,
    _: Annotated[dict, Depends(_require_auth)],
    settings: Annotated[Settings, Depends(_settings)],
) -> dict:
    _require_ajax(request)
    checks: list[dict[str, str]] = []
    store: AssetStore = request.app.state.store
    try:
        database = await asyncio.to_thread(store.health_check)
        database_ok = bool(database["ok"] and database["writable"])
        checks.append(
            {
                "id": "database",
                "label": "数据库",
                "status": "ok" if database_ok else "error",
                "detail": (
                    f"schema v{database['schema_version']} · quick_check "
                    f"{database['quick_check']} · 可写"
                ),
            }
        )
    except (OSError, sqlite3.Error, RuntimeError) as exc:
        database_ok = False
        checks.append(
            {
                "id": "database",
                "label": "数据库",
                "status": "error",
                "detail": f"检查失败：{exc}",
            }
        )

    account = _public_account(request)
    wechat_ok = False
    if account["config_error"]:
        wechat_detail = str(account["config_error"])
        wechat_status = "error"
    elif not account["app_id"] or not account["secret_configured"]:
        wechat_detail = "尚未配置 AppID/AppSecret"
        wechat_status = "warning"
    else:
        try:
            await _require_wechat_config(request).get_token()
            wechat_ok = True
            wechat_detail = f"连接成功 · AppID 尾号 {account['app_id_suffix']}"
            wechat_status = "ok"
        except WechatAPIError as exc:
            wechat_detail = str(exc)
            wechat_status = "error"
    checks.append(
        {
            "id": "wechat",
            "label": "微信连接",
            "status": wechat_status,
            "detail": wechat_detail,
        }
    )

    checks.append(
        {
            "id": "client_api",
            "label": "验证码配对",
            "status": "ok",
            "detail": "可签发统一客户端令牌",
        }
    )
    checks.append(
        {
            "id": "public_url",
            "label": "公网地址",
            "status": "ok" if settings.public_base_url else "warning",
            "detail": settings.public_base_url or "未配置，将按请求地址生成临时图片 URL",
        }
    )
    return {
        "version": __version__,
        "ready": database_ok and wechat_ok,
        "checks": checks,
        "migration_backup": (
            store.last_migration_backup.name if store.last_migration_backup else None
        ),
    }


@app.post("/api/upload")
async def upload_image(
    request: Request,
    _: Annotated[dict, Depends(_require_auth)],
    settings: Annotated[Settings, Depends(_settings)],
    mode: Annotated[Literal["article", "material", "both", "temporary"], Form()],
    image: Annotated[UploadFile, File()],
) -> dict:
    _require_ajax(request)
    user_id = _request_user_id(request)
    account = _request_account(request, required=mode != "temporary")
    account_id = int(account["id"]) if account else None
    if mode != "temporary":
        _require_wechat_config(request)
    filename = _safe_upload_filename(image.filename)
    data = await image.read(settings.max_source_bytes + 1)
    await image.close()
    if not data:
        return {"filename": filename, "status": "failed", "errors": ["文件为空"]}
    if len(data) > settings.max_source_bytes:
        return {
            "filename": filename,
            "status": "failed",
            "errors": [
                f"源文件超过服务器上限 {settings.max_source_bytes / 1_000_000:.0f}MB"
            ],
        }

    try:
        info = inspect_image(data)
    except ImageValidationError as exc:
        return {"filename": filename, "status": "failed", "errors": [str(exc)]}

    if mode == "temporary":
        await asyncio.to_thread(
            _cleanup_expired_temporary_assets,
            request.app.state.store,
            settings.temp_storage_path,
        )
        try:
            asset = await _store_temporary_image(
                request=request,
                settings=settings,
                filename=filename,
                data=data,
            )
        except ImageValidationError as exc:
            return {
                "filename": filename,
                "status": "failed",
                "errors": [str(exc)],
            }
        return {
            "filename": filename,
            "status": "complete",
            "cached": False,
            "errors": [],
            "asset": asset,
        }

    sha256 = hashlib.sha256(data).hexdigest()
    store: AssetStore = request.app.state.store
    existing = store.get_by_hash(
        sha256, user_id=user_id, account_id=account_id
    )
    row = store.upsert_source(
        user_id=user_id,
        account_id=account_id,
        sha256=sha256,
        filename=filename,
        content_type=info.content_type,
        original_bytes=len(data),
        width=info.width,
        height=info.height,
    )
    had_requested_results = bool(existing) and (
        (mode == "article" and existing.get("article_url"))
        or (mode == "material" and existing.get("media_id"))
        or (mode == "both" and existing.get("article_url") and existing.get("media_id"))
    )
    errors: list[str] = []
    client = _require_wechat_config(request)

    if mode in {"material", "both"} and not row.get("media_id"):
        if len(data) >= settings.permanent_max_bytes:
            errors.append(
                f"永久素材未上传：原图需小于 {settings.permanent_max_bytes / 1_000_000:.0f}MB"
            )
        else:
            try:
                result = await client.upload_permanent_image(
                    filename=filename,
                    content_type=info.content_type,
                    data=data,
                )
                row = store.update_result(
                    sha256,
                    user_id=user_id,
                    account_id=account_id,
                    media_id=result["media_id"],
                    material_url=result["url"],
                )
            except WechatAPIError as exc:
                errors.append(f"永久素材未上传：{exc}")

    if mode in {"article", "both"} and not row.get("article_url"):
        try:
            prepared = prepare_article_image(
                data,
                filename,
                max_bytes=settings.article_max_bytes,
                max_dimension=settings.article_max_dimension,
            )
            article_url = await client.upload_article_image(
                filename=prepared.filename,
                content_type=prepared.content_type,
                data=prepared.data,
            )
            row = store.update_result(
                sha256,
                user_id=user_id,
                account_id=account_id,
                article_url=article_url,
                processed_bytes=len(prepared.data),
            )
        except (ImageValidationError, WechatAPIError) as exc:
            errors.append(f"正文图片未上传：{exc}")

    requested_ok = (
        (mode == "article" and row.get("article_url"))
        or (mode == "material" and row.get("media_id"))
        or (mode == "both" and row.get("article_url") and row.get("media_id"))
    )
    any_ok = bool(row.get("article_url") or row.get("media_id"))
    result_status = "complete" if requested_ok else "partial" if any_ok else "failed"
    row = store.update_result(
        sha256,
        user_id=user_id,
        account_id=account_id,
        last_error="；".join(errors) if errors else None,
    )
    return {
        "filename": filename,
        "status": result_status,
        "cached": bool(had_requested_results),
        "errors": errors,
        "asset": _public_asset(row),
    }


@app.get("/api/assets")
async def list_assets(
    request: Request,
    user: Annotated[dict, Depends(_require_auth)],
    settings: Annotated[Settings, Depends(_settings)],
    limit: int = 500,
) -> dict:
    store: AssetStore = request.app.state.store
    account = _request_account(request)
    account_id = int(account["id"]) if account else None
    user_id = int(user["id"])
    await asyncio.to_thread(
        _cleanup_expired_temporary_assets, store, settings.temp_storage_path
    )
    wechat_rows, temporary_rows = await asyncio.gather(
        asyncio.to_thread(
            store.list_assets,
            limit,
            user_id=user_id,
            account_id=account_id,
        ),
        asyncio.to_thread(
            store.list_temporary_assets, limit=limit, user_id=user_id
        ),
    )
    items = [_public_asset(row) for row in wechat_rows]
    items.extend(
        _public_temporary_asset(row, request, settings)
        for row in temporary_rows
    )
    items.sort(
        key=lambda item: item.get("updated_at") or item.get("created_at") or "",
        reverse=True,
    )
    items = items[: min(max(limit, 1), 2000)]
    return {"items": items, "count": len(items)}


async def _delete_asset_item(
    item: DeleteAssetItem,
    *,
    request: Request,
    settings: Settings,
) -> dict:
    store: AssetStore = request.app.state.store
    user_id = _request_user_id(request)
    if item.kind == "temporary":
        row = await asyncio.to_thread(
            store.get_temporary_asset_by_id, item.id, user_id=user_id
        )
        if not row:
            return {"kind": item.kind, "id": item.id, "missing": True}
        await asyncio.to_thread(
            _delete_temporary_row, store, row, settings.temp_storage_path
        )
        return {
            "kind": item.kind,
            "id": item.id,
            "local_deleted": True,
            "remote_deleted": False,
            "remote_delete_supported": False,
        }

    row = await asyncio.to_thread(store.get_asset, item.id, user_id=user_id)
    if not row:
        return {"kind": item.kind, "id": item.id, "missing": True}
    active = _request_account(request)
    if not active or int(row.get("account_id") or 0) != int(active["id"]):
        return {"kind": item.kind, "id": item.id, "missing": True}
    remote_deleted = False
    remote_missing = False
    if row.get("media_id"):
        client = _require_wechat_config(request)
        try:
            await client.delete_permanent_material(row["media_id"])
            remote_deleted = True
        except WechatAPIError as exc:
            if exc.errcode == 40007:
                remote_missing = True
            else:
                raise
    await asyncio.to_thread(store.delete_asset, item.id, user_id=user_id)
    result = {
        "kind": item.kind,
        "id": item.id,
        "local_deleted": True,
        "remote_deleted": remote_deleted,
        "remote_missing": remote_missing,
        "remote_delete_supported": bool(row.get("media_id")),
    }
    if row.get("article_url"):
        result["warning"] = (
            "正文图片 URL 无微信删除接口；已删除控制台记录，原 URL 可能仍可访问"
        )
    return result


async def _delete_asset_items(
    items: list[DeleteAssetItem],
    *,
    request: Request,
    settings: Settings,
) -> dict:
    deleted: list[dict] = []
    errors: list[dict] = []
    seen: set[tuple[str, int]] = set()
    for item in items:
        key = (item.kind, item.id)
        if key in seen:
            continue
        seen.add(key)
        try:
            deleted.append(
                await _delete_asset_item(item, request=request, settings=settings)
            )
        except HTTPException as exc:
            errors.append({"kind": item.kind, "id": item.id, "error": str(exc.detail)})
        except (OSError, RuntimeError, WechatAPIError) as exc:
            errors.append({"kind": item.kind, "id": item.id, "error": str(exc)})
    return {
        "deleted": deleted,
        "deleted_count": len(deleted),
        "errors": errors,
        "error_count": len(errors),
    }


@app.post("/api/assets/delete")
async def delete_assets(
    payload: DeleteAssetsRequest,
    request: Request,
    _: Annotated[dict, Depends(_require_auth)],
    settings: Annotated[Settings, Depends(_settings)],
) -> dict:
    _require_ajax(request)
    return await _delete_asset_items(
        payload.items, request=request, settings=settings
    )


@app.post("/api/assets/delete-all")
async def delete_all_assets(
    request: Request,
    user: Annotated[dict, Depends(_require_auth)],
    settings: Annotated[Settings, Depends(_settings)],
) -> dict:
    _require_ajax(request)
    store: AssetStore = request.app.state.store
    user_id = int(user["id"])
    account = _request_account(request)
    account_id = int(account["id"]) if account else None
    await asyncio.to_thread(
        _cleanup_expired_temporary_assets, store, settings.temp_storage_path
    )
    wechat_rows, temporary_rows = await asyncio.gather(
        asyncio.to_thread(
            store.list_assets,
            None,
            user_id=user_id,
            account_id=account_id,
        ),
        asyncio.to_thread(store.list_all_temporary_assets, user_id=user_id),
    )
    items = [DeleteAssetItem(kind="wechat", id=row["id"]) for row in wechat_rows]
    items.extend(
        DeleteAssetItem(kind="temporary", id=row["id"])
        for row in temporary_rows
    )
    if not items:
        return {"deleted": [], "deleted_count": 0, "errors": [], "error_count": 0}
    return await _delete_asset_items(items, request=request, settings=settings)


@app.post(
    "/api/v1/wechat-images",
    status_code=status.HTTP_201_CREATED,
    summary="通过微信公众号接口批量上传图片",
)
async def api_upload_wechat_images(
    request: Request,
    images: Annotated[list[UploadFile], File(description="重复 images 字段可批量上传")],
    _: Annotated[str, Depends(_require_client_token)],
    settings: Annotated[Settings, Depends(_settings)],
    mode: Annotated[Literal["article", "material", "both"], Form()] = "material",
) -> dict:
    if len(images) > 20:
        raise HTTPException(status_code=413, detail="单次最多上传 20 张图片")
    _require_wechat_config(request)
    items = []
    for image in images:
        result = await upload_image(
            request=request,
            _="ai-api",
            settings=settings,
            mode=mode,
            image=image,
        )
        items.append(_ai_upload_result(result))
    return {
        "items": items,
        "count": len(items),
        "success_count": sum(item["status"] != "failed" for item in items),
        "error_count": sum(item["status"] == "failed" for item in items),
        "mode": mode,
    }


def _publish_api_draft_context(
    request: Request, draft_id: int
) -> tuple[AssetStore, dict, dict]:
    store: AssetStore = request.app.state.store
    user_id = _request_user_id(request)
    account = _request_account(request, required=True)
    assert account is not None
    account_id = int(account["id"])
    row = store.get_draft_job(draft_id, user_id=user_id)
    if not row or int(row.get("account_id") or 0) != account_id:
        raise HTTPException(status_code=404, detail="草稿记录不存在")
    return store, row, account


async def _delete_draft_record(
    *, request: Request, store: AssetStore, row: dict, account: dict
) -> dict:
    remote_deleted = False
    remote_missing = False
    media_id = row.get("media_id")
    if media_id and row.get("status") != "deleted":
        client = _wechat_client_for_account(request, account)
        try:
            await client.delete_draft(media_id)
            remote_deleted = True
        except WechatAPIError as exc:
            if exc.errcode == 40007:
                remote_missing = True
            else:
                raise HTTPException(status_code=502, detail=str(exc)) from exc
    deleted = store.delete_draft_job(
        int(row["id"]),
        user_id=int(row["user_id"]),
        account_id=int(row["account_id"]),
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="草稿记录不存在")
    return {
        "deleted": True,
        "id": int(row["id"]),
        "request_id": row["request_id"],
        "media_id": media_id,
        "remote_deleted": remote_deleted,
        "remote_missing": remote_missing,
        "local_deleted": True,
    }


@app.post(
    "/api/v1/wechat-drafts",
    status_code=status.HTTP_201_CREATED,
    summary="写入微信公众号草稿箱",
)
async def api_create_wechat_draft(
    payload: DraftArticleRequest,
    request: Request,
    response: Response,
    _: Annotated[str, Depends(_require_client_token)],
) -> dict:
    client = _require_wechat_config(request)
    user_id = _request_user_id(request)
    account = _request_account(request, required=True)
    assert account is not None
    account_id = int(account["id"])
    article_data = _draft_article_data(payload.model_dump(exclude={"request_id"}))
    article_data["content"] = normalize_article_content(article_data["content"])
    try:
        validation = validate_article_content(article_data["content"])
    except ArticleValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    content_hash = _draft_content_hash(article_data)
    store: AssetStore = request.app.state.store
    existing = store.get_draft_job_by_request_id(
        payload.request_id, user_id=user_id, account_id=account_id
    )
    if existing:
        if existing["content_hash"] != content_hash:
            raise HTTPException(
                status_code=409,
                detail="request_id 已用于另一篇文章，请更换 request_id",
            )
        if existing["status"] == "created" and existing.get("media_id"):
            response.status_code = status.HTTP_200_OK
            return {
                "status": "created",
                "media_id": existing["media_id"],
                "request_id": payload.request_id,
                "cached": True,
                "validation": validation,
            }
        if existing["status"] == "pending":
            updated_at = datetime.fromisoformat(existing["updated_at"])
            if updated_at > datetime.now(UTC) - timedelta(minutes=5):
                response.status_code = status.HTTP_202_ACCEPTED
                return {
                    "status": "pending",
                    "media_id": None,
                    "request_id": payload.request_id,
                    "cached": True,
                    "validation": validation,
                }
            store.update_draft_job(
                existing["id"],
                status="unknown",
                last_error="任务处理中断，无法确认微信是否已创建草稿",
            )
            raise HTTPException(
                status_code=409,
                detail=(
                    "上次草稿任务结果无法确认，请先在微信公众号草稿箱核对，"
                    "不要直接重试"
                ),
            )
        if existing["status"] == "unknown":
            raise HTTPException(
                status_code=409,
                detail=(
                    "上次草稿任务结果无法确认，请先在微信公众号草稿箱核对，"
                    "不要直接重试"
                ),
            )
        if existing["status"] == "deleted":
            raise HTTPException(
                status_code=409,
                detail="该 request_id 对应的草稿已删除，重新创建时请使用新的 request_id",
            )
        job = store.update_draft_job(existing["id"], status="pending")
    else:
        job = store.create_draft_job(
            user_id=user_id,
            account_id=account_id,
            request_id=payload.request_id,
            content_hash=content_hash,
            title=payload.title,
            author=payload.author,
            digest=payload.digest,
            content=article_data["content"],
            content_source_url=payload.content_source_url,
            thumb_media_id=payload.thumb_media_id,
            need_open_comment=payload.need_open_comment,
            only_fans_can_comment=payload.only_fans_can_comment,
        )

    article = _wechat_draft_article(article_data)
    try:
        media_id = await client.create_draft(article)
    except WechatAPIError as exc:
        store.update_draft_job(
            job["id"],
            status="unknown" if exc.ambiguous else "failed",
            last_error=str(exc),
        )
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    store.update_draft_job(job["id"], status="created", media_id=media_id)
    return {
        "status": "created",
        "media_id": media_id,
        "request_id": payload.request_id,
        "cached": False,
        "validation": validation,
    }


@app.get(
    "/api/v1/wechat-drafts",
    summary="列出当前管理员公众号的草稿任务",
)
async def api_list_wechat_drafts(
    request: Request,
    _: Annotated[str, Depends(_require_client_token)],
    limit: int = 100,
    offset: int = 0,
) -> dict:
    store: AssetStore = request.app.state.store
    user_id = _request_user_id(request)
    account = _request_account(request, required=True)
    assert account is not None
    account_id = int(account["id"])
    items = [
        _public_draft(row, current_user_id=user_id)
        for row in store.list_draft_jobs(
            limit=limit,
            offset=offset,
            user_id=user_id,
            account_id=account_id,
        )
    ]
    total = store.count_draft_jobs(user_id=user_id, account_id=account_id)
    safe_offset = max(offset, 0)
    return {
        "items": items,
        "count": total,
        "limit": min(max(limit, 1), 1000),
        "offset": safe_offset,
        "has_more": safe_offset + len(items) < total,
    }


@app.get(
    "/api/v1/wechat-drafts/wechat-box",
    summary="直接列出微信公众号草稿箱",
)
async def api_list_remote_wechat_drafts(
    request: Request,
    _: Annotated[str, Depends(_require_client_token)],
    offset: int = 0,
    count: int = 20,
    no_content: bool = False,
) -> dict:
    client = _require_wechat_config(request)
    safe_offset = max(offset, 0)
    safe_count = min(max(count, 1), 20)
    try:
        result = await client.list_drafts(
            offset=safe_offset,
            count=safe_count,
            no_content=no_content,
        )
    except WechatAPIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    items = result.get("item")
    if not isinstance(items, list):
        items = []
    return {
        "items": items,
        "count": int(result.get("item_count") or len(items)),
        "total_count": int(result.get("total_count") or 0),
        "offset": safe_offset,
        "limit": safe_count,
        "has_more": safe_offset + len(items) < int(result.get("total_count") or 0),
    }


@app.get(
    "/api/v1/wechat-drafts/wechat-box/{media_id}",
    summary="按 media_id 读取微信草稿",
)
async def api_get_remote_wechat_draft(
    media_id: str,
    request: Request,
    _: Annotated[str, Depends(_require_client_token)],
) -> dict:
    client = _require_wechat_config(request)
    remote = await _get_remote_draft_or_404(client, media_id)
    return {
        "media_id": media_id,
        "article": _first_remote_draft_article(remote),
        "remote": remote,
    }


@app.put(
    "/api/v1/wechat-drafts/wechat-box/{media_id}",
    summary="按 media_id 修改微信草稿",
)
async def api_update_remote_wechat_draft(
    media_id: str,
    payload: DraftArticleUpdate,
    request: Request,
    _: Annotated[str, Depends(_require_client_token)],
) -> dict:
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=422, detail="至少提供一个需要修改的字段")
    if any(value is None for value in changes.values()):
        raise HTTPException(status_code=422, detail="修改字段不能为 null")

    user_id = _request_user_id(request)
    account = _request_account(request, required=True)
    assert account is not None
    account_id = int(account["id"])
    client = _wechat_client_for_account(request, account)
    article_data, validation = await _update_remote_draft_article(
        client=client,
        media_id=media_id,
        changes=changes,
    )

    store: AssetStore = request.app.state.store
    local = store.get_draft_job_by_media_id(
        media_id, user_id=user_id, account_id=account_id
    )
    updated = None
    if local:
        updated = store.update_draft_content(
            int(local["id"]),
            content_hash=_draft_content_hash(article_data),
            title=article_data["title"],
            author=article_data["author"],
            digest=article_data["digest"],
            content=article_data["content"],
            content_source_url=article_data["content_source_url"],
            thumb_media_id=article_data["thumb_media_id"],
            need_open_comment=article_data["need_open_comment"],
            only_fans_can_comment=article_data["only_fans_can_comment"],
        )
    return {
        "media_id": media_id,
        "article": article_data,
        "draft": _public_draft(updated, include_content=True) if updated else None,
        "remote_updated": True,
        "validation": validation,
    }


@app.delete(
    "/api/v1/wechat-drafts/wechat-box/{media_id}",
    summary="按 media_id 删除微信草稿",
)
async def api_delete_remote_wechat_draft(
    media_id: str,
    request: Request,
    _: Annotated[str, Depends(_require_client_token)],
) -> dict:
    user_id = _request_user_id(request)
    account = _request_account(request, required=True)
    assert account is not None
    account_id = int(account["id"])
    client = _wechat_client_for_account(request, account)
    remote_deleted = False
    remote_missing = False
    try:
        await client.delete_draft(media_id)
        remote_deleted = True
    except WechatAPIError as exc:
        if exc.errcode == 40007:
            remote_missing = True
        else:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    store: AssetStore = request.app.state.store
    local = store.get_draft_job_by_media_id(
        media_id, user_id=user_id, account_id=account_id
    )
    local_deleted = False
    if local:
        local_deleted = store.delete_draft_job(
            int(local["id"]), user_id=user_id, account_id=account_id
        )
        if not local_deleted:
            raise HTTPException(status_code=409, detail="本地草稿记录删除失败")
    return {
        "deleted": True,
        "media_id": media_id,
        "remote_deleted": remote_deleted,
        "remote_missing": remote_missing,
        "local_deleted": local_deleted,
    }


@app.get(
    "/api/v1/wechat-drafts/{draft_id}",
    summary="读取草稿任务并核对微信草稿箱",
)
async def api_get_wechat_draft(
    draft_id: int,
    request: Request,
    _: Annotated[str, Depends(_require_client_token)],
) -> dict:
    _, row, account = _publish_api_draft_context(request, draft_id)
    remote = None
    remote_checked = False
    remote_exists: bool | None = None
    if row.get("media_id") and row.get("status") == "created":
        remote_checked = True
        client = _wechat_client_for_account(request, account)
        try:
            remote = await client.get_draft(row["media_id"])
            remote_exists = True
        except WechatAPIError as exc:
            if exc.errcode == 40007:
                remote_exists = False
            else:
                raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {
        "draft": _public_draft(row, include_content=True),
        "remote_checked": remote_checked,
        "remote_exists": remote_exists,
        "remote": remote,
    }


@app.put(
    "/api/v1/wechat-drafts/{draft_id}",
    summary="修改微信公众号草稿",
)
async def api_update_wechat_draft(
    draft_id: int,
    payload: DraftArticleUpdate,
    request: Request,
    _: Annotated[str, Depends(_require_client_token)],
) -> dict:
    store, row, account = _publish_api_draft_context(request, draft_id)
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=422, detail="至少提供一个需要修改的字段")
    if any(value is None for value in changes.values()):
        raise HTTPException(status_code=422, detail="修改字段不能为 null")
    if row.get("status") != "created" or not row.get("media_id"):
        raise HTTPException(status_code=409, detail="只有已写入微信的草稿可以修改")

    client = _wechat_client_for_account(request, account)
    article_data, validation = await _update_remote_draft_article(
        client=client,
        media_id=row["media_id"],
        changes=changes,
    )

    updated = store.update_draft_content(
        draft_id,
        content_hash=_draft_content_hash(article_data),
        title=article_data["title"],
        author=article_data["author"],
        digest=article_data["digest"],
        content=article_data["content"],
        content_source_url=article_data["content_source_url"],
        thumb_media_id=article_data["thumb_media_id"],
        need_open_comment=article_data["need_open_comment"],
        only_fans_can_comment=article_data["only_fans_can_comment"],
    )
    return {
        "draft": _public_draft(updated, include_content=True),
        "remote_updated": True,
        "validation": validation,
    }


@app.delete(
    "/api/v1/wechat-drafts/{draft_id}",
    summary="删除微信公众号草稿及本地任务记录",
)
async def api_delete_wechat_draft(
    draft_id: int,
    request: Request,
    _: Annotated[str, Depends(_require_client_token)],
) -> dict:
    store, row, account = _publish_api_draft_context(request, draft_id)
    return await _delete_draft_record(
        request=request, store=store, row=row, account=account
    )


@app.post(
    "/api/drafts",
    status_code=status.HTTP_201_CREATED,
    summary="当前登录用户写入微信公众号草稿箱",
)
async def create_session_draft(
    payload: DraftArticleRequest,
    request: Request,
    response: Response,
    _: Annotated[dict, Depends(_require_auth)],
) -> dict:
    _require_ajax(request)
    return await api_create_wechat_draft(payload, request, response, "session")


@app.get("/api/drafts")
async def list_drafts(
    request: Request,
    user: Annotated[dict, Depends(_require_auth)],
    limit: int = 100,
    offset: int = 0,
) -> dict:
    store: AssetStore = request.app.state.store
    account = _request_account(request)
    account_id = int(account["id"]) if account else None
    user_id = int(user["id"])
    draft_filters = {"user_id": user_id, "account_id": account_id}
    if user["role"] == "admin":
        draft_filters = {}
    items = [
        _public_draft(row, current_user_id=user_id)
        for row in store.list_draft_jobs(
            limit=limit, offset=offset, **draft_filters
        )
    ]
    total = store.count_draft_jobs(**draft_filters)
    safe_offset = max(offset, 0)
    return {
        "items": items,
        "count": total,
        "limit": min(max(limit, 1), 1000),
        "offset": safe_offset,
        "has_more": safe_offset + len(items) < total,
    }


@app.get("/api/drafts/{draft_id}")
async def get_draft(
    draft_id: int,
    request: Request,
    user: Annotated[dict, Depends(_require_auth)],
) -> dict:
    store: AssetStore = request.app.state.store
    row = store.get_draft_job(draft_id, user_id=int(user["id"]))
    if not row:
        raise HTTPException(status_code=404, detail="草稿记录不存在")
    active = _request_account(request)
    if not active or int(row.get("account_id") or 0) != int(active["id"]):
        raise HTTPException(status_code=404, detail="草稿记录不存在")
    return {
        "draft": _public_draft(
            row, include_content=True, current_user_id=int(user["id"])
        )
    }


@app.post("/api/drafts/{draft_id}/delete")
async def delete_draft(
    draft_id: int,
    request: Request,
    user: Annotated[dict, Depends(_require_auth)],
) -> dict:
    _require_ajax(request)
    store: AssetStore = request.app.state.store
    row = store.get_draft_job(draft_id, user_id=int(user["id"]))
    if not row:
        raise HTTPException(status_code=404, detail="草稿记录不存在")
    active = _request_account(request)
    row_account_id = int(row.get("account_id") or 0)
    if user["role"] != "admin" and (
        not active or row_account_id != int(active["id"])
    ):
        raise HTTPException(status_code=404, detail="草稿记录不存在")
    account = store.get_official_account(
        row_account_id, user_id=int(user["id"])
    )
    if account is None:
        raise HTTPException(status_code=404, detail="草稿记录不存在")
    return await _delete_draft_record(
        request=request, store=store, row=row, account=account
    )


@app.post(
    "/api/v1/temp-images",
    status_code=status.HTTP_201_CREATED,
    summary="批量上传 30 天临时图片",
)
async def api_upload_temporary_images(
    request: Request,
    images: Annotated[list[UploadFile], File(description="重复 images 字段可批量上传")],
    _: Annotated[str, Depends(_require_client_token)],
    settings: Annotated[Settings, Depends(_settings)],
) -> dict:
    if len(images) > 100:
        raise HTTPException(status_code=413, detail="单次最多上传 100 张图片")
    await asyncio.to_thread(
        _cleanup_expired_temporary_assets,
        request.app.state.store,
        settings.temp_storage_path,
    )
    items: list[dict] = []
    errors: list[dict[str, str]] = []
    for image in images:
        filename = _safe_upload_filename(image.filename)
        data = await image.read(settings.max_source_bytes + 1)
        await image.close()
        if not data:
            errors.append({"filename": filename, "error": "文件为空"})
            continue
        if len(data) > settings.max_source_bytes:
            errors.append(
                {
                    "filename": filename,
                    "error": (
                        f"源文件超过服务器上限 "
                        f"{settings.max_source_bytes / 1_000_000:.0f}MB"
                    ),
                }
            )
            continue
        try:
            inspect_image(data)
            items.append(
                await _store_temporary_image(
                    request=request,
                    settings=settings,
                    filename=filename,
                    data=data,
                )
            )
        except ImageValidationError as exc:
            errors.append({"filename": filename, "error": str(exc)})
    return {
        "items": items,
        "count": len(items),
        "errors": errors,
        "retention_days": settings.temp_retention_days,
    }


@app.get(
    "/api/v1/temp-images",
    summary="获取尚未过期的临时图片 URL",
)
async def api_list_temporary_images(
    request: Request,
    _: Annotated[str, Depends(_require_client_token)],
    settings: Annotated[Settings, Depends(_settings)],
    limit: int = 500,
) -> dict:
    store: AssetStore = request.app.state.store
    await asyncio.to_thread(
        _cleanup_expired_temporary_assets, store, settings.temp_storage_path
    )
    items = [
        _public_temporary_asset(row, request, settings)
        for row in store.list_temporary_assets(
            limit=limit, user_id=_request_user_id(request)
        )
    ]
    return {
        "items": items,
        "count": len(items),
        "retention_days": settings.temp_retention_days,
    }


@app.get("/temp/{token}", name="get_temporary_image", include_in_schema=False)
async def get_temporary_image(
    token: str,
    request: Request,
    settings: Annotated[Settings, Depends(_settings)],
) -> FileResponse:
    if len(token) < 24 or len(token) > 64:
        raise HTTPException(status_code=404, detail="图片不存在或已过期")
    store: AssetStore = request.app.state.store
    row = store.get_temporary_asset(token)
    now = datetime.now(UTC)
    now_text = now.isoformat(timespec="seconds")
    if not row or row["expires_at"] <= now_text:
        await asyncio.to_thread(
            _cleanup_expired_temporary_assets, store, settings.temp_storage_path
        )
        raise HTTPException(status_code=404, detail="图片不存在或已过期")
    path = _temporary_asset_path(row, settings.temp_storage_path)
    if path is None or not path.is_file():
        raise HTTPException(status_code=404, detail="图片不存在或已过期")
    expires_at = datetime.fromisoformat(row["expires_at"])
    max_age = max(0, min(3600, int((expires_at - now).total_seconds())))
    return FileResponse(
        path,
        media_type=row["content_type"],
        headers={
            "Cache-Control": f"public, max-age={max_age}",
            "X-Content-Type-Options": "nosniff",
            "X-Robots-Tag": "noindex, nofollow",
        },
    )


@app.get("/api/export.csv")
async def export_csv(
    request: Request,
    user: Annotated[dict, Depends(_require_auth)],
    settings: Annotated[Settings, Depends(_settings)],
) -> Response:
    store: AssetStore = request.app.state.store
    await asyncio.to_thread(
        _cleanup_expired_temporary_assets, store, settings.temp_storage_path
    )
    output = io.StringIO()
    writer = csv.writer(output)
    user_id = int(user["id"])
    account = _request_account(request)
    account_id = int(account["id"]) if account else None
    writer.writerow(
        [
            "文件名",
            "类型",
            "SHA-256",
            "media_id",
            "永久素材URL",
            "正文图片URL",
            "临时图片URL",
            "过期时间",
            "更新时间",
            "错误",
        ]
    )
    for row in store.list_assets(
        2000, user_id=user_id, account_id=account_id
    ):
        writer.writerow(
            [
                _csv_safe(value)
                for value in [
                row["filename"],
                "wechat",
                row["sha256"],
                row.get("media_id") or "",
                row.get("material_url") or "",
                row.get("article_url") or "",
                "",
                "",
                row["updated_at"],
                row.get("last_error") or "",
                ]
            ]
        )
    for row in store.list_temporary_assets(limit=2000, user_id=user_id):
        item = _public_temporary_asset(row, request, settings)
        writer.writerow(
            [
                _csv_safe(value)
                for value in [
                item["filename"],
                "temporary",
                item["sha256"],
                "",
                "",
                "",
                item["url"],
                item["expires_at"],
                item["created_at"],
                "",
                ]
            ]
        )
    content = "\ufeff" + output.getvalue()
    return Response(
        content=content.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="wechat-assets.csv"'},
    )
