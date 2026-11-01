"""SQLAlchemy 2.x ORM models. Works on PostgreSQL (production) and SQLite (dev/tests)."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON, BigInteger, Boolean, DateTime, Float, ForeignKey, Index, Integer, LargeBinary, String, Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    type_annotation_map = {dict: JSON, list: JSON}


TS = DateTime(timezone=True)


# ----------------------------------------------------------------------------- users & roles
class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str | None] = mapped_column(String(128))
    language: Mapped[str] = mapped_column(String(8), default="fa")
    role: Mapped[str] = mapped_column(String(16), default="user", index=True)  # owner|admin|user|banned
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    registered_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(TS, default=utcnow, index=True)
    settings: Mapped[dict] = mapped_column(JSON, default=dict)  # notification verbosity, trophy override...

    players: Mapped[list["PlayerProfile"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class TelegramIdentity(Base):
    """Every chat a user has interacted from (private/group) — used to keep credential entry private-only."""
    __tablename__ = "telegram_identities"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    chat_type: Mapped[str] = mapped_column(String(16))
    first_seen_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    __table_args__ = (UniqueConstraint("user_id", "chat_id"),)


class RolePermission(Base):
    __tablename__ = "role_permissions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    role: Mapped[str] = mapped_column(String(16), index=True)
    permission: Mapped[str] = mapped_column(String(64))
    __table_args__ = (UniqueConstraint("role", "permission"),)


class OwnerConfig(Base):
    """Singleton row (id=1). Inserted atomically — the PK makes double-ownership impossible."""
    __tablename__ = "owner_config"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_telegram_id: Mapped[int] = mapped_column(BigInteger)
    source: Mapped[str] = mapped_column(String(16))  # env|first_start|recovery
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    recovery_code_hash: Mapped[str | None] = mapped_column(String(128))


class AppConfig(Base):
    """Owner-managed runtime configuration (model settings, meta schedule, toggles)."""
    __tablename__ = "app_config"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(TS, default=utcnow, onupdate=utcnow)


# ----------------------------------------------------------------------------- players & scans
class PlayerProfile(Base):
    __tablename__ = "player_profiles"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    player_tag: Mapped[str] = mapped_column(String(16), index=True)
    name: Mapped[str | None] = mapped_column(String(64))
    is_primary: Mapped[bool] = mapped_column(Boolean, default=True)
    track_battles: Mapped[bool] = mapped_column(Boolean, default=True)
    last_scan_at: Mapped[datetime | None] = mapped_column(TS)
    last_battle_poll_at: Mapped[datetime | None] = mapped_column(TS)
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    user: Mapped[User] = relationship(back_populates="players")
    __table_args__ = (UniqueConstraint("user_id", "player_tag"),)


class EncryptedCredential(Base):
    __tablename__ = "encrypted_credentials"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)  # None = global
    kind: Mapped[str] = mapped_column(String(32))  # clash_api | proxy_auth | llm_api
    label: Mapped[str] = mapped_column(String(64), default="default")
    ciphertext: Mapped[str] = mapped_column(Text)
    fingerprint: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(TS)
    last_status: Mapped[str | None] = mapped_column(String(32))
    __table_args__ = (UniqueConstraint("user_id", "kind", "label"),)


class AccountSnapshot(Base):
    __tablename__ = "account_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("player_profiles.id", ondelete="CASCADE"), index=True)
    taken_at: Mapped[datetime] = mapped_column(TS, default=utcnow, index=True)
    trophies: Mapped[int | None] = mapped_column(Integer)
    best_trophies: Mapped[int | None] = mapped_column(Integer)
    exp_level: Mapped[int | None] = mapped_column(Integer)
    arena: Mapped[str | None] = mapped_column(String(64))
    wins: Mapped[int | None] = mapped_column(Integer)
    losses: Mapped[int | None] = mapped_column(Integer)
    clan_tag: Mapped[str | None] = mapped_column(String(16))
    summary: Mapped[dict] = mapped_column(JSON, default=dict)  # computed metrics + availability report
    raw: Mapped[dict] = mapped_column(JSON, default=dict)      # raw /players response (no secrets)


class CardCollectionSnapshot(Base):
    __tablename__ = "card_collection_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    snapshot_id: Mapped[int] = mapped_column(ForeignKey("account_snapshots.id", ondelete="CASCADE"), index=True)
    card_id: Mapped[int] = mapped_column(Integer, index=True)
    name: Mapped[str] = mapped_column(String(64))
    level: Mapped[int] = mapped_column(Integer)         # normalised display level
    raw_level: Mapped[int] = mapped_column(Integer)
    max_level: Mapped[int] = mapped_column(Integer)
    rarity: Mapped[str | None] = mapped_column(String(16))
    count: Mapped[int | None] = mapped_column(Integer)
    evolution_level: Mapped[int | None] = mapped_column(Integer)
    max_evolution_level: Mapped[int | None] = mapped_column(Integer)
    star_level: Mapped[int | None] = mapped_column(Integer)
    is_support: Mapped[bool] = mapped_column(Boolean, default=False)


class BattleRecord(Base):
    __tablename__ = "battle_records"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("player_profiles.id", ondelete="CASCADE"), index=True)
    battle_key: Mapped[str] = mapped_column(String(96))  # battleTime + opponent tag hash → dedupe
    battle_time: Mapped[datetime] = mapped_column(TS, index=True)
    battle_type: Mapped[str | None] = mapped_column(String(48))
    game_mode: Mapped[str | None] = mapped_column(String(64))
    result: Mapped[str] = mapped_column(String(8))  # win|loss|draw
    crowns: Mapped[int | None] = mapped_column(Integer)
    opponent_crowns: Mapped[int | None] = mapped_column(Integer)
    trophy_change: Mapped[int | None] = mapped_column(Integer)
    opponent_tag: Mapped[str | None] = mapped_column(String(16))
    player_deck: Mapped[list] = mapped_column(JSON, default=list)
    opponent_deck: Mapped[list] = mapped_column(JSON, default=list)
    raw: Mapped[dict] = mapped_column(JSON, default=dict)
    observed_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    __table_args__ = (UniqueConstraint("player_id", "battle_key"),)


# ----------------------------------------------------------------------------- knowledge base
class Card(Base):
    __tablename__ = "cards"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)  # official card id
    name: Mapped[str] = mapped_column(String(64), index=True)
    key: Mapped[str | None] = mapped_column(String(64), index=True)
    rarity: Mapped[str | None] = mapped_column(String(16))
    card_type: Mapped[str | None] = mapped_column(String(16))  # Troop|Spell|Building|Tower
    elixir: Mapped[int | None] = mapped_column(Integer)
    max_level: Mapped[int | None] = mapped_column(Integer)
    max_evolution_level: Mapped[int | None] = mapped_column(Integer)
    icon_url: Mapped[str | None] = mapped_column(String(512))
    evo_icon_url: Mapped[str | None] = mapped_column(String(512))
    is_support: Mapped[bool] = mapped_column(Boolean, default=False)
    roles: Mapped[list] = mapped_column(JSON, default=list)  # curated tactical role tags
    source: Mapped[str] = mapped_column(String(32), default="official_api")
    updated_at: Mapped[datetime] = mapped_column(TS, default=utcnow, onupdate=utcnow)


class CardStatVersion(Base):
    """Versioned gameplay stats — never overwritten, a new version is appended."""
    __tablename__ = "card_stat_versions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    card_id: Mapped[int] = mapped_column(ForeignKey("cards.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    stats: Mapped[dict] = mapped_column(JSON, default=dict)  # hp/damage per level, hit_speed, range, speed...
    source: Mapped[str] = mapped_column(String(64))
    source_url: Mapped[str | None] = mapped_column(String(512))
    verification: Mapped[str] = mapped_column(String(24), default="secondary")  # official|secondary|secondary-stale
    effective_from: Mapped[datetime | None] = mapped_column(TS)
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    __table_args__ = (UniqueConstraint("card_id", "version"),)


class BalanceChange(Base):
    __tablename__ = "balance_changes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    card_id: Mapped[int | None] = mapped_column(ForeignKey("cards.id", ondelete="SET NULL"), index=True)
    card_name: Mapped[str] = mapped_column(String(64), index=True)
    form: Mapped[str] = mapped_column(String(16), default="base")  # base|evolution|hero
    change_type: Mapped[str] = mapped_column(String(16))  # buff|nerf|rework|bugfix|neutral
    attribute: Mapped[str] = mapped_column(String(96))
    old_value: Mapped[str | None] = mapped_column(String(64))
    new_value: Mapped[str | None] = mapped_column(String(64))
    change_pct: Mapped[float | None] = mapped_column(Float)
    note: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(TS, index=True)
    effective_at: Mapped[datetime | None] = mapped_column(TS)
    source_url: Mapped[str] = mapped_column(String(512))
    verification: Mapped[str] = mapped_column(String(24), default="official")
    content_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)


class MetaSource(Base):
    """Every processed research document (dedupe + audit)."""
    __tablename__ = "meta_sources"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    url: Mapped[str] = mapped_column(String(512), unique=True)
    title: Mapped[str | None] = mapped_column(String(256))
    tier: Mapped[str] = mapped_column(String(16))  # official|secondary|unverified
    published_at: Mapped[datetime | None] = mapped_column(TS)
    content_hash: Mapped[str | None] = mapped_column(String(64))
    processed_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    items_extracted: Mapped[int] = mapped_column(Integer, default=0)


class ResearchCache(Base):
    __tablename__ = "research_cache"
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))  # search|extract|api
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    expires_at: Mapped[datetime] = mapped_column(TS, index=True)
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)


# ----------------------------------------------------------------------------- decks
class GeneratedDeck(Base):
    __tablename__ = "generated_decks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    player_id: Mapped[int | None] = mapped_column(ForeignKey("player_profiles.id", ondelete="SET NULL"))
    task_id: Mapped[str | None] = mapped_column(String(36), index=True)
    category: Mapped[str] = mapped_column(String(24))
    rank: Mapped[int] = mapped_column(Integer, default=1)
    card_ids: Mapped[list] = mapped_column(JSON, default=list)
    evolution_ids: Mapped[list] = mapped_column(JSON, default=list)
    avg_elixir: Mapped[float] = mapped_column(Float)
    score: Mapped[float] = mapped_column(Float)
    share_link: Mapped[str | None] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow, index=True)


class DeckEvaluation(Base):
    __tablename__ = "deck_evaluations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    deck_id: Mapped[int] = mapped_column(ForeignKey("generated_decks.id", ondelete="CASCADE"), index=True)
    breakdown: Mapped[dict] = mapped_column(JSON, default=dict)
    strengths: Mapped[list] = mapped_column(JSON, default=list)
    weaknesses: Mapped[list] = mapped_column(JSON, default=list)
    substitutions: Mapped[list] = mapped_column(JSON, default=list)
    ai_explanation: Mapped[str | None] = mapped_column(Text)
    weights_version: Mapped[str] = mapped_column(String(16), default="v1")
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)


# ----------------------------------------------------------------------------- agent
class AgentMemory(Base):
    __tablename__ = "agent_memory"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)  # None=global agent notes
    scope: Mapped[str] = mapped_column(String(16), default="user")  # user|agent
    key: Mapped[str] = mapped_column(String(64))
    content: Mapped[str] = mapped_column(Text)
    importance: Mapped[int] = mapped_column(Integer, default=5)
    updated_at: Mapped[datetime] = mapped_column(TS, default=utcnow, onupdate=utcnow)
    __table_args__ = (UniqueConstraint("user_id", "scope", "key"),)


class AgentSkill(Base):
    __tablename__ = "agent_skills"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    description: Mapped[str] = mapped_column(String(256))
    tags: Mapped[list] = mapped_column(JSON, default=list)
    content: Mapped[str] = mapped_column(Text)
    origin: Mapped[str] = mapped_column(String(16), default="bundled")  # bundled|agent|owner
    status: Mapped[str] = mapped_column(String(16), default="active")  # active|pending|archived
    usage_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    __table_args__ = (UniqueConstraint("name", "version"),)


class AgentTask(Base):
    __tablename__ = "agent_tasks"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(String(32))
    goal: Mapped[str] = mapped_column(String(256))
    description: Mapped[str | None] = mapped_column(Text)
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    required_tools: Mapped[list] = mapped_column(JSON, default=list)
    plan: Mapped[list] = mapped_column(JSON, default=list)  # [{step, tool, status}]
    current_step: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(24), default="QUEUED", index=True)
    progress: Mapped[int] = mapped_column(Integer, default=0)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow, index=True)
    started_at: Mapped[datetime | None] = mapped_column(TS)
    finished_at: Mapped[datetime | None] = mapped_column(TS)
    updated_at: Mapped[datetime] = mapped_column(TS, default=utcnow, onupdate=utcnow)


class TaskEvent(Base):
    __tablename__ = "task_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("agent_tasks.id", ondelete="CASCADE"), index=True)
    ts: Mapped[datetime] = mapped_column(TS, default=utcnow)
    level: Mapped[str] = mapped_column(String(8), default="info")
    event: Mapped[str] = mapped_column(String(48))
    message: Mapped[str] = mapped_column(Text)
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class NotificationState(Base):
    __tablename__ = "notification_state"
    task_id: Mapped[str] = mapped_column(ForeignKey("agent_tasks.id", ondelete="CASCADE"), primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    message_id: Mapped[int | None] = mapped_column(BigInteger)
    last_text_hash: Mapped[str | None] = mapped_column(String(64))
    last_sent_at: Mapped[datetime | None] = mapped_column(TS)
    sent_count: Mapped[int] = mapped_column(Integer, default=0)


# ----------------------------------------------------------------------------- network
class ProxyProfile(Base):
    __tablename__ = "proxy_profiles"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    region: Mapped[str] = mapped_column(String(64))
    scheme: Mapped[str] = mapped_column(String(10), default="http")  # http|https|socks5
    host: Mapped[str] = mapped_column(String(255))
    port: Mapped[int] = mapped_column(Integer)
    username: Mapped[str | None] = mapped_column(String(128))
    credential_id: Mapped[int | None] = mapped_column(ForeignKey("encrypted_credentials.id", ondelete="SET NULL"))
    trusted: Mapped[bool] = mapped_column(Boolean, default=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    observed_ipv4: Mapped[str | None] = mapped_column(String(45))
    observed_ipv6: Mapped[str | None] = mapped_column(String(45))
    approved_ip: Mapped[str | None] = mapped_column(String(45))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    last_success_at: Mapped[datetime | None] = mapped_column(TS)
    tests_total: Mapped[int] = mapped_column(Integer, default=0)
    tests_ok: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)


class ApiHealthRecord(Base):
    __tablename__ = "api_health_records"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(TS, default=utcnow, index=True)
    component: Mapped[str] = mapped_column(String(32))  # clash_api|telegram|llm|db|egress|github
    ok: Mapped[bool] = mapped_column(Boolean)
    status_code: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    egress_ip: Mapped[str | None] = mapped_column(String(45))
    detail: Mapped[str | None] = mapped_column(Text)


# ----------------------------------------------------------------------------- runtime
class WorkerLease(Base):
    __tablename__ = "worker_lease"
    name: Mapped[str] = mapped_column(String(32), primary_key=True)
    holder: Mapped[str] = mapped_column(String(96))
    acquired_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(TS)
    fencing_token: Mapped[int] = mapped_column(Integer, default=1)


class RecoveryCheckpoint(Base):
    __tablename__ = "recovery_checkpoints"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow, index=True)
    worker_id: Mapped[str] = mapped_column(String(96))
    reason: Mapped[str] = mapped_column(String(32))  # periodic|shutdown|startup
    telegram_offset: Mapped[int | None] = mapped_column(BigInteger)
    state: Mapped[dict] = mapped_column(JSON, default=dict)


class RunnerRun(Base):
    __tablename__ = "runner_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    worker_id: Mapped[str] = mapped_column(String(96), index=True)
    github_run_id: Mapped[str | None] = mapped_column(String(32))
    started_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(TS)
    exit_reason: Mapped[str | None] = mapped_column(String(48))
    updates_processed: Mapped[int] = mapped_column(Integer, default=0)
    dispatch_status: Mapped[str | None] = mapped_column(String(48))
    chain_index: Mapped[int] = mapped_column(Integer, default=0)


class ProcessedUpdate(Base):
    """Idempotency guard for Telegram updates (in addition to the persisted offset)."""
    __tablename__ = "processed_updates"
    update_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    processed_at: Mapped[datetime] = mapped_column(TS, default=utcnow, index=True)


class ErrorLog(Base):
    __tablename__ = "error_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(TS, default=utcnow, index=True)
    component: Mapped[str] = mapped_column(String(48))
    error_type: Mapped[str] = mapped_column(String(96))
    message: Mapped[str] = mapped_column(Text)
    user_id: Mapped[int | None] = mapped_column(Integer)


Index("ix_battle_player_time", BattleRecord.player_id, BattleRecord.battle_time)
Index("ix_task_user_status", AgentTask.user_id, AgentTask.status)
