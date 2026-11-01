"""Service container: builds and wires every component once (bot, runner, CLI and tests share it)."""
from __future__ import annotations

import logging
import os
from pathlib import Path

from sqlalchemy import func, select

from .agent.llm import OpenAICompatibleLLM
from .agent.memory import MemoryStore
from .agent.notifier import Notifier
from .agent.orchestrator import Orchestrator
from .agent.skills import SkillLibrary
from .agent.tasks import TaskManager
from .agent.terminal import Terminal
from .agent.tools.builtin import build_registry
from .agent.web import WebResearcher
from .clash.client import ClashClient
from .clash.kb import KnowledgeBase
from .clash.scanner import AccountScanner
from .config import Settings
from .db import Database
from .db.models import AppConfig, DeckEvaluation, ErrorLog, GeneratedDeck
from .network import NetworkManager
from .security.crypto import CryptoError, SecretBox
from .security.redact import redact, register_secrets
from .services.credentials import CredentialService
from .services.users import UserService, expand_permissions

log = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parent.parent


class ErrorService:
    def __init__(self, db):
        self.db = db

    async def record(self, component: str, error_type: str, message: str, user_id: int | None = None) -> None:
        try:
            async with self.db.session() as s:
                s.add(ErrorLog(component=component[:48], error_type=error_type[:96], message=redact(message)[:2000],
                               user_id=user_id))
        except Exception:  # never let error logging raise
            log.debug("error log write failed", exc_info=True)

    async def summary(self, limit: int = 10) -> list[dict]:
        async with self.db.session() as s:
            rows = (await s.execute(select(ErrorLog.component, ErrorLog.error_type, func.count(),
                                           func.max(ErrorLog.ts)).group_by(ErrorLog.component, ErrorLog.error_type)
                                    .order_by(func.max(ErrorLog.ts).desc()).limit(limit))).all()
        return [{"component": c, "type": t, "count": n, "last": ts.isoformat() if ts else None} for c, t, n, ts in rows]


class DeckStore:
    def __init__(self, db):
        self.db = db

    async def save(self, user_id: int, player_id: int | None, task_id: str | None, data: dict) -> list[int]:
        ids = []
        async with self.db.session() as s:
            for d in data["decks"]:
                gd = GeneratedDeck(user_id=user_id, player_id=player_id, task_id=task_id, category=d["category"],
                                   rank=d["rank"], card_ids=[c["id"] for c in d["cards"]],
                                   evolution_ids=d.get("evolution_ids") or [], avg_elixir=d["avg_elixir"],
                                   score=d["score"], share_link=d.get("share_link"))
                s.add(gd)
                await s.flush()
                s.add(DeckEvaluation(deck_id=gd.id, breakdown=d["breakdown"], strengths=d["strengths"],
                                     weaknesses=d["weaknesses"], substitutions=d["substitutions"],
                                     ai_explanation=data.get("ai_explanation") if d["rank"] == 1 else None))
                ids.append(gd.id)
        return ids

    async def recent(self, user_id: int, limit: int = 6) -> list[GeneratedDeck]:
        async with self.db.session() as s:
            return list((await s.execute(select(GeneratedDeck).where(GeneratedDeck.user_id == user_id)
                                         .order_by(GeneratedDeck.created_at.desc()).limit(limit))).scalars())


