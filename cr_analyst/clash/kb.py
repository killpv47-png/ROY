"""Card knowledge base service: catalog sync, versioned stats, balance-change ingestion, search.

Source priority (see docs/RESEARCH.md):
  1. Official API `/cards`            -> catalog of record (source="official_api")
  2. Official Supercell blog          -> balance-change history (verification="official")
  3. RoyaleAPI cr-api-data (GitHub)   -> baseline per-level stats (verification="secondary-stale")
Nothing is ever fabricated: when a value is unknown it is stored as missing and reported as such.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import func, select

from ..db.models import BalanceChange, Card, CardStatVersion, MetaSource
from .balance import (
    OFFICIAL_BLOG, ParsedChange, extract_next_data, is_balance_article, parse_balance_article, parse_blog_index,
)
from .knowledge import card_key, roles_for

log = logging.getLogger(__name__)

RA_CARDS_URL = "https://royaleapi.github.io/cr-api-data/json/cards.json"
RA_STATS_URL = "https://royaleapi.github.io/cr-api-data/json/cards_stats.json"
UA = {"User-Agent": "Mozilla/5.0 (compatible; cr-analyst/1.0; +https://github.com)"}


def _type_from_id(cid: int) -> str:
    s = str(cid)
    return {"26": "Troop", "27": "Building", "28": "Spell"}.get(s[:2], "Tower" if s.startswith("159") else "Unknown")


class KnowledgeBase:
    def __init__(self, db, http: httpx.AsyncClient | None = None):
        self.db = db
        self._http = http

    async def _get(self, url: str) -> httpx.Response:
        if self._http is not None:
            return await self._http.get(url, headers=UA, follow_redirects=True)
        async with httpx.AsyncClient(timeout=25) as c:
            return await c.get(url, headers=UA, follow_redirects=True)

    # ------------------------------------------------------------------ catalog
    async def sync_official_catalog(self, cards_payload: dict) -> dict:
        """Upsert the official /cards response (items + supportItems)."""
        items = list(cards_payload.get("items") or [])
        support = list(cards_payload.get("supportItems") or [])
        added = updated = 0
        async with self.db.session() as s:
            for raw, is_support in [(i, False) for i in items] + [(i, True) for i in support]:
                cid = int(raw["id"])
                card = await s.get(Card, cid)
                icons = raw.get("iconUrls") or {}
                ctype = "Tower" if is_support else _type_from_id(cid)
                roles, _ = roles_for(raw.get("name", ""), ctype, raw.get("elixirCost"))
                fields = dict(name=raw.get("name"), key=card_key(raw.get("name", "")),
                              rarity=(raw.get("rarity") or "").lower() or None, card_type=ctype,
                              elixir=raw.get("elixirCost"), max_level=raw.get("maxLevel"),
                              max_evolution_level=raw.get("maxEvolutionLevel"), icon_url=icons.get("medium"),
                              evo_icon_url=icons.get("evolutionMedium"), is_support=is_support,
                              roles=sorted(roles), source="official_api")
                if card is None:
                    s.add(Card(id=cid, **fields))
                    added += 1
                else:
                    for k, v in fields.items():
                        if v is not None or k in ("max_evolution_level", "evo_icon_url"):
                            setattr(card, k, v)
                    updated += 1
        return {"added": added, "updated": updated, "total": len(items) + len(support), "source": "official_api"}

    async def bootstrap_secondary_catalog(self) -> dict:
        """Fallback catalog (only fills cards not yet known from the official API)."""
        r = await self._get(RA_CARDS_URL)
        r.raise_for_status()
        rows = r.json()
        added = 0
        async with self.db.session() as s:
            for c in rows:
                cid = int(c["id"])
                if await s.get(Card, cid):
                    continue
                roles, _ = roles_for(c["name"], c.get("type"), c.get("elixir"))
                s.add(Card(id=cid, name=c["name"], key=c.get("key") or card_key(c["name"]),
                           rarity=(c.get("rarity") or "").lower(), card_type=c.get("type"), elixir=c.get("elixir"),
                           roles=sorted(roles), source="royaleapi_secondary"))
                added += 1
        return {"added": added, "source": "royaleapi_secondary"}

    async def import_secondary_stats(self) -> dict:
        """Import per-level stats from cr-api-data as a *secondary-stale* version (never overwrites)."""
        r = await self._get(RA_STATS_URL)
        r.raise_for_status()
        data = r.json()
        by_name: dict[str, dict] = {}
        for ch in data.get("characters", []):
            if ch.get("id"):
                by_name[ch["key"]] = ch
        for grp in ("building", "spell"):
            for ch in data.get(grp, []):
                k = ch.get("key") or card_key(ch.get("name", ""))
                by_name.setdefault(k, ch)
        projectiles = {p.get("name"): p for p in data.get("projectile", [])}
        imported = 0
        async with self.db.session() as s:
            cards = (await s.execute(select(Card))).scalars().all()
            for card in cards:
                src = by_name.get(card.key or "")
                if not src:
                    continue
                stats = _extract_stats(src, projectiles)
                if not stats:
                    continue
                exists = (await s.execute(select(CardStatVersion).where(
                    CardStatVersion.card_id == card.id, CardStatVersion.source == "royaleapi/cr-api-data"))).first()
                if exists:
                    continue
                ver = (await s.execute(select(func.max(CardStatVersion.version)).where(
                    CardStatVersion.card_id == card.id))).scalar() or 0
                s.add(CardStatVersion(card_id=card.id, version=ver + 1, stats=stats, source="royaleapi/cr-api-data",
                                      source_url=RA_STATS_URL, verification="secondary-stale"))
                imported += 1
        return {"imported": imported, "verification": "secondary-stale"}

    # ------------------------------------------------------------------ balance changes
    async def research_official_balance(self, max_articles: int = 6) -> dict:
        """Check the official blog index and ingest new balance posts. Returns a structured report."""
        report: dict[str, Any] = {"checked": 0, "new_articles": [], "changes_added": 0, "errors": []}
        try:
            r = await self._get(OFFICIAL_BLOG)
            r.raise_for_status()
            nd = extract_next_data(r.text)
            if not nd:
                report["errors"].append("blog index: __NEXT_DATA__ not found (markup changed?)")
                return report
            articles = [a for a in parse_blog_index(nd) if is_balance_article(a["title"])][:max_articles]
        except httpx.HTTPError as e:
            report["errors"].append(f"blog index: {type(e).__name__}")
            return report
        for a in articles:
            report["checked"] += 1
            try:
                added = await self.ingest_article_url(a["url"], a["title"])
                if added is not None:
                    report["new_articles"].append({"title": a["title"], "url": a["url"],
                                                   "published_at": a["published_at"].isoformat()
                                                   if a["published_at"] else None, "changes": added})
                    report["changes_added"] += added
            except Exception as e:  # keep researching other posts
                report["errors"].append(f"{a['url']}: {type(e).__name__}")
        return report

    async def ingest_article_url(self, url: str, title: str | None = None) -> int | None:
        """Returns number of changes added, or None if the article was already processed unchanged."""
        r = await self._get(url)
        r.raise_for_status()
        nd = extract_next_data(r.text)
        if not nd:
            raise ValueError("article has no __NEXT_DATA__")
        return await self.ingest_article_data(nd, url, title)

    async def ingest_article_data(self, next_data: dict, url: str, title: str | None = None) -> int | None:
        import hashlib, json as _json
        body = (next_data.get("props") or {}).get("pageProps") or {}
        h = hashlib.sha256(_json.dumps(body.get("bodyCollection"), sort_keys=True).encode()).hexdigest()
        async with self.db.session() as s:
            src = (await s.execute(select(MetaSource).where(MetaSource.url == url))).scalar_one_or_none()
            if src and src.content_hash == h:
                return None
        changes = parse_balance_article(next_data, url)
        added = await self.store_changes(changes)
        async with self.db.session() as s:
            src = (await s.execute(select(MetaSource).where(MetaSource.url == url))).scalar_one_or_none()
            pub = changes[0].published_at if changes else None
            if src is None:
                s.add(MetaSource(url=url, title=title or body.get("title"), tier="official", published_at=pub,
                                 content_hash=h, items_extracted=len(changes)))
            else:
                src.content_hash, src.items_extracted = h, len(changes)
                src.processed_at = datetime.now(timezone.utc)
        return added

    async def store_changes(self, changes: list[ParsedChange]) -> int:
        added = 0
        async with self.db.session() as s:
            name_to_id = {c.name.lower(): c.id for c in (await s.execute(select(Card))).scalars().all()}
            for ch in changes:
                if (await s.execute(select(BalanceChange.id).where(
                        BalanceChange.content_hash == ch.content_hash))).first():
                    continue
                s.add(BalanceChange(card_id=name_to_id.get(ch.card_name.lower()), card_name=ch.card_name,
                                    form=ch.form, change_type=ch.change_type, attribute=ch.attribute,
                                    old_value=ch.old_value, new_value=ch.new_value, change_pct=ch.change_pct,
                                    note=ch.note, published_at=ch.published_at, effective_at=ch.effective_at,
                                    source_url=ch.source_url, verification=ch.verification,
                                    content_hash=ch.content_hash))
                added += 1
        return added

    # ------------------------------------------------------------------ queries
    async def catalog(self) -> dict[int, dict]:
        async with self.db.session() as s:
            rows = (await s.execute(select(Card))).scalars().all()
        return {c.id: {"id": c.id, "name": c.name, "key": c.key, "rarity": c.rarity, "card_type": c.card_type,
                       "elixir": c.elixir, "max_level": c.max_level, "max_evolution_level": c.max_evolution_level,
                       "icon_url": c.icon_url, "evo_icon_url": c.evo_icon_url, "roles": c.roles or [],
                       "source": c.source} for c in rows}

    async def search_cards(self, query: str, limit: int = 10) -> list[dict]:
        q = f"%{query.lower()}%"
        async with self.db.session() as s:
            rows = (await s.execute(select(Card).where(func.lower(Card.name).like(q)).limit(limit))).scalars().all()
        return [{"id": c.id, "name": c.name, "rarity": c.rarity, "type": c.card_type, "elixir": c.elixir,
                 "roles": c.roles, "source": c.source} for c in rows]

    async def card_details(self, name_or_id: str | int) -> dict | None:
        async with self.db.session() as s:
            if isinstance(name_or_id, int) or str(name_or_id).isdigit():
                card = await s.get(Card, int(name_or_id))
            else:
                card = (await s.execute(select(Card).where(func.lower(Card.name) == str(name_or_id).lower()))
                        ).scalar_one_or_none()
            if not card:
                return None
            stats = (await s.execute(select(CardStatVersion).where(CardStatVersion.card_id == card.id)
                                     .order_by(CardStatVersion.version.desc()))).scalars().first()
            changes = (await s.execute(select(BalanceChange).where(
                func.lower(BalanceChange.card_name) == card.name.lower()).order_by(
                BalanceChange.published_at.desc()).limit(15))).scalars().all()
        newest_change = max((c.effective_at or c.published_at for c in changes if (c.effective_at or c.published_at)),
                            default=None)
        stats_stale = bool(stats and stats.verification == "secondary-stale" and newest_change)
        return {
            "id": card.id, "name": card.name, "rarity": card.rarity, "type": card.card_type, "elixir": card.elixir,
            "max_level": card.max_level, "max_evolution_level": card.max_evolution_level, "roles": card.roles,
            "catalog_source": card.source,
            "stats": stats.stats if stats else None,
            "stats_source": {"source": stats.source, "verification": stats.verification,
                             "url": stats.source_url} if stats else None,
            "stats_warning": ("Baseline stats are from a secondary dataset and pre-date the official changes "
                              "listed below; the official change values take precedence.") if stats_stale else None,
            "balance_history": [{"attribute": c.attribute, "old": c.old_value, "new": c.new_value,
                                 "pct": c.change_pct, "type": c.change_type, "form": c.form,
                                 "effective": (c.effective_at or c.published_at).date().isoformat()
                                 if (c.effective_at or c.published_at) else None,
                                 "source": c.source_url, "verification": c.verification} for c in changes],
        }

    async def recent_changes(self, limit: int = 40) -> list[dict]:
        async with self.db.session() as s:
            rows = (await s.execute(select(BalanceChange).order_by(BalanceChange.published_at.desc(),
                                                                   BalanceChange.id).limit(limit))).scalars().all()
        return [{"card": c.card_name, "form": c.form, "type": c.change_type, "attribute": c.attribute,
                 "old": c.old_value, "new": c.new_value, "pct": c.change_pct, "note": c.note,
                 "effective": (c.effective_at or c.published_at).date().isoformat()
                 if (c.effective_at or c.published_at) else None, "source": c.source_url} for c in rows]

    async def card_trend(self, since_days: int = 60) -> dict[str, int]:
        """Net recent balance direction per card (+1 per buff, -1 per nerf) — used as a meta signal."""
        from datetime import timedelta
        cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)
        async with self.db.session() as s:
            rows = (await s.execute(select(BalanceChange))).scalars().all()
        trend: dict[str, int] = {}
        for c in rows:
            when = c.effective_at or c.published_at
            if when is None:
                continue
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            if when < cutoff or c.form == "hero":
                continue
            k = card_key(c.card_name)
            trend[k] = trend.get(k, 0) + (1 if c.change_type == "buff" else -1 if c.change_type == "nerf" else 0)
        return trend

    async def stats_summary(self) -> dict:
        async with self.db.session() as s:
            n_cards = (await s.execute(select(func.count(Card.id)))).scalar() or 0
            n_off = (await s.execute(select(func.count(Card.id)).where(Card.source == "official_api"))).scalar() or 0
            n_stats = (await s.execute(select(func.count(func.distinct(CardStatVersion.card_id))))).scalar() or 0
            n_changes = (await s.execute(select(func.count(BalanceChange.id)))).scalar() or 0
            last = (await s.execute(select(func.max(BalanceChange.published_at)))).scalar()
        return {"cards": n_cards, "official_cards": n_off, "cards_with_stats": n_stats,
                "balance_changes": n_changes, "latest_change": last.isoformat() if last else None}


def _extract_stats(src: dict, projectiles: dict) -> dict:
    keep = ["hitpoints", "hit_speed", "speed", "range", "sight_range", "deploy_time", "attacks_ground",
            "attacks_air", "target_only_buildings", "area_damage_radius", "damage", "dps", "lifetime",
            "flying_height", "radius", "count"]
    out = {k: src[k] for k in keep if src.get(k) not in (None, 0, False, "")}
    for k in ("hitpoints_per_level", "damage_per_level", "dps_per_level"):
        if src.get(k):
            out[k] = src[k]
    proj = projectiles.get(src.get("projectile") or "")
    if proj and proj.get("damage_per_level") and "damage_per_level" not in out:
        out["damage_per_level"] = proj["damage_per_level"]
    if "range" in out and isinstance(out["range"], (int, float)):
        out["range_tiles"] = round(out["range"] / 1000, 2)
    if "hit_speed" in out and isinstance(out["hit_speed"], (int, float)):
        out["hit_speed_s"] = round(out["hit_speed"] / 1000, 2)
    if out:
        out["level_basis"] = "index 0 = level 1 (cr-api-data convention)"
    return out
