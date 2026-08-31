import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier

from app.database import CURRENT_SCHEMA_VERSION, AssetStore


def test_user_limit_is_atomic_across_concurrent_creates(tmp_path: Path) -> None:
    store = AssetStore(tmp_path / "uploader.sqlite3")
    store.initialize()
    assert store.initialize_admin_credentials("admin", "admin-hash")
    barrier = Barrier(5)

    def create(index: int) -> dict | None:
        barrier.wait()
        return store.create_user(
            username=f"member-{index}",
            password_hash="member-hash",
            max_users=2,
        )

    with ThreadPoolExecutor(max_workers=5) as executor:
        results = list(executor.map(create, range(5)))

    assert sum(result is not None for result in results) == 1
    assert store.count_users() == 2


def test_asset_store_closes_connections_and_deletes_rows(tmp_path: Path) -> None:
    database_path = tmp_path / "uploader.sqlite3"
    store = AssetStore(database_path)
    store.initialize()
    row = store.upsert_source(
        sha256="a" * 64,
        filename="photo.png",
        content_type="image/png",
        original_bytes=10,
        width=1,
        height=1,
    )

    assert store.delete_asset(row["id"])
    assert store.list_assets() == []

    expires_at = (datetime.now(UTC) + timedelta(hours=1)).isoformat(timespec="seconds")
    store.create_admin_session(
        token_hash="b" * 64,
        username="admin",
        expires_at=expires_at,
    )
    session = store.get_admin_session("b" * 64)
    assert session is not None
    assert session["username"] == "admin"
    assert store.delete_admin_session("b" * 64)
    assert store.get_admin_session("b" * 64) is None

    database_path.unlink()
    assert not database_path.exists()


def test_client_pairings_keep_recent_tokens_and_password_revokes_all(
    tmp_path: Path,
) -> None:
    store = AssetStore(tmp_path / "uploader.sqlite3")
    store.initialize()
    assert store.initialize_admin_credentials("admin", "old-password-hash")
    admin = store.get_admin_user()
    assert admin is not None
    user_id = int(admin["id"])
    token_hashes = [f"{index:064x}" for index in range(17)]

    for token_hash in token_hashes:
        store.create_client_token(token_hash=token_hash, user_id=user_id)

    assert store.get_client_token(token_hashes[0]) is None
    assert all(
        store.get_client_token(token_hash) is not None
        for token_hash in token_hashes[1:]
    )

    store.change_user_password_hash(user_id, "new-password-hash")

    assert all(
        store.get_client_token(token_hash) is None for token_hash in token_hashes
    )


