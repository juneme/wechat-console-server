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
from .article import ArticleValidationError, validate_article_content
from .config import Settings, get_settings
from .credentials import CredentialCipher, CredentialError
from .database import AssetStore
from .image_tools import (
    ImageValidationError,
    inspect_image,
    prepare_article_image,
    prepare_temporary_image,
)
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
_login_failure_buckets: dict[str, deque[float]] = {}
_login_failure_lock = Lock()


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=256)


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


def _settings() -> Settings:
    return get_settings()


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


def _require_auth(
    request: Request,
) -> str:
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
    return str(session["username"])


def _require_ajax(request: Request) -> None:
    if (
        request.headers.get("X-Requested-With") != "WechatUploader"
        and not getattr(request.state, "api_key_verified", False)
    ):
        raise HTTPException(
            status_code=403, detail="Missing request verification header"
        )


def _require_temp_api_key(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(bearer_security)
    ],
    settings: Annotated[Settings, Depends(_settings)],
) -> str:
    if not settings.temp_api_configured:
        raise HTTPException(status_code=503, detail="TEMP_API_KEY 尚未配置")
    valid = (
        credentials is not None
        and credentials.scheme.lower() == "bearer"
        and secrets.compare_digest(credentials.credentials, settings.temp_api_key)
    )
    if not valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return "temp-api"


def _require_ai_api_key(
    request: Request,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(bearer_security)
    ],
    settings: Annotated[Settings, Depends(_settings)],
) -> str:
    if not settings.ai_api_configured:
        raise HTTPException(status_code=503, detail="AI_API_KEY 尚未配置")
    valid = (
        credentials is not None
        and credentials.scheme.lower() == "bearer"
        and secrets.compare_digest(credentials.credentials, settings.ai_api_key)
    )
    if not valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
            headers={"WWW-Authenticate": "Bearer"},
        )
    request.state.api_key_verified = True
    return "ai-api"


def _require_publish_api_key(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(bearer_security)
    ],
    settings: Annotated[Settings, Depends(_settings)],
) -> str:
    if not settings.publish_api_configured:
        raise HTTPException(status_code=503, detail="PUBLISH_API_KEY 尚未配置")
    valid = (
        credentials is not None
        and credentials.scheme.lower() == "bearer"
        and secrets.compare_digest(credentials.credentials, settings.publish_api_key)
    )
    if not valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return "publish-api"


