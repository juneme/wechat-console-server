from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc


@dataclass(frozen=True)
class Settings:
    wechat_app_id: str
    wechat_app_secret: str
    admin_username: str
    admin_password: str
    database_path: Path
    max_source_bytes: int
    article_max_bytes: int
    permanent_max_bytes: int
    article_max_dimension: int
    public_base_url: str
    ai_api_key: str
    temp_api_key: str
    temp_storage_path: Path
    temp_max_bytes: int
    temp_storage_max_bytes: int
    temp_user_storage_max_bytes: int
    temp_retention_days: int
    temp_cleanup_interval_seconds: int
    max_users: int
    max_accounts_per_user: int
    credentials_encryption_key: str
    publish_api_key: str

    @property
    def wechat_configured(self) -> bool:
        return bool(self.wechat_app_id and self.wechat_app_secret)

    @property
    def temp_api_configured(self) -> bool:
        return bool(self.temp_api_key)

    @property
    def ai_api_configured(self) -> bool:
        return bool(self.ai_api_key)

    @property
    def publish_api_configured(self) -> bool:
        return bool(self.publish_api_key)

    def validate_runtime(self) -> None:
        if self.admin_password and len(self.admin_password) < 12:
            raise RuntimeError("ADMIN_PASSWORD must contain at least 12 characters")
        if self.admin_password.startswith("replace-with-"):
            raise RuntimeError("ADMIN_PASSWORD must not use the example placeholder")
        if self.temp_api_key and len(self.temp_api_key) < 24:
            raise RuntimeError("TEMP_API_KEY must contain at least 24 characters")
        if self.temp_api_key.startswith("replace-with-"):
            raise RuntimeError("TEMP_API_KEY must not use the example placeholder")
        if self.ai_api_key and len(self.ai_api_key) < 24:
            raise RuntimeError("AI_API_KEY must contain at least 24 characters")
        if self.ai_api_key.startswith("replace-with-"):
            raise RuntimeError("AI_API_KEY must not use the example placeholder")
        if self.publish_api_key and len(self.publish_api_key) < 24:
            raise RuntimeError("PUBLISH_API_KEY must contain at least 24 characters")
        if self.publish_api_key.startswith("replace-with-"):
            raise RuntimeError("PUBLISH_API_KEY must not use the example placeholder")
        if self.credentials_encryption_key and len(self.credentials_encryption_key) < 24:
            raise RuntimeError(
                "CREDENTIALS_ENCRYPTION_KEY must contain at least 24 characters"
            )
        if self.credentials_encryption_key.startswith("replace-with-"):
            raise RuntimeError(
                "CREDENTIALS_ENCRYPTION_KEY must not use the example placeholder"
            )
        if self.public_base_url and not self.public_base_url.startswith(
            ("http://", "https://")
        ):
            raise RuntimeError("PUBLIC_BASE_URL must start with http:// or https://")
        positive_values = {
            "MAX_SOURCE_BYTES": self.max_source_bytes,
            "ARTICLE_MAX_BYTES": self.article_max_bytes,
            "PERMANENT_MAX_BYTES": self.permanent_max_bytes,
            "ARTICLE_MAX_DIMENSION": self.article_max_dimension,
            "TEMP_MAX_BYTES": self.temp_max_bytes,
            "TEMP_STORAGE_MAX_BYTES": self.temp_storage_max_bytes,
            "TEMP_USER_STORAGE_MAX_BYTES": self.temp_user_storage_max_bytes,
            "MAX_USERS": self.max_users,
            "MAX_ACCOUNTS_PER_USER": self.max_accounts_per_user,
        }
        invalid = [name for name, value in positive_values.items() if value < 1]
        if invalid:
            raise RuntimeError(f"{', '.join(invalid)} must be greater than zero")
        if self.temp_retention_days < 1:
            raise RuntimeError("TEMP_RETENTION_DAYS must be at least one day")
        if self.temp_cleanup_interval_seconds < 60:
            raise RuntimeError("TEMP_CLEANUP_INTERVAL_SECONDS must be at least 60")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings(
        wechat_app_id=os.getenv("WECHAT_APP_ID", "").strip(),
        wechat_app_secret=os.getenv("WECHAT_APP_SECRET", "").strip(),
        admin_username=os.getenv("ADMIN_USERNAME", "").strip(),
        admin_password=os.getenv("ADMIN_PASSWORD", ""),
        database_path=Path(os.getenv("DATABASE_PATH", "/data/uploader.sqlite3")),
        max_source_bytes=_int_env("MAX_SOURCE_BYTES", 30_000_000),
        article_max_bytes=_int_env("ARTICLE_MAX_BYTES", 1_000_000),
        permanent_max_bytes=_int_env("PERMANENT_MAX_BYTES", 10_000_000),
        article_max_dimension=_int_env("ARTICLE_MAX_DIMENSION", 2000),
        public_base_url=os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/"),
        ai_api_key=os.getenv("AI_API_KEY", "").strip(),
        temp_api_key=os.getenv("TEMP_API_KEY", "").strip(),
        temp_storage_path=Path(os.getenv("TEMP_STORAGE_PATH", "/data/temp-images")),
        temp_max_bytes=_int_env("TEMP_MAX_BYTES", 1_000_000),
        temp_storage_max_bytes=_int_env("TEMP_STORAGE_MAX_BYTES", 5_000_000_000),
        temp_user_storage_max_bytes=_int_env(
            "TEMP_USER_STORAGE_MAX_BYTES", 500_000_000
        ),
        temp_retention_days=_int_env("TEMP_RETENTION_DAYS", 30),
        temp_cleanup_interval_seconds=_int_env("TEMP_CLEANUP_INTERVAL_SECONDS", 3600),
        max_users=_int_env("MAX_USERS", 100),
        max_accounts_per_user=_int_env("MAX_ACCOUNTS_PER_USER", 20),
        credentials_encryption_key=os.getenv(
            "CREDENTIALS_ENCRYPTION_KEY", ""
        ).strip(),
        publish_api_key=os.getenv("PUBLISH_API_KEY", "").strip(),
    )