def test_v4_replaces_legacy_service_keys_with_hashed_client_tokens(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "v3.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE service_credentials (
                id INTEGER PRIMARY KEY,
                ai_api_key_ciphertext TEXT NOT NULL,
                publish_api_key_ciphertext TEXT NOT NULL,
                temp_api_key_ciphertext TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute("PRAGMA user_version = 3")

    store = AssetStore(database_path)
    store.initialize()
    now = datetime.now(UTC).isoformat(timespec="seconds")
    with sqlite3.connect(database_path) as connection:
        cursor = connection.execute(
            """
            INSERT INTO users (username, password_hash, role, created_at, updated_at)
            VALUES ('admin', 'password-hash', 'admin', ?, ?)
            """,
            (now, now),
        )
        user_id = int(cursor.lastrowid)
    store.replace_client_token(token_hash="a" * 64, user_id=user_id)
    assert store.get_client_token("a" * 64) is not None
    store.replace_client_token(token_hash="b" * 64, user_id=user_id)
    assert store.get_client_token("a" * 64) is None
    assert store.get_client_token("b" * 64) is not None

    with sqlite3.connect(database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert "service_credentials" not in tables
    assert "client_tokens" in tables


def test_legacy_database_is_backed_up_and_versioned(tmp_path: Path) -> None:
    database_path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE legacy_data (value TEXT)")
        connection.execute("INSERT INTO legacy_data VALUES ('preserved')")

    store = AssetStore(database_path)
    store.initialize()

    assert store.schema_version() == CURRENT_SCHEMA_VERSION
    assert store.last_migration_backup is not None
    assert store.last_migration_backup.is_file()
    assert store.health_check() == {
        "ok": True,
        "writable": True,
        "quick_check": "ok",
        "schema_version": CURRENT_SCHEMA_VERSION,
        "supported_schema_version": CURRENT_SCHEMA_VERSION,
    }
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT value FROM legacy_data").fetchone()[0] == (
            "preserved"
        )
        assert connection.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()[0] == CURRENT_SCHEMA_VERSION


def test_v1_migration_claims_legacy_data_after_admin_is_created(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "v1.sqlite3"
    now = datetime.now(UTC).isoformat(timespec="seconds")
    expires_at = (datetime.now(UTC) + timedelta(days=1)).isoformat(
        timespec="seconds"
    )
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE assets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sha256 TEXT NOT NULL UNIQUE,
                filename TEXT NOT NULL,
                content_type TEXT NOT NULL,
                original_bytes INTEGER NOT NULL,
                processed_bytes INTEGER,
                width INTEGER,
                height INTEGER,
                media_id TEXT,
                material_url TEXT,
                article_url TEXT,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE temporary_assets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token TEXT NOT NULL UNIQUE,
                sha256 TEXT NOT NULL,
                filename TEXT NOT NULL,
                stored_name TEXT NOT NULL UNIQUE,
                content_type TEXT NOT NULL,
                original_bytes INTEGER NOT NULL,
                processed_bytes INTEGER NOT NULL,
                width INTEGER NOT NULL,
                height INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );
            CREATE TABLE admin_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token_hash TEXT NOT NULL UNIQUE,
                username TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );
            CREATE TABLE wechat_account (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                display_name TEXT NOT NULL,
                account_type TEXT NOT NULL,
                app_id TEXT NOT NULL,
                app_secret_ciphertext TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE draft_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id TEXT NOT NULL UNIQUE,
                content_hash TEXT NOT NULL,
                title TEXT NOT NULL,
                author TEXT NOT NULL,
                digest TEXT NOT NULL,
                content TEXT NOT NULL,
                content_source_url TEXT NOT NULL,
                thumb_media_id TEXT NOT NULL,
                need_open_comment INTEGER NOT NULL,
                only_fans_can_comment INTEGER NOT NULL,
                media_id TEXT,
                status TEXT NOT NULL,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            );
            PRAGMA user_version = 1;
            """
        )
        connection.execute("INSERT INTO schema_migrations VALUES (1, ?)", (now,))
        connection.execute(
            "INSERT INTO wechat_account VALUES (1, ?, ?, ?, ?, ?, ?)",
            ("旧公众号", "subscription", "wx-v1", "encrypted", now, now),
        )
        connection.execute(
            """
            INSERT INTO assets (
                sha256, filename, content_type, original_bytes, media_id,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("a" * 64, "v1.png", "image/png", 100, "media-v1", now, now),
        )
        connection.execute(
            """
            INSERT INTO temporary_assets (
                token, sha256, filename, stored_name, content_type,
                original_bytes, processed_bytes, width, height,
                created_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "temporary-v1-token",
                "b" * 64,
                "temporary-v1.png",
                "temporary-v1.png",
                "image/png",
                100,
                80,
                10,
                10,
                now,
                expires_at,
            ),
        )
        connection.execute(
            """
            INSERT INTO draft_jobs (
                request_id, content_hash, title, author, digest, content,
                content_source_url, thumb_media_id, need_open_comment,
                only_fans_can_comment, media_id, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "v1-draft",
                "c" * 64,
                "v1 草稿",
                "",
                "",
                "<p>v1</p>",
                "",
                "cover",
                0,
                0,
                "draft-media-v1",
                "created",
                now,
                now,
            ),
        )

    store = AssetStore(database_path)
    store.initialize()
    assert store.get_admin_user() is None
    assert store.initialize_admin_credentials("v1-admin", "password-hash")

    admin = store.get_admin_user()
    assert admin is not None
    accounts = store.list_official_accounts(admin["id"])
    assert len(accounts) == 1
    assert accounts[0]["app_id"] == "wx-v1"
    assert store.list_assets(
        user_id=admin["id"], account_id=accounts[0]["id"]
    )[0]["filename"] == "v1.png"
    assert store.list_temporary_assets(user_id=admin["id"])[0]["filename"] == (
        "temporary-v1.png"
    )
    assert store.list_draft_jobs(
        user_id=admin["id"], account_id=accounts[0]["id"]
    )[0]["request_id"] == "v1-draft"


