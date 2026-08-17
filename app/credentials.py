from __future__ import annotations

import base64
import hashlib
import os
from contextlib import suppress
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


class CredentialError(RuntimeError):
    pass


class CredentialCipher:
    def __init__(self, key: bytes, *, source: str):
        self._fernet = Fernet(key)
        self.source = source

    @classmethod
    def create(cls, *, secret: str, key_path: Path) -> CredentialCipher:
        if secret:
            digest = hashlib.sha256(secret.encode("utf-8")).digest()
            return cls(base64.urlsafe_b64encode(digest), source="environment")

        key_path.parent.mkdir(parents=True, exist_ok=True)
        if not key_path.exists():
            try:
                with key_path.open("xb") as key_file:
                    key_file.write(Fernet.generate_key())
                with suppress(OSError):
                    os.chmod(key_path, 0o600)
            except FileExistsError:
                pass
        key = key_path.read_bytes().strip()
        try:
            return cls(key, source="local-key-file")
        except (TypeError, ValueError) as exc:
            raise CredentialError("公众号凭据加密密钥无效") from exc

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, value: str) -> str:
        try:
            return self._fernet.decrypt(value.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeError, ValueError) as exc:
            raise CredentialError("无法解密公众号凭据，请检查加密主密钥") from exc