def _ai_upload_result(result: dict) -> dict:
    asset = result.get("asset") or {}
    return {
        "filename": result.get("filename"),
        "url": asset.get("url"),
        "size": asset.get("processed_bytes") or asset.get("original_bytes"),
        "uploaded_at": asset.get("updated_at") or asset.get("created_at"),
        "status": result.get("status"),
        "media_id": asset.get("media_id"),
        "material_url": asset.get("material_url"),
        "article_url": asset.get("article_url"),
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
    result["kind"] = "wechat"
    result["url"] = row.get("article_url") or row.get("material_url")
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
    used_bytes = await asyncio.to_thread(store.temporary_storage_bytes)
    if used_bytes + len(prepared.data) > settings.temp_storage_max_bytes:
        raise ImageValidationError("服务器临时图片存储空间已达到上限，请先删除旧图片")
    storage_path = settings.temp_storage_path
    storage_path.mkdir(parents=True, exist_ok=True)
    target = storage_path / stored_name
    await asyncio.to_thread(target.write_bytes, prepared.data)
    expires_at = (
        datetime.now(UTC) + timedelta(days=settings.temp_retention_days)
    ).isoformat(timespec="seconds")
    try:
        row = store.create_temporary_asset(
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


def _require_wechat_config(request: Request) -> WechatClient:
    config_error = getattr(request.app.state, "wechat_config_error", None)
    if config_error:
        raise HTTPException(status_code=503, detail=config_error)
    client: WechatClient = request.app.state.wechat
    if isinstance(client, WechatClient) and (not client.app_id or not client.app_secret):
        raise HTTPException(
            status_code=503,
            detail="微信 AppID/AppSecret 尚未配置，请先在公众号设置中完成配置",
        )
    return client


def _set_wechat_client(
    app: FastAPI, *, app_id: str, app_secret: str, source: str
) -> None:
    app.state.wechat = WechatClient(app_id, app_secret, app.state.http)
    app.state.wechat_source = source
    app.state.wechat_config_error = None


def _public_account(request: Request) -> dict:
    store: AssetStore = request.app.state.store
    row = store.get_wechat_account()
    client: WechatClient = request.app.state.wechat
    return {
        "display_name": row.get("display_name", "未命名公众号") if row else "未命名公众号",
        "account_type": row.get("account_type", "subscription") if row else "subscription",
        "app_id": client.app_id,
        "app_id_suffix": client.app_id[-6:] if client.app_id else "",
        "secret_configured": bool(client.app_secret),
        "source": getattr(request.app.state, "wechat_source", "none"),
        "updated_at": row.get("updated_at") if row else None,
        "encryption": request.app.state.credential_cipher.source,
        "config_error": getattr(request.app.state, "wechat_config_error", None),
    }


def _public_draft(row: dict, *, include_content: bool = False) -> dict:
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
        )
    }
    result["content_characters"] = len(row.get("content") or "")
    if include_content:
        result["content"] = row.get("content") or ""
    return result


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    settings.validate_runtime()
    store = AssetStore(settings.database_path)
    store.initialize()
    store.delete_expired_admin_sessions()
    settings.temp_storage_path.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(
        _cleanup_expired_temporary_assets, store, settings.temp_storage_path
    )
    http = httpx.AsyncClient(timeout=httpx.Timeout(90.0, connect=15.0))
    app.state.store = store
    app.state.settings = settings
    app.state.http = http
    cipher = CredentialCipher.create(
        secret=settings.credentials_encryption_key,
        key_path=settings.database_path.parent / ".wechat-credentials.key",
    )
    app.state.credential_cipher = cipher
    account = store.get_wechat_account()
    if account:
        try:
            app_secret = cipher.decrypt(account["app_secret_ciphertext"])
            _set_wechat_client(
                app,
                app_id=account["app_id"],
                app_secret=app_secret,
                source="console",
            )
        except CredentialError as exc:
            app.state.wechat = WechatClient("", "", http)
            app.state.wechat_source = "console"
            app.state.wechat_config_error = str(exc)
    else:
        _set_wechat_client(
            app,
            app_id=settings.wechat_app_id,
            app_secret=settings.wechat_app_secret,
            source="environment" if settings.wechat_configured else "none",
        )
    cleanup_task = asyncio.create_task(_temporary_cleanup_loop(app))
    try:
        yield
    finally:
        cleanup_task.cancel()
        with suppress(asyncio.CancelledError):
            await cleanup_task
        await http.aclose()


