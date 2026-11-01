"""Authenticated encryption for credentials stored in the database (Fernet / AES-128-CBC + HMAC-SHA256).

APP_ENCRYPTION_KEY may be a Fernet key (urlsafe base64, 32 bytes) or any passphrase (>= 16 chars),
which is stretched with PBKDF2-HMAC-SHA256 (fixed app salt, 390k iterations).
"""
from __future__ import annotations

import base64
import hashlib
import hmac

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

_SALT = b"cr-analyst/credential-store/v1"


class CryptoError(RuntimeError):
    pass


def _derive(key: str) -> bytes:
    raw = key.strip()
    try:
        decoded = base64.urlsafe_b64decode(raw.encode())
        if len(decoded) == 32:
            return raw.encode()
    except Exception:
        pass
    if len(raw) < 16:
        raise CryptoError("APP_ENCRYPTION_KEY must be a Fernet key or a passphrase of at least 16 characters")
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=_SALT, iterations=390_000)
    return base64.urlsafe_b64encode(kdf.derive(raw.encode()))


class SecretBox:
    """Encrypt/decrypt small secrets. Supports key rotation via comma-separated keys (newest first)."""

    def __init__(self, key: str):
        if not key:
            raise CryptoError("APP_ENCRYPTION_KEY is not configured")
        keys = [k for k in key.split(",") if k.strip()]
        self._f = MultiFernet([Fernet(_derive(k)) for k in keys])

    def encrypt(self, plaintext: str) -> str:
        return self._f.encrypt(plaintext.encode()).decode()

    def decrypt(self, token: str) -> str:
        try:
            return self._f.decrypt(token.encode()).decode()
        except InvalidToken as e:
            raise CryptoError("Unable to decrypt credential (wrong key or corrupted record)") from e

    def encrypt_bytes(self, data: bytes) -> bytes:
        return self._f.encrypt(data)

    def decrypt_bytes(self, data: bytes) -> bytes:
        try:
            return self._f.decrypt(data)
        except InvalidToken as e:
            raise CryptoError("Unable to decrypt data") from e


def fingerprint(secret: str) -> str:
    """Non-reversible short fingerprint to identify a credential without revealing it."""
    return hashlib.sha256(secret.encode()).hexdigest()[:12]


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def generate_key() -> str:
    return Fernet.generate_key().decode()
