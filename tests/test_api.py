import hashlib
import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.config import get_settings
from app.main import (
    LOGIN_IP_FAILURE_LIMIT,
    REGISTRATION_IP_LIMIT,
    _login_failure_buckets,
    _registration_buckets,
    app,
)
from app.wechat import WechatAPIError


@pytest.fixture(autouse=True)
def clear_rate_limit_state():
    _login_failure_buckets.clear()
    _registration_buckets.clear()
    yield
    _login_failure_buckets.clear()
    _registration_buckets.clear()


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

        assert client.post("/api/auth/register", headers=headers).status_code == 422
        logout = client.post("/api/auth/logout", headers=headers)
        assert logout.status_code == 200
        assert client.get("/api/auth/me").status_code == 401

    get_settings.cache_clear()


def test_first_run_setup_creates_admin_and_service_keys(
    tmp_path: Path, monkeypatch
) -> None:
    for name in (
        "ADMIN_USERNAME",
        "ADMIN_PASSWORD",
        "AI_API_KEY",
        "PUBLISH_API_KEY",
        "TEMP_API_KEY",
        "CREDENTIALS_ENCRYPTION_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "uploader.sqlite3"))
    monkeypatch.setenv("TEMP_STORAGE_PATH", str(tmp_path / "temp-images"))
    get_settings.cache_clear()

    headers = {"X-Requested-With": "WechatUploader"}
    with TestClient(app) as client:
        assert client.get("/api/setup/status").json() == {
            "configured": False,
            "requires_token": True,
        }
        setup_token = app.state.setup_token
        assert setup_token
        assert (tmp_path / ".wechat-setup-token").read_text().strip() == setup_token
        before_setup = client.post(
            "/api/auth/login",
            headers=headers,
            json={"username": "owner", "password": "first-run-password"},
        )
        assert before_setup.status_code == 409

        rejected_setup = client.post(
            "/api/setup",
            headers=headers,
            json={
                "setup_token": "wrong-setup-token-value",
                "username": "owner",
                "password": "first-run-password",
            },
        )
        assert rejected_setup.status_code == 403

        setup = client.post(
            "/api/setup",
            headers=headers,
            json={
                "setup_token": setup_token,
                "username": " owner ",
                "password": "first-run-password",
            },
        )
        assert setup.status_code == 200
        assert setup.json()["user"] == {"username": "owner", "role": "admin"}
        assert client.get("/api/auth/me").status_code == 200
        assert client.get("/api/setup/status").json() == {
            "configured": True,
            "requires_token": False,
        }
        assert not (tmp_path / ".wechat-setup-token").exists()
        assert client.post(
            "/api/setup",
            headers=headers,
            json={
                "setup_token": setup_token,
                "username": "other",
                "password": "another-password",
            },
        ).status_code == 409

        credentials = app.state.store.get_admin_credentials()
        assert credentials is not None
        assert credentials["username"] == "owner"
        assert credentials["password_hash"].startswith("$argon2id$")
        assert "first-run-password" not in credentials["password_hash"]
        assert app.state.settings.ai_api_configured
        assert app.state.settings.publish_api_configured
        assert app.state.settings.temp_api_configured
        service_credentials = app.state.store.get_service_credentials()
        assert service_credentials is not None
        assert service_credentials["ai_api_key_ciphertext"] != (
            app.state.settings.ai_api_key
        )
        assert service_credentials["publish_api_key_ciphertext"] != (
            app.state.settings.publish_api_key
        )

    with TestClient(app) as restarted_client:
        assert restarted_client.get("/api/setup/status").json() == {
            "configured": True,
            "requires_token": False,
        }
        assert app.state.settings.ai_api_configured
        assert app.state.settings.publish_api_configured
        assert restarted_client.post(
            "/api/auth/login",
            headers=headers,
            json={"username": "owner", "password": "first-run-password"},
        ).status_code == 200

    get_settings.cache_clear()


def test_password_change_is_immediate_and_revokes_all_sessions(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("ADMIN_USERNAME", "desk-admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "strong-password")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "uploader.sqlite3"))
    monkeypatch.setenv("TEMP_STORAGE_PATH", str(tmp_path / "temp-images"))
    get_settings.cache_clear()
    headers = {"X-Requested-With": "WechatUploader"}

    with TestClient(app) as client:
        first_login = client.post(
            "/api/auth/login",
            headers=headers,
            json={"username": "desk-admin", "password": "strong-password"},
        )
        first_token = first_login.cookies.get("wechat_uploader_session")
        second_login = client.post(
            "/api/auth/login",
            headers=headers,
            json={"username": "desk-admin", "password": "strong-password"},
        )
        second_token = second_login.cookies.get("wechat_uploader_session")
        assert first_token and second_token and first_token != second_token

        rejected = client.post(
            "/api/auth/password",
            headers=headers,
            json={
                "current_password": "wrong-password",
                "new_password": "new-strong-password",
            },
        )
        assert rejected.status_code == 400
        assert client.get("/api/auth/me").status_code == 200

        changed = client.post(
            "/api/auth/password",
            headers=headers,
            json={
                "current_password": "strong-password",
                "new_password": "new-strong-password",
            },
        )
        assert changed.status_code == 200
        assert changed.json() == {"password_changed": True, "sessions_revoked": 2}
        assert client.get("/api/auth/me").status_code == 401
        for token in (first_token, second_token):
            token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
            assert app.state.store.get_admin_session(token_hash) is None

        assert client.post(
            "/api/auth/login",
            headers=headers,
            json={"username": "desk-admin", "password": "strong-password"},
        ).status_code == 401
        assert client.post(
            "/api/auth/login",
            headers=headers,
            json={"username": "desk-admin", "password": "new-strong-password"},
        ).status_code == 200

    get_settings.cache_clear()


def test_registration_role_and_password_change_are_user_scoped(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("ADMIN_USERNAME", "owner")
    monkeypatch.setenv("ADMIN_PASSWORD", "owner-password")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "uploader.sqlite3"))
    monkeypatch.setenv("TEMP_STORAGE_PATH", str(tmp_path / "temp-images"))
    get_settings.cache_clear()
    headers = {"X-Requested-With": "WechatUploader"}

    with TestClient(app) as client:
        admin_login = client.post(
            "/api/auth/login",
            headers=headers,
            json={"username": "owner", "password": "owner-password"},
        )
        admin_token = admin_login.cookies.get("wechat_uploader_session")
        assert admin_token

        registered = client.post(
            "/api/auth/register",
            headers=headers,
            json={"username": " alice ", "password": "alice-password"},
        )
        assert registered.status_code == 200
        assert registered.json()["user"] == {"username": "alice", "role": "user"}
        assert client.get("/api/auth/me").json()["user"]["role"] == "user"
        assert client.post("/api/skill-client-config", headers=headers).status_code == 403
        assert client.post(
            "/api/auth/register",
            headers=headers,
            json={"username": "ALICE", "password": "another-password"},
        ).status_code == 409

        first_user_token = registered.cookies.get("wechat_uploader_session")
        second_login = client.post(
            "/api/auth/login",
            headers=headers,
            json={"username": "Alice", "password": "alice-password"},
        )
        second_user_token = second_login.cookies.get("wechat_uploader_session")
        assert first_user_token and second_user_token
        changed = client.post(
            "/api/auth/password",
            headers=headers,
            json={
                "current_password": "alice-password",
                "new_password": "alice-new-password",
            },
        )
        assert changed.status_code == 200
        assert changed.json()["sessions_revoked"] == 2

        client.cookies.clear()
        client.cookies.set("wechat_uploader_session", admin_token)
        assert client.get("/api/auth/me").json()["user"] == {
            "username": "owner",
            "role": "admin",
        }
        assert client.post(
            "/api/auth/login",
            headers=headers,
            json={"username": "alice", "password": "alice-new-password"},
        ).status_code == 200

    get_settings.cache_clear()


def test_multiple_accounts_and_tenant_data_are_isolated(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("ADMIN_USERNAME", "owner")
    monkeypatch.setenv("ADMIN_PASSWORD", "owner-password")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "uploader.sqlite3"))
    monkeypatch.setenv("TEMP_STORAGE_PATH", str(tmp_path / "temp-images"))
    get_settings.cache_clear()
    headers = {"X-Requested-With": "WechatUploader"}
    fake_wechat = FakeWechatClient()

    def use_session(client: TestClient, token: str) -> None:
        client.cookies.clear()
        client.cookies.set("wechat_uploader_session", token)

    def add_account(client: TestClient, name: str, app_id: str) -> dict:
        response = client.post(
            "/api/accounts",
            headers=headers,
            json={
                "display_name": name,
                "account_type": "subscription",
                "app_id": app_id,
                "app_secret": f"secret-{app_id}",
            },
        )
        assert response.status_code == 201
        return response.json()["account"]

    with TestClient(app) as client:
        app.state.wechat = fake_wechat
        alice_register = client.post(
            "/api/auth/register",
            headers=headers,
            json={"username": "alice", "password": "alice-password"},
        )
        alice_token = alice_register.cookies.get("wechat_uploader_session")
        assert alice_token
        alice_first = add_account(client, "Alice 订阅号", "wx-alice-1")
        alice_second = add_account(client, "Alice 服务号", "wx-alice-2")
        listed = client.get("/api/accounts").json()
        assert listed["count"] == 2
        assert listed["active_account_id"] == alice_second["id"]

        activated = client.post(
            f"/api/accounts/{alice_first['id']}/activate", headers=headers
        )
        assert activated.status_code == 200
        assert client.get("/api/account").json()["account"]["id"] == alice_first["id"]

        alice_upload = client.post(
            "/api/upload",
            headers=headers,
            data={"mode": "material"},
            files={"image": ("alice.png", _png_bytes(), "image/png")},
        )
        assert alice_upload.status_code == 200
        alice_asset = alice_upload.json()["asset"]
        alice_temporary = client.post(
            "/api/upload",
            headers=headers,
            data={"mode": "temporary"},
            files={"image": ("alice-temp.png", _png_bytes(), "image/png")},
        ).json()["asset"]

        client.post(f"/api/accounts/{alice_second['id']}/activate", headers=headers)
        second_assets = client.get("/api/assets").json()["items"]
        assert alice_asset["id"] not in {
            item["id"] for item in second_assets if item["kind"] == "wechat"
        }
        assert alice_temporary["id"] in {
            item["id"] for item in second_assets if item["kind"] == "temporary"
        }

        bob_register = client.post(
            "/api/auth/register",
            headers=headers,
            json={"username": "bob", "password": "bob-password-1"},
        )
        bob_token = bob_register.cookies.get("wechat_uploader_session")
        assert bob_token
        bob_account = add_account(client, "Bob 订阅号", "wx-bob-1")
        bob_upload = client.post(
            "/api/upload",
            headers=headers,
            data={"mode": "material"},
            files={"image": ("bob.png", _png_bytes(), "image/png")},
        )
        bob_asset = bob_upload.json()["asset"]
        bob_temporary = client.post(
            "/api/upload",
            headers=headers,
            data={"mode": "temporary"},
            files={"image": ("bob-temp.png", _png_bytes(), "image/png")},
        ).json()["asset"]

        alice_user = app.state.store.get_user_by_username("alice")
        bob_user = app.state.store.get_user_by_username("bob")
        assert alice_user and bob_user
        alice_draft = app.state.store.create_draft_job(
            user_id=alice_user["id"],
            account_id=alice_first["id"],
            request_id="alice-draft-1",
            content_hash="a" * 64,
            title="Alice 草稿",
            author="",
            digest="",
            content="<p>Alice</p>",
            content_source_url="",
            thumb_media_id="alice-cover",
            need_open_comment=0,
            only_fans_can_comment=0,
        )
        app.state.store.update_draft_job(
            alice_draft["id"], status="created", media_id="alice-draft-media"
        )
        bob_draft = app.state.store.create_draft_job(
            user_id=bob_user["id"],
            account_id=bob_account["id"],
            request_id="bob-draft-1",
            content_hash="b" * 64,
            title="Bob 草稿",
            author="",
            digest="",
            content="<p>Bob</p>",
            content_source_url="",
            thumb_media_id="bob-cover",
            need_open_comment=0,
            only_fans_can_comment=0,
        )
        app.state.store.update_draft_job(
            bob_draft["id"], status="created", media_id="bob-draft-media"
        )

        use_session(client, alice_token)
        client.post(f"/api/accounts/{alice_first['id']}/activate", headers=headers)
        assert client.get(
            "/api/status", headers={"X-Wechat-Account-ID": str(bob_account["id"])}
        ).status_code == 404
        assert client.put(
            f"/api/accounts/{bob_account['id']}",
            headers=headers,
            json={
                "display_name": "非法修改",
                "account_type": "service",
                "app_id": "wx-bob-1",
            },
        ).status_code == 404
        assert client.delete(
            f"/api/accounts/{bob_account['id']}", headers=headers
        ).status_code == 404

        alice_items = client.get("/api/assets").json()["items"]
        assert alice_asset["id"] in {
            item["id"] for item in alice_items if item["kind"] == "wechat"
        }
        assert bob_asset["id"] not in {
            item["id"] for item in alice_items if item["kind"] == "wechat"
        }
        assert bob_temporary["id"] not in {
            item["id"] for item in alice_items if item["kind"] == "temporary"
        }
        cross_delete = client.post(
            "/api/assets/delete",
            headers=headers,
            json={"items": [{"kind": "wechat", "id": bob_asset["id"]}]},
        )
        assert cross_delete.status_code == 200
        assert cross_delete.json()["deleted"][0]["missing"] is True
        assert client.get(f"/api/drafts/{bob_draft['id']}").status_code == 404
        assert client.post(
            f"/api/drafts/{bob_draft['id']}/delete", headers=headers
        ).status_code == 404
        assert [item["id"] for item in client.get("/api/drafts").json()["items"]] == [
            alice_draft["id"]
        ]
        alice_visible_draft = client.get("/api/drafts").json()["items"][0]
        assert alice_visible_draft["owner_username"] == "alice"
        assert alice_visible_draft["account_display_name"] == "Alice 订阅号"
        assert alice_visible_draft["can_delete"] is True

        updated = client.put(
            f"/api/accounts/{alice_second['id']}",
            headers=headers,
            json={
                "display_name": "Alice 服务号（新）",
                "account_type": "service",
                "app_id": "wx-alice-2",
            },
        )
        assert updated.status_code == 200
        assert updated.json()["account"]["display_name"] == "Alice 服务号（新）"
        assert client.delete(
            f"/api/accounts/{alice_second['id']}", headers=headers
        ).status_code == 200
        assert client.get("/api/accounts").json()["count"] == 1

        use_session(client, bob_token)
        bob_items = client.get("/api/assets").json()["items"]
        assert bob_asset["id"] in {
            item["id"] for item in bob_items if item["kind"] == "wechat"
        }
        assert alice_temporary["id"] not in {
            item["id"] for item in bob_items if item["kind"] == "temporary"
        }
        assert [item["id"] for item in client.get("/api/drafts").json()["items"]] == [
            bob_draft["id"]
        ]

        client.cookies.clear()
        admin_login = client.post(
            "/api/auth/login",
            headers=headers,
            json={"username": "owner", "password": "owner-password"},
        )
        assert admin_login.status_code == 200
        admin_drafts = client.get("/api/drafts").json()["items"]
        drafts_by_id = {item["id"]: item for item in admin_drafts}
        assert {alice_draft["id"], bob_draft["id"]} <= drafts_by_id.keys()
        assert drafts_by_id[alice_draft["id"]]["owner_username"] == "alice"
        assert (
            drafts_by_id[alice_draft["id"]]["account_display_name"]
            == "Alice 订阅号"
        )
        assert drafts_by_id[bob_draft["id"]]["owner_username"] == "bob"
        assert drafts_by_id[bob_draft["id"]]["account_display_name"] == "Bob 订阅号"
        assert drafts_by_id[alice_draft["id"]]["can_delete"] is False
        assert drafts_by_id[bob_draft["id"]]["can_delete"] is False
        assert client.get(f"/api/drafts/{bob_draft['id']}").status_code == 404
        assert client.post(
            f"/api/drafts/{bob_draft['id']}/delete", headers=headers
        ).status_code == 404

        overview = client.get("/api/overview").json()
        assert overview["counts"]["drafts"] == 2
        assert {item["id"] for item in overview["recent_drafts"]} >= {
            alice_draft["id"],
            bob_draft["id"],
        }

    get_settings.cache_clear()


def test_draft_page_opens_wechat_platform_in_a_new_tab() -> None:
    page = Path("app/static/index.html").read_text(encoding="utf-8")
    assert 'href="https://mp.weixin.qq.com/"' in page
    assert "打开微信公众平台" in page
    assert 'target="_blank"' in page
    assert 'rel="noopener noreferrer"' in page


def test_workspace_switch_clears_user_specific_browser_state() -> None:
    script = Path("app/static/app.js").read_text(encoding="utf-8")
    reset = script.split("function resetWorkspaceState() {", 1)[1].split(
        "\n}\n\nfunction showLogin", 1
    )[0]
    enter = script.split("async function enterWorkspace(user) {", 1)[1].split(
        "\n}\n", 1
    )[0]

    for statement in (
        "state.overview = null;",
        "state.drafts = [];",
        "state.accounts = [];",
        "state.assets = [];",
        "renderAccountList();",
        "renderDrafts();",
        "renderAssets();",
    ):
        assert statement in reset
    assert enter.index("await loadWorkspace();") < enter.index(
        "els.appShell.hidden = false;"
    )


def test_registration_rate_limit_counts_successful_registrations(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("ADMIN_PASSWORD", "strong-password")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "uploader.sqlite3"))
    monkeypatch.setenv("TEMP_STORAGE_PATH", str(tmp_path / "temp-images"))
    get_settings.cache_clear()
    headers = {"X-Requested-With": "WechatUploader"}

    with TestClient(app) as client:
        for index in range(REGISTRATION_IP_LIMIT):
            response = client.post(
                "/api/auth/register",
                headers=headers,
                json={
                    "username": f"member-{index}",
                    "password": "member-password",
                },
            )
            assert response.status_code == 200
        limited = client.post(
            "/api/auth/register",
            headers=headers,
            json={"username": "one-too-many", "password": "member-password"},
        )
        assert limited.status_code == 429

    get_settings.cache_clear()