app = FastAPI(
    title="微信公众号控制台",
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


@app.post("/api/auth/login")
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    settings: Annotated[Settings, Depends(_settings)],
) -> dict:
    _require_ajax(request)
    username_valid = secrets.compare_digest(
        payload.username.encode("utf-8"), settings.admin_username.encode("utf-8")
    )
    password_valid = secrets.compare_digest(
        payload.password.encode("utf-8"), settings.admin_password.encode("utf-8")
    )
    rate_username = settings.admin_username if username_valid else "<invalid>"
    _enforce_login_rate_limit(request, rate_username)
    if not (username_valid and password_valid):
        _record_login_failure(request, rate_username)
        raise HTTPException(status_code=401, detail="用户名或密码不正确")
    _clear_login_failures(request, rate_username)

    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    expires_at = (
        datetime.now(UTC) + timedelta(seconds=SESSION_MAX_AGE_SECONDS)
    ).isoformat(timespec="seconds")
    store: AssetStore = request.app.state.store
    store.create_admin_session(
        token_hash=token_hash,
        username=settings.admin_username,
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
    return {"user": {"username": settings.admin_username}}


@app.get("/api/auth/me")
async def current_user(
    username: Annotated[str, Depends(_require_auth)],
) -> dict:
    return {"user": {"username": username}}


@app.post("/api/auth/logout")
async def logout(
    request: Request,
    response: Response,
    _: Annotated[str, Depends(_require_auth)],
) -> dict[str, bool]:
    _require_ajax(request)
    token = request.cookies.get(SESSION_COOKIE, "")
    if token:
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        store: AssetStore = request.app.state.store
        store.delete_admin_session(token_hash)
    response.delete_cookie(SESSION_COOKIE, path="/", samesite="lax")
    return {"logged_out": True}


@app.get("/api/status")
async def api_status(
    request: Request,
    _: Annotated[str, Depends(_require_auth)],
    settings: Annotated[Settings, Depends(_settings)],
) -> dict:
    account = _public_account(request)
    return {
        "ready": bool(account["app_id"] and account["secret_configured"])
        and not account["config_error"],
        "app_id_suffix": account["app_id_suffix"],
        "account": account,
        "temporary_ready": True,
        "temporary_api_ready": settings.temp_api_configured,
        "image_api_ready": settings.ai_api_configured,
        "publish_api_ready": settings.publish_api_configured,
        "temporary_retention_days": settings.temp_retention_days,
        "limits": {
            "article_bytes": settings.article_max_bytes,
            "permanent_bytes": settings.permanent_max_bytes,
            "temporary_bytes": settings.temp_max_bytes,
        },
    }


@app.get("/api/account")
async def get_account(
    request: Request,
    _: Annotated[str, Depends(_require_auth)],
) -> dict:
    return {"account": _public_account(request)}


@app.put("/api/account")
async def update_account(
    payload: WechatAccountUpdate,
    request: Request,
    _: Annotated[str, Depends(_require_auth)],
) -> dict:
    _require_ajax(request)
    display_name = payload.display_name.strip()
    app_id = payload.app_id.strip()
    supplied_secret = (payload.app_secret or "").strip()
    if not display_name or not app_id:
        raise HTTPException(status_code=422, detail="公众号名称和 AppID 不能为空")
    if supplied_secret and len(supplied_secret) < 8:
        raise HTTPException(status_code=422, detail="AppSecret 格式不正确")

    store: AssetStore = request.app.state.store
    current = store.get_wechat_account()
    client: WechatClient = request.app.state.wechat
    app_secret = supplied_secret or client.app_secret
    if not app_secret:
        raise HTTPException(status_code=422, detail="首次保存时必须填写 AppSecret")

    cipher: CredentialCipher = request.app.state.credential_cipher
    ciphertext = (
        cipher.encrypt(app_secret)
        if supplied_secret or not current
        else current["app_secret_ciphertext"]
    )
    store.upsert_wechat_account(
        display_name=display_name,
        account_type=payload.account_type,
        app_id=app_id,
        app_secret_ciphertext=ciphertext,
    )
    _set_wechat_client(
        request.app,
        app_id=app_id,
        app_secret=app_secret,
        source="console",
    )
    return {"account": _public_account(request)}


@app.get("/api/overview")
async def overview(
    request: Request,
    _: Annotated[str, Depends(_require_auth)],
    settings: Annotated[Settings, Depends(_settings)],
) -> dict:
    store: AssetStore = request.app.state.store
    counts = store.overview_counts()
    return {
        "account": _public_account(request),
        "counts": counts,
        "apis": {
            "wechat": bool(request.app.state.wechat.app_id and request.app.state.wechat.app_secret),
            "images": settings.ai_api_configured,
            "drafts": settings.publish_api_configured,
            "temporary": settings.temp_api_configured,
        },
        "recent_drafts": [
            _public_draft(row) for row in store.list_draft_jobs(limit=5)
        ],
    }


@app.post("/api/account/test")
@app.post("/api/test-connection")
async def test_connection(
    request: Request,
    _: Annotated[str, Depends(_require_auth)],
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
    _: Annotated[str, Depends(_require_auth)],
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
            await request.app.state.wechat.get_token()
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

    for check_id, label, configured in (
        ("image_api", "图片 API Key", settings.ai_api_configured),
        ("publish_api", "草稿 API Key", settings.publish_api_configured),
        ("temporary_api", "临时图片 API Key", settings.temp_api_configured),
    ):
        checks.append(
            {
                "id": check_id,
                "label": label,
                "status": "ok" if configured else "warning",
                "detail": "已配置" if configured else "未配置",
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
        "ready": database_ok
        and wechat_ok
        and settings.ai_api_configured
        and settings.publish_api_configured,
        "checks": checks,
        "migration_backup": (
            store.last_migration_backup.name if store.last_migration_backup else None
        ),
    }


@app.post("/api/upload")
async def upload_image(
    request: Request,
    _: Annotated[str, Depends(_require_auth)],
    settings: Annotated[Settings, Depends(_settings)],
    mode: Annotated[Literal["article", "material", "both", "temporary"], Form()],
    image: Annotated[UploadFile, File()],
) -> dict:
    _require_ajax(request)
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
    existing = store.get_by_hash(sha256)
    row = store.upsert_source(
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
    _: Annotated[str, Depends(_require_auth)],
    settings: Annotated[Settings, Depends(_settings)],
    limit: int = 500,
) -> dict:
    store: AssetStore = request.app.state.store
    await asyncio.to_thread(
        _cleanup_expired_temporary_assets, store, settings.temp_storage_path
    )
    wechat_rows, temporary_rows = await asyncio.gather(
        asyncio.to_thread(store.list_assets, limit),
        asyncio.to_thread(store.list_temporary_assets, limit=limit),
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
    if item.kind == "temporary":
        row = await asyncio.to_thread(store.get_temporary_asset_by_id, item.id)
        if not row:
            return {"kind": item.kind, "id": item.id, "missing": True}
        await asyncio.to_thread(
            _delete_temporary_row, store, row, settings.temp_storage_path
        )
        return {"kind": item.kind, "id": item.id, "remote_deleted": False}

    row = await asyncio.to_thread(store.get_asset, item.id)
    if not row:
        return {"kind": item.kind, "id": item.id, "missing": True}
    remote_deleted = False
    if row.get("media_id"):
        client = _require_wechat_config(request)
        await client.delete_permanent_material(row["media_id"])
        remote_deleted = True
    await asyncio.to_thread(store.delete_asset, item.id)
    result = {
        "kind": item.kind,
        "id": item.id,
        "remote_deleted": remote_deleted,
    }
    if row.get("article_url"):
        result["warning"] = "正文图片 URL 无微信删除接口，已删除本地记录"
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
    _: Annotated[str, Depends(_require_auth)],
    settings: Annotated[Settings, Depends(_settings)],
) -> dict:
    _require_ajax(request)
    return await _delete_asset_items(
        payload.items, request=request, settings=settings
    )


@app.post("/api/assets/delete-all")
async def delete_all_assets(
    request: Request,
    _: Annotated[str, Depends(_require_auth)],
    settings: Annotated[Settings, Depends(_settings)],
) -> dict:
    _require_ajax(request)
    store: AssetStore = request.app.state.store
    await asyncio.to_thread(
        _cleanup_expired_temporary_assets, store, settings.temp_storage_path
    )
    wechat_rows, temporary_rows = await asyncio.gather(
        asyncio.to_thread(store.list_assets, None),
        asyncio.to_thread(store.list_all_temporary_assets),
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
    _: Annotated[str, Depends(_require_ai_api_key)],
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


@app.post(
    "/api/v1/wechat-drafts",
    status_code=status.HTTP_201_CREATED,
    summary="写入微信公众号草稿箱",
)
async def api_create_wechat_draft(
    payload: DraftArticleRequest,
    request: Request,
    response: Response,
    _: Annotated[str, Depends(_require_publish_api_key)],
) -> dict:
    client = _require_wechat_config(request)
    try:
        validation = validate_article_content(payload.content)
    except ArticleValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    article_data = payload.model_dump(exclude={"request_id"})
    content_hash = hashlib.sha256(
        json.dumps(
            article_data, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    store: AssetStore = request.app.state.store
    existing = store.get_draft_job_by_request_id(payload.request_id)
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
            request_id=payload.request_id,
            content_hash=content_hash,
            title=payload.title,
            author=payload.author,
            digest=payload.digest,
            content=payload.content,
            content_source_url=payload.content_source_url,
            thumb_media_id=payload.thumb_media_id,
            need_open_comment=payload.need_open_comment,
            only_fans_can_comment=payload.only_fans_can_comment,
        )

    article = {
        "article_type": "news",
        "title": payload.title,
        "content": payload.content,
        "thumb_media_id": payload.thumb_media_id,
        "need_open_comment": payload.need_open_comment,
        "only_fans_can_comment": payload.only_fans_can_comment,
    }
    if payload.author:
        article["author"] = payload.author
    if payload.digest:
        article["digest"] = payload.digest
    if payload.content_source_url:
        article["content_source_url"] = payload.content_source_url
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


@app.get("/api/drafts")
async def list_drafts(
    request: Request,
    _: Annotated[str, Depends(_require_auth)],
    limit: int = 200,
) -> dict:
    store: AssetStore = request.app.state.store
    items = [_public_draft(row) for row in store.list_draft_jobs(limit=limit)]
    return {"items": items, "count": len(items)}


@app.get("/api/drafts/{draft_id}")
async def get_draft(
    draft_id: int,
    request: Request,
    _: Annotated[str, Depends(_require_auth)],
) -> dict:
    store: AssetStore = request.app.state.store
    row = store.get_draft_job(draft_id)
    if not row:
        raise HTTPException(status_code=404, detail="草稿记录不存在")
    return {"draft": _public_draft(row, include_content=True)}


@app.post("/api/drafts/{draft_id}/delete")
async def delete_draft(
    draft_id: int,
    request: Request,
    _: Annotated[str, Depends(_require_auth)],
) -> dict:
    _require_ajax(request)
    store: AssetStore = request.app.state.store
    row = store.get_draft_job(draft_id)
    if not row:
        raise HTTPException(status_code=404, detail="草稿记录不存在")
    if row.get("media_id") and row["status"] != "deleted":
        client = _require_wechat_config(request)
        try:
            await client.delete_draft(row["media_id"])
        except WechatAPIError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
    updated = store.update_draft_job(draft_id, status="deleted")
    return {"draft": _public_draft(updated)}


@app.post(
    "/api/v1/temp-images",
    status_code=status.HTTP_201_CREATED,
    summary="批量上传 30 天临时图片",
)
async def api_upload_temporary_images(
    request: Request,
    images: Annotated[list[UploadFile], File(description="重复 images 字段可批量上传")],
    _: Annotated[str, Depends(_require_temp_api_key)],
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
    _: Annotated[str, Depends(_require_temp_api_key)],
    settings: Annotated[Settings, Depends(_settings)],
    limit: int = 500,
) -> dict:
    store: AssetStore = request.app.state.store
    await asyncio.to_thread(
        _cleanup_expired_temporary_assets, store, settings.temp_storage_path
    )
    items = [
        _public_temporary_asset(row, request, settings)
        for row in store.list_temporary_assets(limit=limit)
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
    _: Annotated[str, Depends(_require_auth)],
    settings: Annotated[Settings, Depends(_settings)],
) -> Response:
    store: AssetStore = request.app.state.store
    await asyncio.to_thread(
        _cleanup_expired_temporary_assets, store, settings.temp_storage_path
    )
    output = io.StringIO()
    writer = csv.writer(output)
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
    for row in store.list_assets(2000):
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
    for row in store.list_temporary_assets(limit=2000):
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
