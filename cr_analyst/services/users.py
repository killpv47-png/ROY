"""User registration, atomic owner initialisation, roles & permissions (enforced in the backend)."""
from __future__ import annotations

import hashlib
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from ..db.models import OwnerConfig, PlayerProfile, RolePermission, TelegramIdentity, User

log = logging.getLogger(__name__)

ROLES = ("owner", "admin", "user", "banned")
ROLE_RANK = {"banned": 0, "user": 1, "admin": 2, "owner": 3}

# Default permission matrix — stored in DB (role_permissions) so the owner can extend it.
DEFAULT_PERMISSIONS: dict[str, set[str]] = {
    "user": {"account.use", "scan.run", "decks.generate", "battles.view", "meta.view", "agent.ask",
             "credentials.own", "settings.own"},
    "admin": {"admin.users.view", "admin.tasks.view", "admin.status.view", "admin.meta.manage",
              "admin.errors.view"},
    "owner": {"owner.all", "admin.users.manage", "admin.proxy.manage", "admin.api.manage", "admin.llm.manage",
              "admin.runner.control", "admin.lang.manage", "agent.terminal", "agent.files.write",
              "admin.skills.approve"},
}


def expand_permissions(role: str, extra: dict[str, set[str]] | None = None) -> set[str]:
    table = extra or DEFAULT_PERMISSIONS
    perms: set[str] = set()
    for r, rank in ROLE_RANK.items():
        if rank and rank <= ROLE_RANK.get(role, 0):
            perms |= table.get(r, set())
    return perms


class PermissionDenied(Exception):
    def __init__(self, permission: str):
        super().__init__(permission)
        self.permission = permission


@dataclass
class RegistrationResult:
    user: User
    created: bool
    became_owner: bool


def _hash_code(code: str) -> str:
    return hashlib.sha256(("cr-analyst-owner-recovery:" + code).encode()).hexdigest()