def test_user_account_and_temporary_storage_quotas(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("ADMIN_PASSWORD", "strong-password")
    monkeypatch.setenv("MAX_USERS", "2")
    monkeypatch.setenv("MAX_ACCOUNTS_PER_USER", "1")
    monkeypatch.setenv("TEMP_USER_STORAGE_MAX_BYTES", "1")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "uploader.sqlite3"))
    monkeypatch.setenv("TEMP_STORAGE_PATH", str(tmp_path / "temp-images"))
    get_settings.cache_clear()
    headers = {"X-Requested-With": "WechatUploader"}

    with TestClient(app) as client:
        registered = client.post(
            "/api/auth/register",
            headers=headers,
            json={"username": "member", "password": "member-password"},
        )
        assert registered.status_code == 200
        user_limit = client.post(
            "/api/auth/register",
            headers=headers,
            json={"username": "extra", "password": "member-password"},
        )
        assert user_limit.status_code == 403

        account_payload = {
            "display_name": "第一个公众号",
            "account_type": "subscription",
            "app_id": "wx-first",
            "app_secret": "secret-first",
        }
        assert client.post(
            "/api/accounts", headers=headers, json=account_payload
        ).status_code == 201
        account_limit = client.post(
            "/api/accounts",
            headers=headers,
            json={**account_payload, "display_name": "第二个公众号", "app_id": "wx-second"},
        )
        assert account_limit.status_code == 403

        temporary = client.post(
            "/api/upload",
            headers=headers,
            data={"mode": "temporary"},
            files={"image": ("temp.png", _png_bytes(), "image/png")},
        )
        assert temporary.status_code == 200
        assert temporary.json()["status"] == "failed"
        assert "当前用户临时图片存储空间" in temporary.json()["errors"][0]
        assert app.state.store.list_temporary_assets(user_id=2) == []

    get_settings.cache_clear()