class Services:
    def __init__(self, settings: Settings, *, sender=None, llm=None, clash_transport=None, web_transport=None,
                 net_transport_factory=None):
        self.settings = settings
        register_secrets(settings.secret_values())
        self.project_root = PROJECT_ROOT
        self.db = Database(settings.database_url)
        try:
            self.box: SecretBox | None = SecretBox(settings.app_encryption_key) if settings.app_encryption_key else None
        except CryptoError as e:
            log.error("encryption disabled: %s", e)
            self.box = None
        self.kb = KnowledgeBase(self.db)
        self.network = NetworkManager(self.db, self.box, expected_ip=settings.expected_egress_ip,
                                      is_github_runner=os.getenv("GITHUB_ACTIONS") == "true",
                                      transport_factory=net_transport_factory)
        self.credentials = CredentialService(self.db, self.box, settings.clash_api_token)
        from .i18n import DEFAULT_LANG
        self.users = UserService(self.db, settings.admin_id, DEFAULT_LANG)
        self.tasks = TaskManager(self.db)
        self.memory = MemoryStore(self.db)
        self.skills = SkillLibrary(self.db, settings.skills_dir)
        self.web = WebResearcher(self.db, settings.effective_search_provider, settings.search_api_key,
                                 transport=web_transport, tavily_key=settings.tavily_api_key,
                                 tavily_depth=settings.tavily_search_depth)
        self.terminal = Terminal(settings.workspace_dir, PROJECT_ROOT)
        self.notifier = Notifier(self.db, sender)
        self.errors = ErrorService(self.db)
        self.decks = DeckStore(self.db)
        if llm is not None:
            self.llm = llm
        elif settings.llm_enabled:
            self.llm = OpenAICompatibleLLM(settings.llm_api_key, settings.llm_model, settings.llm_base_url,
                                           temperature=settings.llm_temperature, max_tokens=settings.llm_max_tokens)
        else:
            self.llm = None
        self._clash_transport = clash_transport
        self._clash_clients: dict[int | None, ClashClient] = {}
        self.registry = build_registry(self)
        self.orchestrator = Orchestrator(self)

    async def init(self) -> None:
        """Create schema (idempotent; Alembic migrations are the production path), seed reference data."""
        await self.db.create_all()
        await self.users.seed_permissions()
        await self.users.init_owner_from_env()
        n = await self.skills.seed_bundled()
        if n:
            log.info("seeded %d bundled skills", n)
        await self.load_runtime_overrides()

    async def load_runtime_overrides(self) -> None:
        """Owner-managed AI model settings stored in app_config override env defaults."""
        cfg = await self.get_config("llm_settings", {}) or {}
        if self.llm is not None and isinstance(self.llm, OpenAICompatibleLLM):
            if cfg.get("model"):
                self.llm.model = cfg["model"]
            if cfg.get("temperature") is not None:
                self.llm.temperature = float(cfg["temperature"])
            if cfg.get("max_tokens"):
                self.llm.max_tokens = int(cfg["max_tokens"])

    async def get_config(self, key: str, default=None):
        async with self.db.session() as s:
            row = await s.get(AppConfig, key)
            return row.value.get("v", default) if row else default

    async def set_config(self, key: str, value) -> None:
        async with self.db.session() as s:
            row = await s.get(AppConfig, key)
            if row:
                row.value = {"v": value}
            else:
                s.add(AppConfig(key=key, value={"v": value}))

    async def close(self) -> None:
        for c in self._clash_clients.values():
            await c.aclose()
        if self.llm is not None and hasattr(self.llm, "aclose"):
            await self.llm.aclose()
        await self.db.dispose()

    # ------------------------------------------------------------------ per-user helpers
    def clash_for(self, user_id: int | None) -> ClashClient:
        if user_id not in self._clash_clients:
            async def token():
                v, _ = await self.credentials.clash_token_for(user_id)
                return v

            async def health(ok, status, latency, egress, detail):
                await self.network.record_health("clash_api", ok, status, latency, egress, detail)

            self._clash_clients[user_id] = ClashClient(self.settings.clash_api_base, token, self.network.resolve_route,
                                                       transport=self._clash_transport, health_hook=health)
        return self._clash_clients[user_id]

    def scanner_for(self, user_id: int | None) -> AccountScanner:
        return AccountScanner(self.db, self.clash_for(user_id), self.kb)

    async def permissions_for(self, user) -> set[str]:
        if user is None or user.role == "banned" or not user.is_active:
            return set()
        return expand_permissions(user.role, await self.users.permission_table())

    async def deck_weights(self) -> dict | None:
        from .decks.engine import load_weights
        stored = await self.get_config("deck_weights")
        if stored:
            return stored
        if self.settings.deck_weights_file:
            return load_weights(self.settings.deck_weights_file)
        return None

    @staticmethod
    async def noop_progress(_k: str, _p: int) -> None:
        return None
