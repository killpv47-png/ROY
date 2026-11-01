"""Encrypted credential store (Supercell developer keys, LLM keys).

* Plaintext never touches logs, messages, or the repository; only a short SHA-256 fingerprint is shown.
* Per-user keys override the optional shared CLASH_API_TOKEN.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from sqlalchemy import select

from ..db.models import EncryptedCredential
from ..security.crypto import CryptoError, SecretBox, fingerprint
from ..security.redact import register_secrets

JWT_RE = re.compile(r"^eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")


def looks_like_clash_key(value: str) -> bool:
    """Supercell developer keys are JWTs (three base64url segments starting with 'eyJ')."""
    return bool(JWT_RE.match(value.strip())) and len(value.strip()) > 100


class CredentialService:
    def __init__(self, db, box: SecretBox | None, shared_clash_token: str = ""):
        self.db = db
        self.box = box
        self.shared = shared_clash_token
        if shared_clash_token:
            register_secrets([shared_clash_token])

    def _require_box(self) -> SecretBox:
        if not self.box:
            raise CryptoError("APP_ENCRYPTION_KEY is not configured; cannot store credentials")
        return self.box

    async def set(self, user_id: int | None, kind: str, value: str, label: str = "default") -> str:
        box = self._require_box()
        value = value.strip()
        fp = fingerprint(value)
        register_secrets([value])
        async with self.db.session() as s:
            row = (await s.execute(select(EncryptedCredential).where(
                EncryptedCredential.user_id.is_(None) if user_id is None else EncryptedCredential.user_id == user_id,
                EncryptedCredential.kind == kind, EncryptedCredential.label == label))).scalar_one_or_none()
            if row:
                row.ciphertext, row.fingerprint, row.last_status = box.encrypt(value), fp, None
                row.created_at = datetime.now(timezone.utc)
            else:
                s.add(EncryptedCredential(user_id=user_id, kind=kind, label=label, ciphertext=box.encrypt(value),
                                          fingerprint=fp))
        return fp

    async def get(self, user_id: int | None, kind: str, label: str = "default") -> str | None:
        if not self.box:
            return None
        async with self.db.session() as s:
            row = (await s.execute(select(EncryptedCredential).where(
                EncryptedCredential.user_id.is_(None) if user_id is None else EncryptedCredential.user_id == user_id,
                EncryptedCredential.kind == kind, EncryptedCredential.label == label))).scalar_one_or_none()
            if not row:
                return None
            row.last_used_at = datetime.now(timezone.utc)
            value = self.box.decrypt(row.ciphertext)
        register_secrets([value])
        return value

    async def delete(self, user_id: int | None, kind: str, label: str = "default") -> bool:
        async with self.db.session() as s:
            row = (await s.execute(select(EncryptedCredential).where(
                EncryptedCredential.user_id.is_(None) if user_id is None else EncryptedCredential.user_id == user_id,
                EncryptedCredential.kind == kind, EncryptedCredential.label == label))).scalar_one_or_none()
            if not row:
                return False
            await s.delete(row)
            return True

    async def info(self, user_id: int | None, kind: str, label: str = "default") -> dict | None:
        async with self.db.session() as s:
            row = (await s.execute(select(EncryptedCredential).where(
                EncryptedCredential.user_id.is_(None) if user_id is None else EncryptedCredential.user_id == user_id,
                EncryptedCredential.kind == kind, EncryptedCredential.label == label))).scalar_one_or_none()
        if not row:
            return None
        return {"fingerprint": row.fingerprint, "created_at": row.created_at.isoformat(),
                "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
                "last_status": row.last_status}

    async def mark_status(self, user_id: int | None, kind: str, status: str, label: str = "default") -> None:
        async with self.db.session() as s:
            row = (await s.execute(select(EncryptedCredential).where(
                EncryptedCredential.user_id.is_(None) if user_id is None else EncryptedCredential.user_id == user_id,
                EncryptedCredential.kind == kind, EncryptedCredential.label == label))).scalar_one_or_none()
            if row:
                row.last_status = status[:32]

    async def clash_token_for(self, user_id: int | None) -> tuple[str | None, str]:
        """Resolution order: user key → owner-set global key (DB) → CLASH_API_TOKEN env."""
        if user_id is not None:
            v = await self.get(user_id, "clash_api")
            if v:
                return v, "user"
        v = await self.get(None, "clash_api")
        if v:
            return v, "global"
        if self.shared:
            return self.shared, "env"
        return None, "none"
