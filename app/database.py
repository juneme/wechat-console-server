from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

CURRENT_SCHEMA_VERSION = 4


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class AssetStore:
    def __init__(self, path: Path):
        self.path = path
        self.last_migration_backup: Path | None = None

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            current_version = int(
                connection.execute("PRAGMA user_version").fetchone()[0]
            )
            if current_version > CURRENT_SCHEMA_VERSION:
                raise RuntimeError(
                    "数据库版本高于当前程序支持范围，请升级程序后再启动"
                )
            existing_tables = connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
            if current_version < CURRENT_SCHEMA_VERSION and existing_tables:
                timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
                backup_path = self.path.with_name(
                    f"{self.path.stem}.schema-v{current_version}-to-v"
                    f"{CURRENT_SCHEMA_VERSION}-{timestamp}.sqlite3"
                )
                with sqlite3.connect(backup_path) as backup:
                    connection.backup(backup)
                self.last_migration_backup = backup_path

            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS assets (
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
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_assets_updated_at ON assets(updated_at DESC)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS temporary_assets (
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
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_temporary_assets_expires_at "
                "ON temporary_assets(expires_at)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS admin_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    token_hash TEXT NOT NULL UNIQUE,
                    username TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_admin_sessions_expires_at "
                "ON admin_sessions(expires_at)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS admin_credentials (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    username TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL CHECK (role IN ('admin', 'user')),
                    active_account_id INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS official_accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    display_name TEXT NOT NULL,
                    account_type TEXT NOT NULL CHECK (
                        account_type IN ('subscription', 'service')
                    ),
                    app_id TEXT NOT NULL,
                    app_secret_ciphertext TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(user_id, app_id)
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_official_accounts_user "
                "ON official_accounts(user_id, updated_at DESC)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS wechat_account (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    display_name TEXT NOT NULL,
                    account_type TEXT NOT NULL,
                    app_id TEXT NOT NULL,
                    app_secret_ciphertext TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS draft_jobs (
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
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_draft_jobs_updated_at "
                "ON draft_jobs(updated_at DESC)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                )
                """
            )
            if current_version < 1:
                connection.execute(
                    "INSERT OR IGNORE INTO schema_migrations (version, applied_at) "
                    "VALUES (1, ?)",
                    (_now(),),
                )
                connection.execute("PRAGMA user_version = 1")
            if current_version < 2:
                connection.execute(
                    "INSERT OR IGNORE INTO schema_migrations (version, applied_at) "
                    "VALUES (2, ?)",
                    (_now(),),
                )
                connection.execute("PRAGMA user_version = 2")
            if current_version < 3:
                self._migrate_to_v3(connection)
                connection.execute(
                    "INSERT OR IGNORE INTO schema_migrations (version, applied_at) "
                    "VALUES (3, ?)",
                    (_now(),),
                )
                connection.execute("PRAGMA user_version = 3")
            if current_version < 4:
                self._migrate_to_v4(connection)
                connection.execute(
                    "INSERT OR IGNORE INTO schema_migrations (version, applied_at) "
                    "VALUES (4, ?)",
                    (_now(),),
                )
                connection.execute("PRAGMA user_version = 4")

    def _migrate_to_v3(self, connection: sqlite3.Connection) -> None:
        now = _now()
        admin = connection.execute(
            "SELECT username, password_hash, created_at, updated_at "
            "FROM admin_credentials WHERE id = 1"
        ).fetchone()
        if admin:
            connection.execute(
                """
                INSERT OR IGNORE INTO users (
                    username, password_hash, role, created_at, updated_at
                ) VALUES (?, ?, 'admin', ?, ?)
                """,
                (
                    admin["username"],
                    admin["password_hash"],
                    admin["created_at"],
                    admin["updated_at"],
                ),
            )
        admin_user = connection.execute(
            "SELECT id FROM users WHERE role = 'admin' ORDER BY id LIMIT 1"
        ).fetchone()
        admin_user_id = int(admin_user["id"]) if admin_user else None

        legacy_account = connection.execute(
            "SELECT * FROM wechat_account WHERE id = 1"
        ).fetchone()
        if legacy_account and admin_user_id is not None:
            connection.execute(
                """
                INSERT OR IGNORE INTO official_accounts (
                    user_id, display_name, account_type, app_id,
                    app_secret_ciphertext, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    admin_user_id,
                    legacy_account["display_name"],
                    legacy_account["account_type"],
                    legacy_account["app_id"],
                    legacy_account["app_secret_ciphertext"],
                    legacy_account["created_at"],
                    legacy_account["updated_at"],
                ),
            )
        first_account = (
            connection.execute(
                "SELECT id FROM official_accounts WHERE user_id = ? ORDER BY id LIMIT 1",
                (admin_user_id,),
            ).fetchone()
            if admin_user_id is not None
            else None
        )
        account_id = int(first_account["id"]) if first_account else None
        if admin_user_id is not None and account_id is not None:
            connection.execute(
                "UPDATE users SET active_account_id = ?, updated_at = ? WHERE id = ?",
                (account_id, now, admin_user_id),
            )

        connection.execute("DROP TABLE admin_sessions")
        connection.execute(
            """
            CREATE TABLE admin_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token_hash TEXT NOT NULL UNIQUE,
                user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                username TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "CREATE INDEX idx_admin_sessions_expires_at "
            "ON admin_sessions(expires_at)"
        )

        connection.execute("DROP INDEX IF EXISTS idx_assets_updated_at")
        connection.execute("ALTER TABLE assets RENAME TO assets_v2")
        connection.execute(
            """
            CREATE TABLE assets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                account_id INTEGER REFERENCES official_accounts(id) ON DELETE CASCADE,
                sha256 TEXT NOT NULL,
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
                updated_at TEXT NOT NULL,
                UNIQUE(account_id, sha256)
            )
            """
        )
        connection.execute(
            """
            INSERT INTO assets (
                id, user_id, account_id, sha256, filename, content_type,
                original_bytes, processed_bytes, width, height, media_id,
                material_url, article_url, last_error, created_at, updated_at
            )
            SELECT id, ?, ?, sha256, filename, content_type, original_bytes,
                   processed_bytes, width, height, media_id, material_url,
                   article_url, last_error, created_at, updated_at
            FROM assets_v2
            """,
            (admin_user_id, account_id),
        )
        connection.execute("DROP TABLE assets_v2")
        connection.execute(
            "CREATE INDEX idx_assets_updated_at ON assets(updated_at DESC)"
        )
        connection.execute(
            "CREATE INDEX idx_assets_account ON assets(user_id, account_id, updated_at DESC)"
        )

        connection.execute("ALTER TABLE temporary_assets ADD COLUMN user_id INTEGER")
        if admin_user_id is not None:
            connection.execute(
                "UPDATE temporary_assets SET user_id = ? WHERE user_id IS NULL",
                (admin_user_id,),
            )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_temporary_assets_user "
            "ON temporary_assets(user_id, created_at DESC)"
        )

        connection.execute("DROP INDEX IF EXISTS idx_draft_jobs_updated_at")
        connection.execute("ALTER TABLE draft_jobs RENAME TO draft_jobs_v2")
        connection.execute(
            """
            CREATE TABLE draft_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                account_id INTEGER REFERENCES official_accounts(id) ON DELETE CASCADE,
                request_id TEXT NOT NULL,
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
                updated_at TEXT NOT NULL,
                UNIQUE(user_id, account_id, request_id)
            )
            """
        )
        connection.execute(
            """
            INSERT INTO draft_jobs (
                id, user_id, account_id, request_id, content_hash, title,
                author, digest, content, content_source_url, thumb_media_id,
                need_open_comment, only_fans_can_comment, media_id, status,
                last_error, created_at, updated_at
            )
            SELECT id, ?, ?, request_id, content_hash, title, author, digest,
                   content, content_source_url, thumb_media_id,
                   need_open_comment, only_fans_can_comment, media_id, status,
                   last_error, created_at, updated_at
            FROM draft_jobs_v2
            """,
            (admin_user_id, account_id),
        )
        connection.execute("DROP TABLE draft_jobs_v2")
        connection.execute(
            "CREATE INDEX idx_draft_jobs_updated_at ON draft_jobs(updated_at DESC)"
        )
        connection.execute(
            "CREATE INDEX idx_draft_jobs_account "
            "ON draft_jobs(user_id, account_id, updated_at DESC)"
        )

    def _migrate_to_v4(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE client_tokens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token_hash TEXT NOT NULL UNIQUE,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at TEXT NOT NULL,
                last_used_at TEXT
            )
            """
        )
        connection.execute(
            "CREATE INDEX idx_client_tokens_user ON client_tokens(user_id)"
        )
        connection.execute("DROP TABLE IF EXISTS service_credentials")

    def schema_version(self) -> int:
        with self._connect() as connection:
            return int(connection.execute("PRAGMA user_version").fetchone()[0])

    def health_check(self) -> dict[str, Any]:
        connection = sqlite3.connect(self.path, timeout=5)
        try:
            quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
            schema_version = int(
                connection.execute("PRAGMA user_version").fetchone()[0]
            )
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "CREATE TABLE __wechat_console_write_probe (value INTEGER)"
            )
            connection.rollback()
        finally:
            connection.close()
        return {
            "ok": quick_check == "ok",
            "writable": True,
            "quick_check": quick_check,
            "schema_version": schema_version,
            "supported_schema_version": CURRENT_SCHEMA_VERSION,
        }

    def create_admin_session(
        self,
        *,
        token_hash: str,
        username: str,
        expires_at: str,
        user_id: int | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM admin_sessions WHERE expires_at <= ?", (_now(),)
            )
            connection.execute(
                """
                INSERT INTO admin_sessions (
                    token_hash, user_id, username, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (token_hash, user_id, username, _now(), expires_at),
            )

    def get_user_by_id(self, user_id: int) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE id = ?", (user_id,)
            ).fetchone()
        return dict(row) if row else None

    def get_user_by_username(self, username: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE username = ? COLLATE NOCASE",
                (username,),
            ).fetchone()
        return dict(row) if row else None

    def get_admin_user(self) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE role = 'admin' ORDER BY id LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    def create_user(
        self,
        *,
        username: str,
        password_hash: str,
        role: str = "user",
        max_users: int | None = None,
    ) -> dict[str, Any] | None:
        now = _now()
        try:
            with self._connect() as connection:
                if max_users is not None:
                    connection.execute("BEGIN IMMEDIATE")
                    total = int(
                        connection.execute(
                            "SELECT COUNT(*) FROM users"
                        ).fetchone()[0]
                    )
                    if total >= max_users:
                        return None
                cursor = connection.execute(
                    """
                    INSERT INTO users (
                        username, password_hash, role, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (username, password_hash, role, now, now),
                )
                row = connection.execute(
                    "SELECT * FROM users WHERE id = ?", (cursor.lastrowid,)
                ).fetchone()
        except sqlite3.IntegrityError:
            return None
        return dict(row) if row else None

    def count_users(self) -> int:
        with self._connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS total FROM users").fetchone()
        return int(row["total"]) if row else 0

    def _claim_unassigned_legacy_data(
        self, connection: sqlite3.Connection, user_id: int
    ) -> None:
        account = connection.execute(
            "SELECT id FROM official_accounts WHERE user_id = ? ORDER BY id LIMIT 1",
            (user_id,),
        ).fetchone()
        if account is None:
            legacy = connection.execute(
                "SELECT * FROM wechat_account WHERE id = 1"
            ).fetchone()
            if legacy:
                cursor = connection.execute(
                    """
                    INSERT INTO official_accounts (
                        user_id, display_name, account_type, app_id,
                        app_secret_ciphertext, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        legacy["display_name"],
                        legacy["account_type"],
                        legacy["app_id"],
                        legacy["app_secret_ciphertext"],
                        legacy["created_at"],
                        legacy["updated_at"],
                    ),
                )
                account = {"id": int(cursor.lastrowid)}
        account_id = int(account["id"]) if account else None
        if account_id is not None:
            connection.execute(
                "UPDATE users SET active_account_id = ?, updated_at = ? WHERE id = ?",
                (account_id, _now(), user_id),
            )
            connection.execute(
                "UPDATE assets SET user_id = ?, account_id = ? "
                "WHERE user_id IS NULL AND account_id IS NULL",
                (user_id, account_id),
            )
            connection.execute(
                "UPDATE draft_jobs SET user_id = ?, account_id = ? "
                "WHERE user_id IS NULL AND account_id IS NULL",
                (user_id, account_id),
            )
        connection.execute(
            "UPDATE temporary_assets SET user_id = ? WHERE user_id IS NULL",
            (user_id,),
        )

    def get_admin_credentials(self) -> dict[str, Any] | None:
        return self.get_admin_user()

    def initialize_admin_credentials(self, username: str, password_hash: str) -> bool:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    """
                    INSERT INTO users (
                        username, password_hash, role, created_at, updated_at
                    ) VALUES (?, ?, 'admin', ?, ?)
                    """,
                    (username, password_hash, now, now),
                )
                self._claim_unassigned_legacy_data(
                    connection, int(cursor.lastrowid)
                )
        except sqlite3.IntegrityError:
            return False
        return True

    def replace_client_token(self, *, token_hash: str, user_id: int) -> None:
        now = _now()
        with self._connect() as connection:
            connection.execute("DELETE FROM client_tokens WHERE user_id = ?", (user_id,))
            connection.execute(
                """
                INSERT INTO client_tokens (token_hash, user_id, created_at)
                VALUES (?, ?, ?)
                """,
                (token_hash, user_id, now),
            )

    def get_client_token(self, token_hash: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT client_tokens.*, users.username, users.role
                FROM client_tokens
                JOIN users ON users.id = client_tokens.user_id
                WHERE client_tokens.token_hash = ?
                """,
                (token_hash,),
            ).fetchone()
            if row:
                connection.execute(
                    "UPDATE client_tokens SET last_used_at = ? WHERE id = ?",
                    (_now(), row["id"]),
                )
        return dict(row) if row else None

    def revoke_client_tokens(self, user_id: int) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM client_tokens WHERE user_id = ?", (user_id,)
            )
        return cursor.rowcount

    def change_admin_password_hash(self, password_hash: str) -> int:
        admin = self.get_admin_user()
        if admin is None:
            raise RuntimeError("管理员凭据尚未初始化")
        return self.change_user_password_hash(int(admin["id"]), password_hash)

    def change_user_password_hash(self, user_id: int, password_hash: str) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?",
                (password_hash, _now(), user_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("管理员凭据尚未初始化")
            revoked = connection.execute(
                "DELETE FROM admin_sessions WHERE user_id = ?", (user_id,)
            ).rowcount
            connection.execute("DELETE FROM client_tokens WHERE user_id = ?", (user_id,))
        return revoked

    def get_admin_session(
        self, token_hash: str, *, active_after: str | None = None
    ) -> dict[str, Any] | None:
        cutoff = active_after or _now()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM admin_sessions
                WHERE token_hash = ? AND expires_at > ?
                """,
                (token_hash, cutoff),
            ).fetchone()
        return dict(row) if row else None

    def delete_admin_session(self, token_hash: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM admin_sessions WHERE token_hash = ?", (token_hash,)
            )
        return cursor.rowcount > 0

    def delete_expired_admin_sessions(self, *, expired_at: str | None = None) -> int:
        cutoff = expired_at or _now()
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM admin_sessions WHERE expires_at <= ?", (cutoff,)
            )
        return cursor.rowcount

    def get_by_hash(
        self,
        sha256: str,
        *,
        user_id: int | None = None,
        account_id: int | None = None,
    ) -> dict[str, Any] | None:
        query = "SELECT * FROM assets WHERE sha256 = ?"
        values: list[Any] = [sha256]
        if user_id is not None:
            query += " AND user_id = ?"
            values.append(user_id)
        if account_id is not None:
            query += " AND account_id = ?"
            values.append(account_id)
        with self._connect() as connection:
            row = connection.execute(query, values).fetchone()
        return dict(row) if row else None

    def get_asset(
        self, asset_id: int, *, user_id: int | None = None
    ) -> dict[str, Any] | None:
        query = "SELECT * FROM assets WHERE id = ?"
        values: tuple[Any, ...] = (asset_id,)
        if user_id is not None:
            query += " AND user_id = ?"
            values = (asset_id, user_id)
        with self._connect() as connection:
            row = connection.execute(query, values).fetchone()
        return dict(row) if row else None

    def upsert_source(
        self,
        *,
        sha256: str,
        filename: str,
        content_type: str,
        original_bytes: int,
        width: int,
        height: int,
        user_id: int | None = None,
        account_id: int | None = None,
    ) -> dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO assets (
                    user_id, account_id, sha256, filename, content_type, original_bytes,
                    width, height, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id, sha256) DO UPDATE SET
                    filename = excluded.filename,
                    content_type = excluded.content_type,
                    original_bytes = excluded.original_bytes,
                    width = excluded.width,
                    height = excluded.height,
                    updated_at = excluded.updated_at
                """,
                (
                    user_id,
                    account_id,
                    sha256,
                    filename,
                    content_type,
                    original_bytes,
                    width,
                    height,
                    now,
                    now,
                ),
            )
        return self.get_by_hash(
            sha256, user_id=user_id, account_id=account_id
        ) or {}

    def update_result(
        self,
        sha256: str,
        *,
        media_id: str | None = None,
        material_url: str | None = None,
        article_url: str | None = None,
        processed_bytes: int | None = None,
        last_error: str | None = None,
        user_id: int | None = None,
        account_id: int | None = None,
    ) -> dict[str, Any]:
        updates: list[str] = ["updated_at = ?", "last_error = ?"]
        values: list[Any] = [_now(), last_error]
        optional = {
            "media_id": media_id,
            "material_url": material_url,
            "article_url": article_url,
            "processed_bytes": processed_bytes,
        }
        for column, value in optional.items():
            if value is not None:
                updates.append(f"{column} = ?")
                values.append(value)
        where = ["sha256 = ?"]
        values.append(sha256)
        if user_id is not None:
            where.append("user_id = ?")
            values.append(user_id)
        if account_id is not None:
            where.append("account_id = ?")
            values.append(account_id)
        with self._connect() as connection:
            connection.execute(
                f"UPDATE assets SET {', '.join(updates)} WHERE {' AND '.join(where)}",
                values,
            )
        return self.get_by_hash(
            sha256, user_id=user_id, account_id=account_id
        ) or {}

    def list_assets(
        self,
        limit: int | None = 500,
        *,
        user_id: int | None = None,
        account_id: int | None = None,
    ) -> list[dict[str, Any]]:
        filters = []
        values: list[Any] = []
        if user_id is not None:
            filters.append("user_id = ?")
            values.append(user_id)
        if account_id is not None:
            filters.append("account_id = ?")
            values.append(account_id)
        where = f" WHERE {' AND '.join(filters)}" if filters else ""
        with self._connect() as connection:
            if limit is None:
                rows = connection.execute(
                    f"SELECT * FROM assets{where} ORDER BY updated_at DESC", values
                ).fetchall()
            else:
                safe_limit = min(max(limit, 1), 2000)
                rows = connection.execute(
                    f"SELECT * FROM assets{where} ORDER BY updated_at DESC LIMIT ?",
                    [*values, safe_limit],
                ).fetchall()
        return [dict(row) for row in rows]

    def delete_asset(self, asset_id: int, *, user_id: int | None = None) -> bool:
        query = "DELETE FROM assets WHERE id = ?"
        values: tuple[Any, ...] = (asset_id,)
        if user_id is not None:
            query += " AND user_id = ?"
            values = (asset_id, user_id)
        with self._connect() as connection:
            cursor = connection.execute(query, values)
        return cursor.rowcount > 0

    def create_temporary_asset(
        self,
        *,
        token: str,
        sha256: str,
        filename: str,
        stored_name: str,
        content_type: str,
        original_bytes: int,
        processed_bytes: int,
        width: int,
        height: int,
        expires_at: str,
        user_id: int | None = None,
    ) -> dict[str, Any]:
        created_at = _now()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO temporary_assets (
                    user_id, token, sha256, filename, stored_name, content_type,
                    original_bytes, processed_bytes, width, height,
                    created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    token,
                    sha256,
                    filename,
                    stored_name,
                    content_type,
                    original_bytes,
                    processed_bytes,
                    width,
                    height,
                    created_at,
                    expires_at,
                ),
            )
            row = connection.execute(
                "SELECT * FROM temporary_assets WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        return dict(row) if row else {}

    def get_temporary_asset(self, token: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM temporary_assets WHERE token = ?", (token,)
            ).fetchone()
        return dict(row) if row else None

    def get_temporary_asset_by_id(
        self, asset_id: int, *, user_id: int | None = None
    ) -> dict[str, Any] | None:
        query = "SELECT * FROM temporary_assets WHERE id = ?"
        values: tuple[Any, ...] = (asset_id,)
        if user_id is not None:
            query += " AND user_id = ?"
            values = (asset_id, user_id)
        with self._connect() as connection:
            row = connection.execute(query, values).fetchone()
        return dict(row) if row else None

    def list_temporary_assets(
        self,
        *,
        limit: int | None = 500,
        active_after: str | None = None,
        user_id: int | None = None,
    ) -> list[dict[str, Any]]:
        cutoff = active_after or _now()
        user_filter = " AND user_id = ?" if user_id is not None else ""
        values: list[Any] = [cutoff]
        if user_id is not None:
            values.append(user_id)
        with self._connect() as connection:
            if limit is None:
                rows = connection.execute(
                    """
                    SELECT * FROM temporary_assets
                    WHERE expires_at > ?{user_filter}
                    ORDER BY created_at DESC
                    """.format(user_filter=user_filter),
                    values,
                ).fetchall()
            else:
                safe_limit = min(max(limit, 1), 2000)
                rows = connection.execute(
                    """
                    SELECT * FROM temporary_assets
                    WHERE expires_at > ?{user_filter}
                    ORDER BY created_at DESC
                    LIMIT ?
                    """.format(user_filter=user_filter),
                    [*values, safe_limit],
                ).fetchall()
        return [dict(row) for row in rows]

    def list_expired_temporary_assets(
        self, *, expired_at: str | None = None
    ) -> list[dict[str, Any]]:
        cutoff = expired_at or _now()
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM temporary_assets WHERE expires_at <= ?", (cutoff,)
            ).fetchall()
        return [dict(row) for row in rows]

    def list_all_temporary_assets(
        self, *, user_id: int | None = None
    ) -> list[dict[str, Any]]:
        where = " WHERE user_id = ?" if user_id is not None else ""
        values = (user_id,) if user_id is not None else ()
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM temporary_assets{where} ORDER BY created_at DESC",
                values,
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_temporary_asset(
        self, asset_id: int, *, user_id: int | None = None
    ) -> bool:
        query = "DELETE FROM temporary_assets WHERE id = ?"
        values: tuple[Any, ...] = (asset_id,)
        if user_id is not None:
            query += " AND user_id = ?"
            values = (asset_id, user_id)
        with self._connect() as connection:
            cursor = connection.execute(query, values)
        return cursor.rowcount > 0

    def temporary_storage_bytes(self, *, user_id: int | None = None) -> int:
        query = "SELECT COALESCE(SUM(processed_bytes), 0) AS total FROM temporary_assets"
        values: tuple[Any, ...] = ()
        if user_id is not None:
            query += " WHERE user_id = ?"
            values = (user_id,)
        with self._connect() as connection:
            row = connection.execute(query, values).fetchone()
        return int(row["total"]) if row else 0

    def get_wechat_account(self) -> dict[str, Any] | None:
        admin = self.get_admin_user()
        if admin is None:
            return None
        return self.get_active_official_account(int(admin["id"]))

    def upsert_wechat_account(
        self,
        *,
        display_name: str,
        account_type: str,
        app_id: str,
        app_secret_ciphertext: str,
    ) -> dict[str, Any]:
        admin = self.get_admin_user()
        if admin is None:
            raise RuntimeError("管理员凭据尚未初始化")
        user_id = int(admin["id"])
        current = self.get_active_official_account(user_id)
        if current:
            return self.update_official_account(
                int(current["id"]),
                user_id=user_id,
                display_name=display_name,
                account_type=account_type,
                app_id=app_id,
                app_secret_ciphertext=app_secret_ciphertext,
            ) or {}
        return self.create_official_account(
            user_id=user_id,
            display_name=display_name,
            account_type=account_type,
            app_id=app_id,
            app_secret_ciphertext=app_secret_ciphertext,
        ) or {}

    def list_official_accounts(self, user_id: int) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM official_accounts WHERE user_id = ? "
                "ORDER BY updated_at DESC, id DESC",
                (user_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_official_account(
        self, account_id: int, *, user_id: int | None = None
    ) -> dict[str, Any] | None:
        query = "SELECT * FROM official_accounts WHERE id = ?"
        values: tuple[Any, ...] = (account_id,)
        if user_id is not None:
            query += " AND user_id = ?"
            values = (account_id, user_id)
        with self._connect() as connection:
            row = connection.execute(query, values).fetchone()
        return dict(row) if row else None

    def get_active_official_account(self, user_id: int) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT account.* FROM users
                LEFT JOIN official_accounts AS account
                  ON account.id = users.active_account_id
                 AND account.user_id = users.id
                WHERE users.id = ?
                """,
                (user_id,),
            ).fetchone()
            if row and row["id"] is not None:
                return dict(row)
            row = connection.execute(
                "SELECT * FROM official_accounts WHERE user_id = ? ORDER BY id LIMIT 1",
                (user_id,),
            ).fetchone()
        return dict(row) if row else None

    def create_official_account(
        self,
        *,
        user_id: int,
        display_name: str,
        account_type: str,
        app_id: str,
        app_secret_ciphertext: str,
    ) -> dict[str, Any] | None:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    """
                    INSERT INTO official_accounts (
                        user_id, display_name, account_type, app_id,
                        app_secret_ciphertext, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        display_name,
                        account_type,
                        app_id,
                        app_secret_ciphertext,
                        now,
                        now,
                    ),
                )
                account_id = int(cursor.lastrowid)
                connection.execute(
                    "UPDATE users SET active_account_id = ?, updated_at = ? "
                    "WHERE id = ? AND active_account_id IS NULL",
                    (account_id, now, user_id),
                )
                row = connection.execute(
                    "SELECT * FROM official_accounts WHERE id = ?", (account_id,)
                ).fetchone()
        except sqlite3.IntegrityError:
            return None
        return dict(row) if row else None

    def update_official_account(
        self,
        account_id: int,
        *,
        user_id: int,
        display_name: str,
        account_type: str,
        app_id: str,
        app_secret_ciphertext: str,
    ) -> dict[str, Any] | None:
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    """
                    UPDATE official_accounts
                    SET display_name = ?, account_type = ?, app_id = ?,
                        app_secret_ciphertext = ?, updated_at = ?
                    WHERE id = ? AND user_id = ?
                    """,
                    (
                        display_name,
                        account_type,
                        app_id,
                        app_secret_ciphertext,
                        _now(),
                        account_id,
                        user_id,
                    ),
                )
                if cursor.rowcount != 1:
                    return None
                row = connection.execute(
                    "SELECT * FROM official_accounts WHERE id = ?", (account_id,)
                ).fetchone()
        except sqlite3.IntegrityError:
            return None
        return dict(row) if row else None

    def set_active_official_account(self, user_id: int, account_id: int) -> bool:
        with self._connect() as connection:
            owned = connection.execute(
                "SELECT 1 FROM official_accounts WHERE id = ? AND user_id = ?",
                (account_id, user_id),
            ).fetchone()
            if not owned:
                return False
            connection.execute(
                "UPDATE users SET active_account_id = ?, updated_at = ? WHERE id = ?",
                (account_id, _now(), user_id),
            )
        return True

    def delete_official_account(self, account_id: int, *, user_id: int) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM official_accounts WHERE id = ? AND user_id = ?",
                (account_id, user_id),
            )
            if cursor.rowcount != 1:
                return False
            replacement = connection.execute(
                "SELECT id FROM official_accounts WHERE user_id = ? ORDER BY id LIMIT 1",
                (user_id,),
            ).fetchone()
            connection.execute(
                "UPDATE users SET active_account_id = ?, updated_at = ? WHERE id = ?",
                (int(replacement["id"]) if replacement else None, _now(), user_id),
            )
        return True

    def get_draft_job_by_request_id(
        self,
        request_id: str,
        *,
        user_id: int | None = None,
        account_id: int | None = None,
    ) -> dict[str, Any] | None:
        query = "SELECT * FROM draft_jobs WHERE request_id = ?"
        values: list[Any] = [request_id]
        if user_id is not None:
            query += " AND user_id = ?"
            values.append(user_id)
        if account_id is not None:
            query += " AND account_id = ?"
            values.append(account_id)
        with self._connect() as connection:
            row = connection.execute(query, values).fetchone()
        return dict(row) if row else None

    def get_draft_job(
        self, draft_id: int, *, user_id: int | None = None
    ) -> dict[str, Any] | None:
        query = "SELECT * FROM draft_jobs WHERE id = ?"
        values: tuple[Any, ...] = (draft_id,)
        if user_id is not None:
            query += " AND user_id = ?"
            values = (draft_id, user_id)
        with self._connect() as connection:
            row = connection.execute(query, values).fetchone()
        return dict(row) if row else None

    def get_draft_job_by_media_id(
        self,
        media_id: str,
        *,
        user_id: int | None = None,
        account_id: int | None = None,
    ) -> dict[str, Any] | None:
        filters = ["media_id = ?"]
        values: list[Any] = [media_id]
        if user_id is not None:
            filters.append("user_id = ?")
            values.append(user_id)
        if account_id is not None:
            filters.append("account_id = ?")
            values.append(account_id)
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT * FROM draft_jobs WHERE {' AND '.join(filters)}",
                values,
            ).fetchone()
        return dict(row) if row else None

    def create_draft_job(
        self,
        *,
        request_id: str,
        content_hash: str,
        title: str,
        author: str,
        digest: str,
        content: str,
        content_source_url: str,
        thumb_media_id: str,
        need_open_comment: int,
        only_fans_can_comment: int,
        user_id: int | None = None,
        account_id: int | None = None,
    ) -> dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO draft_jobs (
                    user_id, account_id, request_id, content_hash, title, author, digest, content,
                    content_source_url, thumb_media_id, need_open_comment,
                    only_fans_can_comment, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    user_id,
                    account_id,
                    request_id,
                    content_hash,
                    title,
                    author,
                    digest,
                    content,
                    content_source_url,
                    thumb_media_id,
                    need_open_comment,
                    only_fans_can_comment,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM draft_jobs WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        return dict(row) if row else {}

    def update_draft_job(
        self,
        draft_id: int,
        *,
        status: str,
        media_id: str | None = None,
        last_error: str | None = None,
    ) -> dict[str, Any]:
        updates = ["status = ?", "last_error = ?", "updated_at = ?"]
        values: list[Any] = [status, last_error, _now()]
        if media_id is not None:
            updates.append("media_id = ?")
            values.append(media_id)
        values.append(draft_id)
        with self._connect() as connection:
            connection.execute(
                f"UPDATE draft_jobs SET {', '.join(updates)} WHERE id = ?", values
            )
        return self.get_draft_job(draft_id) or {}

    def update_draft_content(
        self,
        draft_id: int,
        *,
        content_hash: str,
        title: str,
        author: str,
        digest: str,
        content: str,
        content_source_url: str,
        thumb_media_id: str,
        need_open_comment: int,
        only_fans_can_comment: int,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE draft_jobs
                SET content_hash = ?, title = ?, author = ?, digest = ?, content = ?,
                    content_source_url = ?, thumb_media_id = ?, need_open_comment = ?,
                    only_fans_can_comment = ?, status = 'created', last_error = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    content_hash,
                    title,
                    author,
                    digest,
                    content,
                    content_source_url,
                    thumb_media_id,
                    need_open_comment,
                    only_fans_can_comment,
                    _now(),
                    draft_id,
                ),
            )
        return self.get_draft_job(draft_id) or {}

    def delete_draft_job(
        self,
        draft_id: int,
        *,
        user_id: int | None = None,
        account_id: int | None = None,
    ) -> bool:
        filters = ["id = ?"]
        values: list[Any] = [draft_id]
        if user_id is not None:
            filters.append("user_id = ?")
            values.append(user_id)
        if account_id is not None:
            filters.append("account_id = ?")
            values.append(account_id)
        with self._connect() as connection:
            cursor = connection.execute(
                f"DELETE FROM draft_jobs WHERE {' AND '.join(filters)}", values
            )
        return cursor.rowcount == 1

    def purge_deleted_draft_jobs(self) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM draft_jobs WHERE status = 'deleted'"
            )
        return cursor.rowcount

    def list_draft_jobs(
        self,
        limit: int = 200,
        *,
        user_id: int | None = None,
        account_id: int | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        safe_limit = min(max(limit, 1), 1000)
        safe_offset = max(offset, 0)
        filters = []
        values: list[Any] = []
        if user_id is not None:
            filters.append("draft_jobs.user_id = ?")
            values.append(user_id)
        if account_id is not None:
            filters.append("draft_jobs.account_id = ?")
            values.append(account_id)
        where = f" WHERE {' AND '.join(filters)}" if filters else ""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT draft_jobs.*, users.username AS owner_username, "
                "official_accounts.display_name AS account_display_name "
                "FROM draft_jobs "
                "LEFT JOIN users ON users.id = draft_jobs.user_id "
                "LEFT JOIN official_accounts ON official_accounts.id = draft_jobs.account_id"
                f"{where} ORDER BY draft_jobs.updated_at DESC LIMIT ? OFFSET ?",
                [*values, safe_limit, safe_offset],
            ).fetchall()
        return [dict(row) for row in rows]

    def count_draft_jobs(
        self, *, user_id: int | None = None, account_id: int | None = None
    ) -> int:
        filters = []
        values: list[Any] = []
        if user_id is not None:
            filters.append("user_id = ?")
            values.append(user_id)
        if account_id is not None:
            filters.append("account_id = ?")
            values.append(account_id)
        where = f" WHERE {' AND '.join(filters)}" if filters else ""
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT COUNT(*) AS total FROM draft_jobs{where}", values
            ).fetchone()
        return int(row["total"]) if row else 0

    def overview_counts(
        self, *, user_id: int | None = None, account_id: int | None = None
    ) -> dict[str, int]:
        filters = []
        values: list[Any] = []
        if user_id is not None:
            filters.append("user_id = ?")
            values.append(user_id)
        if account_id is not None:
            filters.append("account_id = ?")
            values.append(account_id)
        where = f" WHERE {' AND '.join(filters)}" if filters else ""
        draft_prefix = f"{' AND '.join(filters)} AND " if filters else ""
        with self._connect() as connection:
            assets = connection.execute(
                f"SELECT COUNT(*) AS total FROM assets{where}", values
            ).fetchone()
            temporary = connection.execute(
                "SELECT COUNT(*) AS total FROM temporary_assets "
                + ("WHERE user_id = ? AND " if user_id is not None else "WHERE ")
                + "expires_at > ?",
                ([user_id] if user_id is not None else []) + [_now()],
            ).fetchone()
            drafts = connection.execute(
                f"SELECT COUNT(*) AS total FROM draft_jobs WHERE {draft_prefix}status = 'created'",
                values,
            ).fetchone()
            failures = connection.execute(
                f"SELECT COUNT(*) AS total FROM draft_jobs WHERE {draft_prefix}status = 'failed'",
                values,
            ).fetchone()
            unknown = connection.execute(
                f"SELECT COUNT(*) AS total FROM draft_jobs WHERE {draft_prefix}status = 'unknown'",
                values,
            ).fetchone()
        return {
            "assets": int(assets["total"]) if assets else 0,
            "temporary_assets": int(temporary["total"]) if temporary else 0,
            "drafts": int(drafts["total"]) if drafts else 0,
            "failed_drafts": int(failures["total"]) if failures else 0,
            "unknown_drafts": int(unknown["total"]) if unknown else 0,
        }
