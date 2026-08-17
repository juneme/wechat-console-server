from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

CURRENT_SCHEMA_VERSION = 1


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
        self, *, token_hash: str, username: str, expires_at: str
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM admin_sessions WHERE expires_at <= ?", (_now(),)
            )
            connection.execute(
                """
                INSERT INTO admin_sessions (
                    token_hash, username, created_at, expires_at
                ) VALUES (?, ?, ?, ?)
                """,
                (token_hash, username, _now(), expires_at),
            )

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

    def get_by_hash(self, sha256: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM assets WHERE sha256 = ?", (sha256,)
            ).fetchone()
        return dict(row) if row else None

    def get_asset(self, asset_id: int) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM assets WHERE id = ?", (asset_id,)
            ).fetchone()
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
    ) -> dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO assets (
                    sha256, filename, content_type, original_bytes,
                    width, height, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(sha256) DO UPDATE SET
                    filename = excluded.filename,
                    content_type = excluded.content_type,
                    original_bytes = excluded.original_bytes,
                    width = excluded.width,
                    height = excluded.height,
                    updated_at = excluded.updated_at
                """,
                (
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
        return self.get_by_hash(sha256) or {}

    def update_result(
        self,
        sha256: str,
        *,
        media_id: str | None = None,
        material_url: str | None = None,
        article_url: str | None = None,
        processed_bytes: int | None = None,
        last_error: str | None = None,
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
        values.append(sha256)
        with self._connect() as connection:
            connection.execute(
                f"UPDATE assets SET {', '.join(updates)} WHERE sha256 = ?", values
            )
        return self.get_by_hash(sha256) or {}

    def list_assets(self, limit: int | None = 500) -> list[dict[str, Any]]:
        with self._connect() as connection:
            if limit is None:
                rows = connection.execute(
                    "SELECT * FROM assets ORDER BY updated_at DESC"
                ).fetchall()
            else:
                safe_limit = min(max(limit, 1), 2000)
                rows = connection.execute(
                    "SELECT * FROM assets ORDER BY updated_at DESC LIMIT ?",
                    (safe_limit,),
                ).fetchall()
        return [dict(row) for row in rows]

    def delete_asset(self, asset_id: int) -> bool:
        with self._connect() as connection:
            cursor = connection.execute("DELETE FROM assets WHERE id = ?", (asset_id,))
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
    ) -> dict[str, Any]:
        created_at = _now()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO temporary_assets (
                    token, sha256, filename, stored_name, content_type,
                    original_bytes, processed_bytes, width, height,
                    created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
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

    def get_temporary_asset_by_id(self, asset_id: int) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM temporary_assets WHERE id = ?", (asset_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_temporary_assets(
        self, *, limit: int | None = 500, active_after: str | None = None
    ) -> list[dict[str, Any]]:
        cutoff = active_after or _now()
        with self._connect() as connection:
            if limit is None:
                rows = connection.execute(
                    """
                    SELECT * FROM temporary_assets
                    WHERE expires_at > ?
                    ORDER BY created_at DESC
                    """,
                    (cutoff,),
                ).fetchall()
            else:
                safe_limit = min(max(limit, 1), 2000)
                rows = connection.execute(
                    """
                    SELECT * FROM temporary_assets
                    WHERE expires_at > ?
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (cutoff, safe_limit),
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

    def list_all_temporary_assets(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM temporary_assets ORDER BY created_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_temporary_asset(self, asset_id: int) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM temporary_assets WHERE id = ?", (asset_id,)
            )
        return cursor.rowcount > 0

    def temporary_storage_bytes(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(SUM(processed_bytes), 0) AS total FROM temporary_assets"
            ).fetchone()
        return int(row["total"]) if row else 0

    def get_wechat_account(self) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM wechat_account WHERE id = 1"
            ).fetchone()
        return dict(row) if row else None

    def upsert_wechat_account(
        self,
        *,
        display_name: str,
        account_type: str,
        app_id: str,
        app_secret_ciphertext: str,
    ) -> dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO wechat_account (
                    id, display_name, account_type, app_id,
                    app_secret_ciphertext, created_at, updated_at
                ) VALUES (1, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    display_name = excluded.display_name,
                    account_type = excluded.account_type,
                    app_id = excluded.app_id,
                    app_secret_ciphertext = excluded.app_secret_ciphertext,
                    updated_at = excluded.updated_at
                """,
                (
                    display_name,
                    account_type,
                    app_id,
                    app_secret_ciphertext,
                    now,
                    now,
                ),
            )
        return self.get_wechat_account() or {}

    def get_draft_job_by_request_id(self, request_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM draft_jobs WHERE request_id = ?", (request_id,)
            ).fetchone()
        return dict(row) if row else None

    def get_draft_job(self, draft_id: int) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM draft_jobs WHERE id = ?", (draft_id,)
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
    ) -> dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO draft_jobs (
                    request_id, content_hash, title, author, digest, content,
                    content_source_url, thumb_media_id, need_open_comment,
                    only_fans_can_comment, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
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

    def list_draft_jobs(self, limit: int = 200) -> list[dict[str, Any]]:
        safe_limit = min(max(limit, 1), 1000)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM draft_jobs ORDER BY updated_at DESC LIMIT ?",
                (safe_limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def overview_counts(self) -> dict[str, int]:
        with self._connect() as connection:
            assets = connection.execute("SELECT COUNT(*) AS total FROM assets").fetchone()
            temporary = connection.execute(
                "SELECT COUNT(*) AS total FROM temporary_assets WHERE expires_at > ?",
                (_now(),),
            ).fetchone()
            drafts = connection.execute(
                "SELECT COUNT(*) AS total FROM draft_jobs WHERE status = 'created'"
            ).fetchone()
            failures = connection.execute(
                "SELECT COUNT(*) AS total FROM draft_jobs WHERE status = 'failed'"
            ).fetchone()
            unknown = connection.execute(
                "SELECT COUNT(*) AS total FROM draft_jobs WHERE status = 'unknown'"
            ).fetchone()
        return {
            "assets": int(assets["total"]) if assets else 0,
            "temporary_assets": int(temporary["total"]) if temporary else 0,
            "drafts": int(drafts["total"]) if drafts else 0,
            "failed_drafts": int(failures["total"]) if failures else 0,
            "unknown_drafts": int(unknown["total"]) if unknown else 0,
        }
