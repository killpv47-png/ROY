"""Skills = procedural memory (Hermes concept), with progressive disclosure.

* Bundled skills live in `skills/<name>/SKILL.md` (YAML front-matter + markdown body) and are seeded
  into the versioned `agent_skills` table at startup.
* Level 0: `index()` — name + description only (cheap, used for routing).
* Level 1: `load(name)` — full body, loaded only when the current task needs it.
* The agent records improved versions after successful tasks (`record_improvement`); when
  `skills_write_approval` is on, agent-written versions are staged as `pending` until the owner approves.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

import yaml
from sqlalchemy import func, select

from ..db.models import AgentSkill, AppConfig

log = logging.getLogger(__name__)
_FM = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.S)
MAX_SKILL_CHARS = 6000


def parse_skill_md(text: str) -> tuple[dict, str]:
    m = _FM.match(text)
    if not m:
        raise ValueError("SKILL.md must start with YAML front-matter")
    meta = yaml.safe_load(m.group(1)) or {}
    if not meta.get("name") or not meta.get("description"):
        raise ValueError("front-matter requires name and description")
    return meta, m.group(2).strip()


def render_skill_md(name: str, description: str, version: int, tags: list[str], body: str) -> str:
    fm = yaml.safe_dump({"name": name, "description": description, "version": version, "tags": tags},
                        allow_unicode=True, sort_keys=False).strip()
    return f"---\n{fm}\n---\n\n{body.strip()}\n"


class SkillLibrary:
    def __init__(self, db, skills_dir: Path):
        self.db = db
        self.dir = Path(skills_dir)

    async def seed_bundled(self) -> int:
        """Insert bundled skills whose (name, version) is not yet stored. Never overwrites agent versions."""
        added = 0
        if not self.dir.is_dir():
            return 0
        for p in sorted(self.dir.glob("*/SKILL.md")):
            try:
                meta, body = parse_skill_md(p.read_text(encoding="utf-8"))
            except Exception as e:
                log.warning("invalid skill %s: %s", p, e)
                continue
            ver = int(meta.get("version", 1))
            async with self.db.session() as s:
                exists = (await s.execute(select(AgentSkill.id).where(AgentSkill.name == meta["name"],
                                                                      AgentSkill.version == ver))).first()
                if exists:
                    continue
                newer = (await s.execute(select(func.max(AgentSkill.version)).where(
                    AgentSkill.name == meta["name"]))).scalar()
                if newer and newer > ver:
                    continue
                s.add(AgentSkill(name=meta["name"], version=ver, description=str(meta["description"])[:256],
                                 tags=list(meta.get("tags") or []), content=body[:MAX_SKILL_CHARS], origin="bundled"))
                added += 1
        return added

    async def _latest(self, s, name: str, status: str = "active") -> AgentSkill | None:
        return (await s.execute(select(AgentSkill).where(AgentSkill.name == name, AgentSkill.status == status)
                                .order_by(AgentSkill.version.desc()).limit(1))).scalar_one_or_none()

    async def index(self) -> list[dict]:
        async with self.db.session() as s:
            rows = (await s.execute(select(AgentSkill).where(AgentSkill.status == "active")
                                    .order_by(AgentSkill.name, AgentSkill.version.desc()))).scalars().all()
        seen, out = set(), []
        for r in rows:
            if r.name in seen:
                continue
            seen.add(r.name)
            out.append({"name": r.name, "description": r.description, "version": r.version, "tags": r.tags,
                        "origin": r.origin, "usage": r.usage_count})
        return out

    async def load(self, name: str) -> dict | None:
        async with self.db.session() as s:
            r = await self._latest(s, name)
            if not r:
                return None
            r.usage_count += 1
            return {"name": r.name, "version": r.version, "description": r.description, "content": r.content}

    async def match(self, task_kind: str, text: str = "") -> list[str]:
        """Cheap routing: choose skills by task kind / tags without loading bodies."""
        kind_map = {
            "scan": ["clash_account_analysis", "player_progress_tracking"],
            "deck": ["deck_optimization"],
            "battles": ["battle_analysis"],
            "meta": ["card_meta_research", "balance_change_tracking"],
            "api_check": ["api_troubleshooting"],
            "ask": [],
        }
        names = list(kind_map.get(task_kind, []))
        low = text.lower()
        for sk in await self.index():
            if sk["name"] in names:
                continue
            if any(tag and tag.lower() in low for tag in sk.get("tags") or []):
                names.append(sk["name"])
        return names[:3]

    async def write_approval_required(self) -> bool:
        async with self.db.session() as s:
            row = await s.get(AppConfig, "skills_write_approval")
            return bool(row and row.value.get("v"))

    async def record_improvement(self, name: str, body: str, *, description: str | None = None,
                                 tags: list[str] | None = None, origin: str = "agent") -> dict:
        body = body.strip()[:MAX_SKILL_CHARS]
        if len(body) < 40:
            raise ValueError("skill body too short")
        status = "pending" if origin == "agent" and await self.write_approval_required() else "active"
        async with self.db.session() as s:
            cur = await self._latest(s, name)
            if cur and cur.content.strip() == body:
                return {"name": name, "version": cur.version, "status": "unchanged"}
            ver = ((await s.execute(select(func.max(AgentSkill.version)).where(AgentSkill.name == name))).scalar()
                   or 0) + 1
            s.add(AgentSkill(name=name, version=ver, description=(description or (cur.description if cur else name))[:256],
                             tags=tags if tags is not None else (cur.tags if cur else []), content=body,
                             origin=origin, status=status))
        return {"name": name, "version": ver, "status": status}

    async def pending(self) -> list[AgentSkill]:
        async with self.db.session() as s:
            return list((await s.execute(select(AgentSkill).where(AgentSkill.status == "pending"))).scalars())

    async def approve(self, skill_id: int, approve: bool = True) -> bool:
        async with self.db.session() as s:
            r = await s.get(AgentSkill, skill_id)
            if not r or r.status != "pending":
                return False
            r.status = "active" if approve else "archived"
            return True

    async def append_lesson(self, name: str, lesson: str) -> dict | None:
        """Self-improvement: add a generalisable pitfall to the skill's 'Lessons' section (deduplicated)."""
        cur = await self.load(name)
        if not cur:
            return None
        lesson = lesson.strip()[:200]
        if lesson.lower() in cur["content"].lower():
            return {"name": name, "status": "unchanged"}
        body = cur["content"]
        if "## Lessons" not in body:
            body += "\n\n## Lessons\n"
        body += f"\n- {lesson}"
        return await self.record_improvement(name, body)
