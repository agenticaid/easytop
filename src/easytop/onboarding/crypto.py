"""Encrypt a parent's Webtop session cookies for storage (Spec 03: the store only ever sees ciphertext).

Fernet (AES-128-CBC + HMAC, authenticated): a wrong key or any change to the data fails loudly. The key
comes from `SESSION_ENCRYPTION_KEY` (a `.env` value in development, Secret Manager in production), never
from the database. Error messages never carry cookie values or key material.
"""

from __future__ import annotations

import json
import os

from cryptography.fernet import Fernet, InvalidToken

from easytop.errors import SessionDecryptError

ENV_VAR = "SESSION_ENCRYPTION_KEY"


def new_key() -> bytes:
    """A fresh key: `python -c "from easytop.onboarding.crypto import new_key; print(new_key().decode())"`."""
    return Fernet.generate_key()


class SessionCipher:
    def __init__(self, key: bytes | str):
        try:
            self._fernet = Fernet(key)
        except (ValueError, TypeError):
            raise ValueError(f"{ENV_VAR} is not a valid Fernet key") from None

    @classmethod
    def from_env(cls) -> SessionCipher:
        key = os.environ.get(ENV_VAR)
        if not key:
            raise ValueError(f"set {ENV_VAR} (see easytop.onboarding.crypto.new_key)")
        return cls(key)

    def encrypt(self, cookies: dict[str, str]) -> bytes:
        return self._fernet.encrypt(json.dumps(cookies).encode())

    def decrypt(self, blob: bytes) -> dict[str, str]:
        try:
            data = json.loads(self._fernet.decrypt(blob))
        except (InvalidToken, ValueError, TypeError):
            raise SessionDecryptError("stored session cannot be decrypted") from None
        if not isinstance(data, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in data.items()
        ):
            raise SessionDecryptError("stored session has an unexpected shape")
        return data