def test_v2_migration_assigns_legacy_data_to_admin_and_revokes_sessions(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "v2.sqlite3"
    now = datetime.now(UTC).isoformat(timespec="seconds")
    expires_at = (datetime.now(UTC) + timedelta(days=1)).isoformat(
        timespec="seconds"
    )
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE admin_credentials (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                username TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE admin_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token_hash TEXT NOT NULL UNIQUE,
                username TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );
            CREATE TABLE wechat_account (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                display_name TEXT NOT NULL,
                account_type TEXT NOT NULL,
                app_id TEXT NOT NULL,
                app_secret_ciphertext TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE assets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sha256 TEXT NOT NULL UNIQUE,
                filename TEXT NOT NULL,
                content_type TEXT NOT NULL,
                original_bytes INTEGER NOT NULL,
                processed_bytes INTEGER,
                width INTEGER,
                height INTEGER,
                media_id TEXT,
                material_url TEXT,
                article_url TEXT,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE temporary_assets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token TEXT NOT NULL UNIQUE,
                sha256 TEXT NOT NULL,
                filename TEXT NOT NULL,
                stored_name TEXT NOT NULL UNIQUE,
                content_type TEXT NOT NULL,
                original_bytes INTEGER NOT NULL,
                processed_bytes INTEGER NOT NULL,
                width INTEGER NOT NULL,
                height INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );
            CREATE TABLE draft_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id TEXT NOT NULL UNIQUE,
                content_hash TEXT NOT NULL,
                title TEXT NOT NULL,
                author TEXT NOT NULL,
                digest TEXT NOT NULL,
                content TEXT NOT NULL,
                content_source_url TEXT NOT NULL,
                thumb_media_id TEXT NOT NULL,
                need_open_comment INTEGER NOT NULL,
                only_fans_can_comment INTEGER NOT NULL,
                media_id TEXT,
                status TEXT NOT NULL,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            PRAGMA user_version = 2;
            """
        )
        connection.execute(
            "INSERT INTO admin_credentials VALUES (1, ?, ?, ?, ?)",
            ("legacy-admin", "legacy-hash", now, now),
        )
        connection.execute(
            "INSERT INTO admin_sessions "
            "(token_hash, username, created_at, expires_at) VALUES (?, ?, ?, ?)",
            ("legacy-token", "legacy-admin", now, expires_at),
        )
        connection.execute(
            "INSERT INTO wechat_account VALUES (1, ?, ?, ?, ?, ?, ?)",
            ("旧公众号", "subscription", "wx-legacy", "encrypted", now, now),
        )
        connection.execute(
            """
            INSERT INTO assets (
                sha256, filename, content_type, original_bytes, media_id,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("a" * 64, "legacy.png", "image/png", 100, "media-legacy", now, now),
        )
        connection.execute(
            """
            INSERT INTO temporary_assets (
                token, sha256, filename, stored_name, content_type,
                original_bytes, processed_bytes, width, height,
                created_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "temporary-legacy-token",
                "b" * 64,
                "temporary.png",
                "temporary.png",
                "image/png",
                100,
                80,
                10,
                10,
                now,
                expires_at,
            ),
        )
        connection.execute(
            """
            INSERT INTO draft_jobs (
                request_id, content_hash, title, author, digest, content,
                content_source_url, thumb_media_id, need_open_comment,
                only_fans_can_comment, media_id, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy-draft",
                "c" * 64,
                "旧草稿",
                "",
                "",
                "<p>legacy</p>",
                "",
                "cover",
                0,
                0,
                "draft-media",
                "created",
                now,
                now,
            ),
        )

    store = AssetStore(database_path)
    store.initialize()

    admin = store.get_admin_user()
    assert admin is not None
    assert admin["username"] == "legacy-admin"
    assert admin["role"] == "admin"
    accounts = store.list_official_accounts(admin["id"])
    assert len(accounts) == 1
    assert accounts[0]["app_id"] == "wx-legacy"
    assert store.get_active_official_account(admin["id"])["id"] == accounts[0]["id"]
    assert store.list_assets(
        user_id=admin["id"], account_id=accounts[0]["id"]
    )[0]["filename"] == "legacy.png"
    assert store.list_temporary_assets(user_id=admin["id"])[0]["filename"] == (
        "temporary.png"
    )
    assert store.list_draft_jobs(
        user_id=admin["id"], account_id=accounts[0]["id"]
    )[0]["request_id"] == "legacy-draft"
    assert store.get_admin_session("legacy-token") is None
