import io
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image

from app.config import get_settings
from app.main import LOGIN_IP_FAILURE_LIMIT, _login_failure_buckets, app
from app.wechat import WechatAPIError


class FakeWechatClient:
    def __init__(self) -> None:
        self.app_id = "wx-test"
        self.app_secret = "secret-test"
        self.deleted: list[str] = []
        self.deleted_drafts: list[str] = []
        self.created_drafts: list[dict] = []

    async def upload_permanent_image(self, **_: object) -> dict[str, str]:
        return {"media_id": "media-1", "url": "https://example.test/material/1"}

    async def get_token(self, **_: object) -> str:
        return "test-access-token"

    async def delete_permanent_material(self, media_id: str) -> None:
        self.deleted.append(media_id)

    async def create_draft(self, article: dict) -> str:
        self.created_drafts.append(article)
        return "draft-media-1"

    async def delete_draft(self, media_id: str) -> None:
        self.deleted_drafts.append(media_id)


class AmbiguousDraftWechatClient(FakeWechatClient):
    async def create_draft(self, article: dict) -> str:
        self.created_drafts.append(article)
        raise WechatAPIError(
            None,
            "调用微信接口时网络请求失败，远端是否已执行无法确认",
            ambiguous=True,
        )


def _png_bytes() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (32, 24), "green").save(output, "PNG")
    return output.getvalue()


def test_records_survive_reload_and_delete_remote_material(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "strong-password")
    monkeypatch.setenv("WECHAT_APP_ID", "wx-test")
    monkeypatch.setenv("WECHAT_APP_SECRET", "secret-test")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "uploader.sqlite3"))
    monkeypatch.setenv("TEMP_STORAGE_PATH", str(tmp_path / "temp-images"))
    get_settings.cache_clear()

    headers = {"X-Requested-With": "WechatUploader"}
    fake_wechat = FakeWechatClient()
    with TestClient(app) as client:
        app.state.wechat = fake_wechat
        login = client.post(
            "/api/auth/login",
            headers={**headers, "Content-Type": "application/json"},
            json={"username": "admin", "password": "strong-password"},
        )
        assert login.status_code == 200
        upload = client.post(
            "/api/upload",
            headers=headers,
            data={"mode": "material"},
            files={"image": ("photo.png", _png_bytes(), "image/png")},
        )
        assert upload.status_code == 200
        asset = upload.json()["asset"]

        listing = client.get("/api/assets")
        assert listing.json()["items"][0]["id"] == asset["id"]

        deleted = client.post(
            "/api/assets/delete",
            headers={**headers, "Content-Type": "application/json"},
            json={"items": [{"kind": "wechat", "id": asset["id"]}]},
        )
        assert deleted.status_code == 200
        assert deleted.json()["deleted_count"] == 1
        assert fake_wechat.deleted == ["media-1"]
        assert client.get("/api/assets").json()["items"] == []

    get_settings.cache_clear()


def test_admin_login_session_and_logout(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ADMIN_USERNAME", "desk-admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "strong-password")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "uploader.sqlite3"))
    monkeypatch.setenv("TEMP_STORAGE_PATH", str(tmp_path / "temp-images"))
    get_settings.cache_clear()

    headers = {"X-Requested-With": "WechatUploader"}
    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        unauthorized = client.get("/api/auth/me")
        assert unauthorized.status_code == 401
        assert "WWW-Authenticate" not in unauthorized.headers

        invalid = client.post(
            "/api/auth/login",
            headers=headers,
            json={"username": "desk-admin", "password": "wrong-password"},
        )
        assert invalid.status_code == 401

        login = client.post(
            "/api/auth/login",
            headers=headers,
            json={"username": "desk-admin", "password": "strong-password"},
        )
        assert login.status_code == 200
        assert login.json()["user"]["username"] == "desk-admin"
        assert "HttpOnly" in login.headers["set-cookie"]
        assert client.get("/api/auth/me").status_code == 200

        assert client.post("/api/auth/register", headers=headers).status_code == 404
        logout = client.post("/api/auth/logout", headers=headers)
        assert logout.status_code == 200
        assert client.get("/api/auth/me").status_code == 401

    get_settings.cache_clear()


def test_ai_wechat_api_returns_required_json_fields(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("ADMIN_PASSWORD", "strong-password")
    monkeypatch.setenv("WECHAT_APP_ID", "wx-test")
    monkeypatch.setenv("WECHAT_APP_SECRET", "secret-test")
    monkeypatch.setenv("AI_API_KEY", "test-ai-key-at-least-24-characters")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "uploader.sqlite3"))
    monkeypatch.setenv("TEMP_STORAGE_PATH", str(tmp_path / "temp-images"))
    get_settings.cache_clear()

    with TestClient(app) as client:
        fake_wechat = FakeWechatClient()
        app.state.wechat = fake_wechat
        unauthorized = client.post(
            "/api/v1/wechat-images",
            files={"images": ("photo.png", _png_bytes(), "image/png")},
        )
        assert unauthorized.status_code == 401

        uploaded = client.post(
            "/api/v1/wechat-images",
            headers={"Authorization": "Bearer test-ai-key-at-least-24-characters"},
            data={"mode": "material"},
            files={"images": ("photo.png", _png_bytes(), "image/png")},
        )
        assert uploaded.status_code == 201
        assert uploaded.json()["success_count"] == 1
        item = uploaded.json()["items"][0]
        assert item["filename"] == "photo.png"
        assert item["url"] == "https://example.test/material/1"
        assert item["size"] == len(_png_bytes())
        assert item["uploaded_at"]
        assert item["media_id"] == "media-1"
        assert list((tmp_path / "temp-images").iterdir()) == []

    get_settings.cache_clear()