class UserService:
    def __init__(self, db, admin_id: int | None = None, default_language: str = "fa"):
        self.db = db
        self.admin_id = admin_id
        self.default_language = default_language

    async def seed_permissions(self) -> None:
        async with self.db.session() as s:
            existing = {(r.role, r.permission) for r in (await s.execute(select(RolePermission))).scalars()}
            for role, perms in DEFAULT_PERMISSIONS.items():
                for p in perms:
                    if (role, p) not in existing:
                        s.add(RolePermission(role=role, permission=p))

    async def permission_table(self) -> dict[str, set[str]]:
        async with self.db.session() as s:
            rows = (await s.execute(select(RolePermission))).scalars().all()
        table: dict[str, set[str]] = {}
        for r in rows:
            table.setdefault(r.role, set()).add(r.permission)
        return table or DEFAULT_PERMISSIONS

    # ------------------------------------------------------------------ owner
    async def get_owner_id(self) -> int | None:
        async with self.db.session() as s:
            row = await s.get(OwnerConfig, 1)
            return row.owner_telegram_id if row else None

    async def _claim_owner(self, telegram_id: int, source: str) -> bool:
        """Atomic: the singleton PK (id=1) guarantees only one INSERT can ever succeed."""
        try:
            async with self.db.session() as s:
                s.add(OwnerConfig(id=1, owner_telegram_id=telegram_id, source=source))
            return True
        except IntegrityError:
            return False

    async def init_owner_from_env(self) -> int | None:
        """At startup: if ADMIN_ID is set and no owner exists, ADMIN_ID becomes owner. Never replaces an owner."""
        owner = await self.get_owner_id()
        if owner is None and self.admin_id:
            if await self._claim_owner(self.admin_id, "env"):
                owner = self.admin_id
                async with self.db.session() as s:
                    u = (await s.execute(select(User).where(User.telegram_id == owner))).scalar_one_or_none()
                    if u:
                        u.role = "owner"
            else:
                owner = await self.get_owner_id()
        return owner

    async def create_recovery_code(self) -> str:
        """Generate a one-time owner recovery code (shown once to the owner, stored hashed)."""
        code = secrets.token_urlsafe(18)
        async with self.db.session() as s:
            row = await s.get(OwnerConfig, 1)
            if not row:
                raise ValueError("no owner")
            row.recovery_code_hash = _hash_code(code)
        return code

    async def recover_owner(self, telegram_id: int, code: str) -> bool:
        """Transfer ownership with the one-time recovery code (or via CLI `python -m cr_analyst owner-transfer`)."""
        async with self.db.session() as s:
            row = await s.get(OwnerConfig, 1)
            if not row or not row.recovery_code_hash:
                return False
            if not secrets.compare_digest(row.recovery_code_hash, _hash_code(code.strip())):
                return False
            await self._set_owner(s, row, telegram_id, "recovery")
            row.recovery_code_hash = None
        return True

    async def transfer_owner_cli(self, telegram_id: int) -> None:
        """Out-of-band recovery for operators with database / secrets access."""
        async with self.db.session() as s:
            row = await s.get(OwnerConfig, 1)
            if row is None:
                s.add(OwnerConfig(id=1, owner_telegram_id=telegram_id, source="recovery"))
                u = (await s.execute(select(User).where(User.telegram_id == telegram_id))).scalar_one_or_none()
                if u:
                    u.role = "owner"
                return
            await self._set_owner(s, row, telegram_id, "recovery")

    @staticmethod
    async def _set_owner(s, row: OwnerConfig, telegram_id: int, source: str) -> None:
        old = (await s.execute(select(User).where(User.telegram_id == row.owner_telegram_id))).scalar_one_or_none()
        if old:
            old.role = "admin"
        row.owner_telegram_id = telegram_id
        row.source = source
        new = (await s.execute(select(User).where(User.telegram_id == telegram_id))).scalar_one_or_none()
        if new:
            new.role = "owner"

    # ------------------------------------------------------------------ registration
    async def register(self, telegram_id: int, username: str | None, first_name: str | None,
                       language_code: str | None, chat_id: int, chat_type: str,
                       supported_languages: tuple[str, ...] = ()) -> RegistrationResult:
        lang = (language_code or "").split("-")[0].lower()
        if lang not in supported_languages:
            lang = self.default_language
        created = False
        async with self.db.session() as s:
            u = (await s.execute(select(User).where(User.telegram_id == telegram_id))).scalar_one_or_none()
            if u is None:
                u = User(telegram_id=telegram_id, username=username, first_name=first_name, language=lang,
                         role="user", settings={"notify": "normal", "deck_style": None})
                s.add(u)
                try:
                    await s.flush()
                    created = True
                except IntegrityError:  # concurrent /start from the same user
                    await s.rollback()
                    u = (await s.execute(select(User).where(User.telegram_id == telegram_id))).scalar_one()
            else:
                u.username, u.first_name = username, first_name
                u.last_seen_at = datetime.now(timezone.utc)
            ident = (await s.execute(select(TelegramIdentity).where(
                TelegramIdentity.user_id == u.id, TelegramIdentity.chat_id == chat_id))).scalar_one_or_none()
            if ident is None:
                s.add(TelegramIdentity(user_id=u.id, chat_id=chat_id, chat_type=chat_type))
        became_owner = False
        owner = await self.get_owner_id()
        if owner is None:
            if self.admin_id:
                await self.init_owner_from_env()
            elif chat_type == "private":
                became_owner = await self._claim_owner(telegram_id, "first_start")
            owner = await self.get_owner_id()
        if owner == telegram_id:
            async with self.db.session() as s:
                u2 = await s.get(User, u.id)
                if u2.role != "owner":
                    u2.role = "owner"
                u = u2
        return RegistrationResult(user=u, created=created, became_owner=became_owner)

    # ------------------------------------------------------------------ queries & management
    async def get(self, telegram_id: int) -> User | None:
        async with self.db.session() as s:
            return (await s.execute(select(User).where(User.telegram_id == telegram_id))).scalar_one_or_none()

    async def get_by_id(self, user_id: int) -> User | None:
        async with self.db.session() as s:
            return await s.get(User, user_id)

    async def require(self, telegram_id: int, permission: str) -> User:
        u = await self.get(telegram_id)
        if u is None or u.role == "banned" or not u.is_active:
            raise PermissionDenied(permission)
        if permission not in expand_permissions(u.role, await self.permission_table()):
            raise PermissionDenied(permission)
        return u

    async def has(self, user: User, permission: str) -> bool:
        if user.role == "banned":
            return False
        return permission in expand_permissions(user.role, await self.permission_table())

    async def set_language(self, telegram_id: int, lang: str) -> None:
        async with self.db.session() as s:
            u = (await s.execute(select(User).where(User.telegram_id == telegram_id))).scalar_one()
            u.language = lang

    async def update_settings(self, telegram_id: int, **kv) -> dict:
        async with self.db.session() as s:
            u = (await s.execute(select(User).where(User.telegram_id == telegram_id))).scalar_one()
            st = dict(u.settings or {})
            st.update(kv)
            u.settings = st
            return st

    async def set_role(self, actor_tid: int, target_tid: int, role: str) -> User:
        if role not in ("admin", "user", "banned"):
            raise ValueError("role")
        actor = await self.require(actor_tid, "admin.users.manage")
        async with self.db.session() as s:
            t = (await s.execute(select(User).where(User.telegram_id == target_tid))).scalar_one_or_none()
            if not t:
                raise LookupError("user")
            if t.role == "owner" or t.telegram_id == actor.telegram_id:
                raise PermissionDenied("cannot_change_owner_or_self")
            t.role = role
            return t

    async def list_users(self, offset: int = 0, limit: int = 10) -> tuple[list[User], int]:
        async with self.db.session() as s:
            total = (await s.execute(select(func.count(User.id)))).scalar() or 0
            rows = (await s.execute(select(User).order_by(User.registered_at.desc()).offset(offset).limit(limit))
                    ).scalars().all()
        return list(rows), total

    async def counts(self, active_days: int = 7) -> dict:
        since = datetime.now(timezone.utc) - timedelta(days=active_days)
        async with self.db.session() as s:
            total = (await s.execute(select(func.count(User.id)))).scalar() or 0
            active = (await s.execute(select(func.count(User.id)).where(User.last_seen_at >= since))).scalar() or 0
            admins = (await s.execute(select(func.count(User.id)).where(User.role.in_(("admin", "owner"))))
                      ).scalar() or 0
            banned = (await s.execute(select(func.count(User.id)).where(User.role == "banned"))).scalar() or 0
            players = (await s.execute(select(func.count(PlayerProfile.id)))).scalar() or 0
        return {"total": total, "active": active, "inactive": total - active, "admins": admins, "banned": banned,
                "linked_players": players}

    async def primary_player(self, user_id: int) -> PlayerProfile | None:
        async with self.db.session() as s:
            return (await s.execute(select(PlayerProfile).where(PlayerProfile.user_id == user_id)
                                    .order_by(PlayerProfile.is_primary.desc(), PlayerProfile.id.desc()).limit(1))
                    ).scalar_one_or_none()

    async def touch(self, telegram_id: int) -> None:
        async with self.db.session() as s:
            u = (await s.execute(select(User).where(User.telegram_id == telegram_id))).scalar_one_or_none()
            if u:
                u.last_seen_at = datetime.now(timezone.utc)