def test_logged_in_user_can_create_owned_draft(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("ADMIN_PASSWORD", "strong-password")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "uploader.sqlite3"))
    monkeypatch.setenv("TEMP_STORAGE_PATH", str(tmp_path / "temp-images"))
    get_settings.cache_clear()
    headers = {"X-Requested-With": "WechatUploader"}
    fake_wechat = FakeWechatClient()
    payload = {
        "request_id": "member-draft-001",
        "title": "用户草稿",
        "content": '<p><img src="https://mmbiz.qpic.cn/member/image.jpg"></p>',
        "thumb_media_id": "member-cover",
    }

    with TestClient(app) as client:
        app.state.wechat = fake_wechat
        registered = client.post(
            "/api/auth/register",
            headers=headers,
            json={"username": "member", "password": "member-password"},
        )
        assert registered.status_code == 200
        account = client.post(
            "/api/accounts",
            headers=headers,
            json={
                "display_name": "用户订阅号",
                "account_type": "subscription",
                "app_id": "wx-member",
                "app_secret": "secret-member",
            },
        )
        assert account.status_code == 201
        created = client.post("/api/drafts", headers=headers, json=payload)
        assert created.status_code == 201
        listed = client.get("/api/drafts").json()
        assert listed["count"] == 1
        assert listed["items"][0]["owner_username"] == "member"
        assert listed["items"][0]["account_display_name"] == "用户订阅号"

    get_settings.cache_clear()