def test_console_saves_encrypted_wechat_account(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ADMIN_PASSWORD", "strong-password")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "uploader.sqlite3"))
    monkeypatch.setenv("TEMP_STORAGE_PATH", str(tmp_path / "temp-images"))
    monkeypatch.delenv("WECHAT_APP_ID", raising=False)
    monkeypatch.delenv("WECHAT_APP_SECRET", raising=False)
    get_settings.cache_clear()

    headers = {"X-Requested-With": "WechatUploader"}
    with TestClient(app) as client:
        client.post(
            "/api/auth/login",
            headers=headers,
            json={"username": "admin", "password": "strong-password"},
        )
        saved = client.put(
            "/api/account",
            headers=headers,
            json={
                "display_name": "测试公众号",
                "account_type": "service",
                "app_id": "wx-console-test",
                "app_secret": "console-secret-value",
            },
        )
        assert saved.status_code == 200
        account = saved.json()["account"]
        assert account["display_name"] == "测试公众号"
        assert account["secret_configured"] is True
        assert "app_secret" not in account
        row = app.state.store.get_wechat_account()
        assert row is not None
        assert row["app_secret_ciphertext"] != "console-secret-value"
        assert app.state.credential_cipher.decrypt(row["app_secret_ciphertext"]) == (
            "console-secret-value"
        )

    get_settings.cache_clear()


def test_publish_api_creates_idempotent_draft(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ADMIN_PASSWORD", "strong-password")
    monkeypatch.setenv("WECHAT_APP_ID", "wx-test")
    monkeypatch.setenv("WECHAT_APP_SECRET", "secret-test")
    monkeypatch.setenv("PUBLISH_API_KEY", "publish-api-key-at-least-24-characters")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "uploader.sqlite3"))
    monkeypatch.setenv("TEMP_STORAGE_PATH", str(tmp_path / "temp-images"))
    get_settings.cache_clear()

    payload = {
        "request_id": "article-20260817-001",
        "title": "测试文章",
        "author": "作者",
        "digest": "摘要",
        "content": (
            '<section><img src="https://mmbiz.qpic.cn/test/image.jpg"></section>'
        ),
        "thumb_media_id": "cover-media-id",
        "need_open_comment": 0,
        "only_fans_can_comment": 0,
    }
    api_headers = {
        "Authorization": "Bearer publish-api-key-at-least-24-characters"
    }
    fake_wechat = FakeWechatClient()
    with TestClient(app) as client:
        app.state.wechat = fake_wechat
        created = client.post(
            "/api/v1/wechat-drafts", headers=api_headers, json=payload
        )
        assert created.status_code == 201
        assert created.json()["media_id"] == "draft-media-1"
        assert created.json()["validation"]["images"] == 1

        repeated = client.post(
            "/api/v1/wechat-drafts", headers=api_headers, json=payload
        )
        assert repeated.status_code == 200
        assert repeated.json()["cached"] is True
        assert len(fake_wechat.created_drafts) == 1

        changed = {**payload, "title": "另一篇文章"}
        conflict = client.post(
            "/api/v1/wechat-drafts", headers=api_headers, json=changed
        )
        assert conflict.status_code == 409

        login_headers = {"X-Requested-With": "WechatUploader"}
        client.post(
            "/api/auth/login",
            headers=login_headers,
            json={"username": "admin", "password": "strong-password"},
        )
        drafts = client.get("/api/drafts")
        assert drafts.json()["count"] == 1
        draft_id = drafts.json()["items"][0]["id"]
        deleted = client.post(
            f"/api/drafts/{draft_id}/delete", headers=login_headers
        )
        assert deleted.status_code == 200
        assert fake_wechat.deleted_drafts == ["draft-media-1"]

    get_settings.cache_clear()


