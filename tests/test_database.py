import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.database import CURRENT_SCHEMA_VERSION, AssetStore


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
            "SELECT version FROM schema_migrations"
        ).fetchone()[0] == CURRENT_SCHEMA_VERSION
