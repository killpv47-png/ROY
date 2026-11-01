"""Persistent per-user memory (Hermes-style bounded, curated memory).

* Small durable facts keyed per user (`user` scope) + agent operational notes (`agent` scope, global).
* A compact summary with a hard character budget is injected into prompts; detailed history (scans,
  battles, decks) is retrieved from the database only when a task needs it.
* Entries are scanned for prompt-injection and secret patterns before being accepted.
* Strict isolation: every query is filtered by user_id; agent-scope notes never contain user data.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from sqlalchemy import select

from ..db.models import AgentMemory
from ..security.redact import redact

USER_CHAR_LIMIT = 1375
AGENT_CHAR_LIMIT = 2200
_INJECTION = re.compile(r"(?i)(ignore (all|previous|the above) instructions|system prompt|you are now|"
                        r"disregard .*rules|reveal .*(key|token|secret)|BEGIN [A-Z ]*PRIVATE KEY)")
_INVISIBLE = re.compile("[\u200b-\u200d\u2060\ufeff\u202a-\u202e]")


class MemoryRejected(ValueError):
    pass


def scan_entry(content: str) -> str:
    c = _INVISIBLE.sub("", content).strip()
    if not c:
        raise MemoryRejected("empty")
    if _INJECTION.search(c):
        raise MemoryRejected("injection_pattern")
    if redact(c) != c:
        raise MemoryRejected("contains_secret")
    return c[:400]


class MemoryStore:
    def __init__(self, db):
        self.db = db

    async def remember(self, user_id: int | None, key: str, content: str, *, scope: str = "user",
                       importance: int = 5) -> bool:
        """Upsert by key (dedupe). Enforces the budget by evicting least-important/oldest entries."""
        if scope == "user" and user_id is None:
            raise MemoryRejected("user scope requires user_id")
        if scope == "agent":
            user_id = None
        content = scan_entry(content)
        limit = USER_CHAR_LIMIT if scope == "user" else AGENT_CHAR_LIMIT
        async with self.db.session() as s:
            q = select(AgentMemory).where(AgentMemory.scope == scope)
            q = q.where(AgentMemory.user_id.is_(None)) if user_id is None else q.where(AgentMemory.user_id == user_id)
            rows = list((await s.execute(q)).scalars())
            existing = next((r for r in rows if r.key == key), None)
            if existing and existing.content == content:
                return False
            if existing:
                existing.content, existing.importance = content, importance
                existing.updated_at = datetime.now(timezone.utc)
            else:
                existing = AgentMemory(user_id=user_id, scope=scope, key=key[:64], content=content,
                                       importance=importance)
                s.add(existing)
                rows.append(existing)
            total = sum(len(r.content) for r in rows)
            evictable = sorted((r for r in rows if r is not existing),
                               key=lambda r: (r.importance, r.updated_at or datetime.min.replace(tzinfo=timezone.utc)))
            while total > limit and evictable:
                victim = evictable.pop(0)
                total -= len(victim.content)
                await s.delete(victim)
            return True

    async def forget(self, user_id: int | None, key: str, scope: str = "user") -> bool:
        async with self.db.session() as s:
            q = select(AgentMemory).where(AgentMemory.scope == scope, AgentMemory.key == key)
            q = q.where(AgentMemory.user_id.is_(None)) if user_id is None else q.where(AgentMemory.user_id == user_id)
            row = (await s.execute(q)).scalar_one_or_none()
            if not row:
                return False
            await s.delete(row)
            return True

    async def entries(self, user_id: int | None, scope: str = "user") -> list[AgentMemory]:
        async with self.db.session() as s:
            q = select(AgentMemory).where(AgentMemory.scope == scope)
            q = q.where(AgentMemory.user_id.is_(None)) if user_id is None else q.where(AgentMemory.user_id == user_id)
            return list((await s.execute(q.order_by(AgentMemory.importance.desc(), AgentMemory.key))).scalars())

    async def get(self, user_id: int, key: str) -> str | None:
        for e in await self.entries(user_id):
            if e.key == key:
                return e.content
        return None

    async def summary(self, user_id: int, include_agent: bool = True) -> str:
        """Compact block for the system prompt (frozen per task)."""
        user = await self.entries(user_id, "user")
        lines = [f"- {e.key}: {e.content}" for e in user]
        out = "USER MEMORY:\n" + ("\n".join(lines) if lines else "- (empty)")
        if include_agent:
            notes = await self.entries(None, "agent")
            if notes:
                out += "\nAGENT NOTES:\n" + "\n".join(f"- {e.content}" for e in notes[:8])
        return out[: USER_CHAR_LIMIT + AGENT_CHAR_LIMIT // 2]

    async def usage(self, user_id: int) -> dict:
        user = await self.entries(user_id, "user")
        used = sum(len(e.content) for e in user)
        return {"entries": len(user), "chars": used, "limit": USER_CHAR_LIMIT,
                "pct": round(used / USER_CHAR_LIMIT * 100, 1)}

    async def wipe_user(self, user_id: int) -> int:
        entries = await self.entries(user_id, "user")
        async with self.db.session() as s:
            for e in entries:
                obj = await s.get(AgentMemory, e.id)
                if obj:
                    await s.delete(obj)
        return len(entries)