def test_ambiguous_draft_result_is_not_retried(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ADMIN_PASSWORD", "strong-password")
    monkeypatch.setenv("WECHAT_APP_ID", "wx-test")
    monkeypatch.setenv("WECHAT_APP_SECRET", "secret-test")
    monkeypatch.setenv("PUBLISH_API_KEY", "publish-api-key-at-least-24-characters")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "uploader.sqlite3"))
    monkeypatch.setenv("TEMP_STORAGE_PATH", str(tmp_path / "temp-images"))
    get_settings.cache_clear()
    payload = {
        "request_id": "article-ambiguous-001",
        "title": "结果待核实",
        "content": '<img src="https://mmbiz.qpic.cn/test/image.jpg">',
        "thumb_media_id": "cover-media-id",
    }
    headers = {
        "Authorization": "Bearer publish-api-key-at-least-24-characters"
    }
    fake_wechat = AmbiguousDraftWechatClient()

    with TestClient(app) as client:
        app.state.wechat = fake_wechat
        first = client.post("/api/v1/wechat-drafts", headers=headers, json=payload)
        assert first.status_code == 502
        assert app.state.store.get_draft_job_by_request_id(payload["request_id"])[
            "status"
        ] == "unknown"

        repeated = client.post("/api/v1/wechat-drafts", headers=headers, json=payload)
        assert repeated.status_code == 409
        assert len(fake_wechat.created_drafts) == 1

    get_settings.cache_clear()


def test_login_rate_limit_blocks_repeated_failures(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ADMIN_USERNAME", "rate-admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "strong-password")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "uploader.sqlite3"))
    monkeypatch.setenv("TEMP_STORAGE_PATH", str(tmp_path / "temp-images"))
    get_settings.cache_clear()
    _login_failure_buckets.clear()
    headers = {"X-Requested-With": "WechatUploader"}

    with TestClient(app) as client:
        for _ in range(LOGIN_IP_FAILURE_LIMIT):
            response = client.post(
                "/api/auth/login",
                headers=headers,
                json={"username": "rate-admin", "password": "wrong-password"},
            )
            assert response.status_code == 401
        limited = client.post(
            "/api/auth/login",
            headers=headers,
            json={"username": "rate-admin", "password": "strong-password"},
        )
        assert limited.status_code == 429
        assert int(limited.headers["Retry-After"]) > 0

    _login_failure_buckets.clear()
    get_settings.cache_clear()


def test_diagnostics_reports_readiness_without_secrets(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "strong-password")
    monkeypatch.setenv("WECHAT_APP_ID", "wx-test")
    monkeypatch.setenv("WECHAT_APP_SECRET", "secret-test")
    monkeypatch.setenv("AI_API_KEY", "image-api-key-at-least-24-characters")
    monkeypatch.setenv("PUBLISH_API_KEY", "publish-api-key-at-least-24-characters")
    monkeypatch.setenv("TEMP_API_KEY", "temp-api-key-at-least-24-characters")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "uploader.sqlite3"))
    monkeypatch.setenv("TEMP_STORAGE_PATH", str(tmp_path / "temp-images"))
    get_settings.cache_clear()
    headers = {"X-Requested-With": "WechatUploader"}

    with TestClient(app) as client:
        app.state.wechat = FakeWechatClient()
        login = client.post(
            "/api/auth/login",
            headers=headers,
            json={"username": "admin", "password": "strong-password"},
        )
        assert login.status_code == 200
        response = client.post("/api/diagnostics/run", headers=headers)
        assert response.status_code == 200
        payload = response.json()
        assert payload["ready"] is True
        assert {check["id"] for check in payload["checks"]} == {
            "database",
            "wechat",
            "image_api",
            "publish_api",
            "temporary_api",
            "public_url",
        }
        serialized = response.text
        assert "image-api-key-at-least-24-characters" not in serialized
        assert "publish-api-key-at-least-24-characters" not in serialized
        assert "secret-test" not in serialized

    get_settings.cache_clear()


def test_skill_client_config_requires_admin_and_disables_caching(
    tmp_path: Path, monkeypatch
) -> None:
    image_key = "image-api-key-at-least-24-characters"
    publish_key = "publish-api-key-at-least-24-characters"
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "strong-password")
    monkeypatch.setenv("AI_API_KEY", image_key)
    monkeypatch.setenv("PUBLISH_API_KEY", publish_key)
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://console.example.test")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "uploader.sqlite3"))
    monkeypatch.setenv("TEMP_STORAGE_PATH", str(tmp_path / "temp-images"))
    get_settings.cache_clear()
    headers = {"X-Requested-With": "WechatUploader"}

    with TestClient(app) as client:
        unauthorized = client.post("/api/skill-client-config", headers=headers)
        assert unauthorized.status_code == 401

        login = client.post(
            "/api/auth/login",
            headers=headers,
            json={"username": "admin", "password": "strong-password"},
        )
        assert login.status_code == 200
        assert client.post("/api/skill-client-config").status_code == 403

        response = client.post("/api/skill-client-config", headers=headers)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store, private"
        assert response.headers["pragma"] == "no-cache"
        assert response.json() == {
            "configured": True,
            "values": {
                "WECHAT_CONSOLE_URL": "https://console.example.test",
                "WECHAT_IMAGE_API_KEY": image_key,
                "WECHAT_PUBLISH_API_KEY": publish_key,
            },
        }

        assert image_key not in client.get("/api/status").text
        assert publish_key not in client.get("/api/overview").text

    get_settings.cache_clear()