def test_admin_can_delete_own_draft_from_inactive_account_and_paginate(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("ADMIN_PASSWORD", "strong-password")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "uploader.sqlite3"))
    monkeypatch.setenv("TEMP_STORAGE_PATH", str(tmp_path / "temp-images"))
    get_settings.cache_clear()
    headers = {"X-Requested-With": "WechatUploader"}
    fake_wechat = FakeWechatClient()

    with TestClient(app) as client:
        app.state.wechat = fake_wechat
        assert client.post(
            "/api/auth/login",
            headers=headers,
            json={"username": "admin", "password": "strong-password"},
        ).status_code == 200
        account_ids = []
        for suffix in ("one", "two"):
            created_account = client.post(
                "/api/accounts",
                headers=headers,
                json={
                    "display_name": f"公众号 {suffix}",
                    "account_type": "subscription",
                    "app_id": f"wx-{suffix}",
                    "app_secret": f"secret-{suffix}",
                },
            )
            account_ids.append(created_account.json()["account"]["id"])
        admin = app.state.store.get_admin_user()
        assert admin is not None
        draft_ids = []
        for index in range(3):
            draft = app.state.store.create_draft_job(
                user_id=admin["id"],
                account_id=account_ids[0],
                request_id=f"admin-draft-{index}",
                content_hash=str(index) * 64,
                title=f"管理员草稿 {index}",
                author="",
                digest="",
                content="<p>draft</p>",
                content_source_url="",
                thumb_media_id="cover",
                need_open_comment=0,
                only_fans_can_comment=0,
            )
            app.state.store.update_draft_job(
                draft["id"], status="created", media_id=f"media-{index}"
            )
            draft_ids.append(draft["id"])

        first_page = client.get("/api/drafts?limit=2&offset=0").json()
        second_page = client.get("/api/drafts?limit=2&offset=2").json()
        assert first_page["count"] == 3
        assert first_page["has_more"] is True
        assert len(first_page["items"]) == 2
        assert second_page["has_more"] is False
        assert len(second_page["items"]) == 1
        target = next(
            item for item in first_page["items"] + second_page["items"]
            if item["id"] == draft_ids[0]
        )
        assert target["can_delete"] is True
        deleted = client.post(
            f"/api/drafts/{draft_ids[0]}/delete", headers=headers
        )
        assert deleted.status_code == 200
        assert fake_wechat.deleted_drafts == ["media-0"]

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


def test_ai_api_can_target_a_selected_admin_account(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("ADMIN_PASSWORD", "strong-password")
    monkeypatch.setenv("AI_API_KEY", "test-ai-key-at-least-24-characters")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "uploader.sqlite3"))
    monkeypatch.setenv("TEMP_STORAGE_PATH", str(tmp_path / "temp-images"))
    monkeypatch.delenv("WECHAT_APP_ID", raising=False)
    monkeypatch.delenv("WECHAT_APP_SECRET", raising=False)
    get_settings.cache_clear()
    web_headers = {"X-Requested-With": "WechatUploader"}
    api_headers = {"Authorization": "Bearer test-ai-key-at-least-24-characters"}

    with TestClient(app) as client:
        app.state.wechat = FakeWechatClient()
        assert client.post(
            "/api/auth/login",
            headers=web_headers,
            json={"username": "admin", "password": "strong-password"},
        ).status_code == 200
        account_ids = []
        for suffix in ("one", "two"):
            created = client.post(
                "/api/accounts",
                headers=web_headers,
                json={
                    "display_name": f"公众号 {suffix}",
                    "account_type": "subscription",
                    "app_id": f"wx-{suffix}",
                    "app_secret": f"secret-{suffix}",
                },
            )
            account_ids.append(created.json()["account"]["id"])

        selected_headers = {
            **api_headers,
            "X-Wechat-Account-ID": str(account_ids[0]),
        }
        uploaded = client.post(
            "/api/v1/wechat-images",
            headers=selected_headers,
            data={"mode": "material"},
            files={"images": ("selected.png", _png_bytes(), "image/png")},
        )
        assert uploaded.status_code == 201
        admin = app.state.store.get_admin_user()
        assert admin is not None
        assert len(
            app.state.store.list_assets(
                user_id=admin["id"], account_id=account_ids[0]
            )
        ) == 1
        assert app.state.store.list_assets(
            user_id=admin["id"], account_id=account_ids[1]
        ) == []
        assert client.post(
            "/api/v1/wechat-images",
            headers={**api_headers, "X-Wechat-Account-ID": "999999"},
            data={"mode": "material"},
            files={"images": ("missing.png", _png_bytes(), "image/png")},
        ).status_code == 404

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
