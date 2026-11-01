"""Full account scanner: profile, card collection, battle log, chests, clan — with an explicit
availability report distinguishing retrieved / stored / calculated / unavailable data."""
from __future__ import annotations

import logging
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from sqlalchemy import select

from ..db.models import AccountSnapshot, BattleRecord, CardCollectionSnapshot, PlayerProfile
from .battles import analyze_battles, normalize_battle
from .cards import OwnedCard, parse_collection
from .client import ClashAPIError, ClashClient, NotFound
from .tags import normalize_tag

log = logging.getLogger(__name__)
Progress = Callable[[str, int], Awaitable[None]]

PROFILE_FIELDS = {"tag", "name", "expLevel", "trophies", "bestTrophies", "wins", "losses", "battleCount",
                  "threeCrownWins", "challengeCardsWon", "challengeMaxWins", "tournamentCardsWon",
                  "tournamentBattleCount", "role", "donations", "donationsReceived", "totalDonations", "warDayWins",
                  "clanCardsCollected", "clan", "arena", "leagueStatistics", "badges", "achievements", "cards",
                  "supportCards", "currentDeck", "currentDeckSupportCards", "currentFavouriteCard", "starPoints",
                  "expPoints", "totalExpPoints", "currentPathOfLegendSeasonResult",
                  "lastPathOfLegendSeasonResult", "bestPathOfLegendSeasonResult", "legacyTrophyRoadHighScore",
                  "progress"}


async def _noop(_m: str, _p: int) -> None:
    return None


def upgrade_priorities(cards: list[OwnedCard], gmax: int, deck_ids: set[int] | None = None,
                       limit: int = 8) -> list[dict]:
    """Deterministic ranking: current-deck cards first, then evolvable cards, then higher-level cards
    (closest to max = cheapest relative gain). Explained via `reasons`."""
    out = []
    for c in cards:
        if c.level >= gmax:
            continue
        score, reasons = c.level / 4, []
        if deck_ids and c.id in deck_ids:
            score += 3
            reasons.append("in_current_deck")
        if c.can_evolve:
            score += 1
            reasons.append("evolution_available")
        if c.roles and "win_condition" in c.roles:
            score += 0.5
            reasons.append("win_condition")
        out.append({"id": c.id, "name": c.name, "level": c.level, "rarity": c.rarity, "score": round(score, 2),
                    "reasons": reasons})
    out.sort(key=lambda x: -x["score"])
    return out[:limit]


def collection_summary(cards: list[OwnedCard], support: list[OwnedCard], gmax: int) -> dict:
    by_rarity: dict[str, list] = defaultdict(list)
    by_type: Counter = Counter()
    by_level: Counter = Counter()
    for c in cards:
        by_rarity[c.rarity or "unknown"].append(c.level)
        by_type[c.card_type] += 1
        by_level[c.level] += 1
    return {
        "total": len(cards),
        "global_max_level": gmax,
        "avg_level": round(sum(c.level for c in cards) / len(cards), 2) if cards else None,
        "maxed": sum(1 for c in cards if c.level >= gmax),
        "by_rarity": {r: {"count": len(v), "avg_level": round(sum(v) / len(v), 2)} for r, v in by_rarity.items()},
        "by_type": dict(by_type),
        "level_histogram": {str(k): v for k, v in sorted(by_level.items(), reverse=True)},
        "evolutions_unlocked": [c.name for c in cards if c.is_evolved],
        "evolvable_not_unlocked": [c.name for c in cards if c.can_evolve and not c.is_evolved],
        "evolution_data_available": any(c.max_evolution_level is not None or c.evolution_level is not None
                                        for c in cards),
        "champions": [c.name for c in cards if c.is_champion],
        "tower_troops": [{"name": s.name, "level": s.level} for s in support],
        "top_cards": [{"name": c.name, "level": c.level} for c in sorted(cards, key=lambda c: -c.level)[:10]],
    }


class AccountScanner:
    def __init__(self, db, client: ClashClient, kb):
        self.db = db
        self.client = client
        self.kb = kb

    async def ensure_player(self, user_id: int, tag: str) -> PlayerProfile:
        tag = normalize_tag(tag)
        async with self.db.session() as s:
            p = (await s.execute(select(PlayerProfile).where(PlayerProfile.user_id == user_id,
                                                             PlayerProfile.player_tag == tag))).scalar_one_or_none()
            if p is None:
                for o in (await s.execute(select(PlayerProfile).where(PlayerProfile.user_id == user_id))).scalars():
                    o.is_primary = False
                p = PlayerProfile(user_id=user_id, player_tag=tag, is_primary=True)
                s.add(p)
                await s.flush()
            return p

    async def full_scan(self, user_id: int, tag: str, progress: Progress = _noop, *,
                        include_chests: bool = True, include_clan: bool = True) -> dict[str, Any]:
        tag = normalize_tag(tag)
        availability: dict[str, str] = {}
        await progress("api_profile", 10)
        player = await self.client.get_player(tag)  # NotFound / auth errors propagate to the task
        availability["profile"] = "retrieved"
        catalog = await self.kb.catalog()
        if not catalog:
            try:
                await progress("api_catalog", 18)
                await self.kb.sync_official_catalog(await self.client.get_cards())
                catalog = await self.kb.catalog()
                availability["card_catalog"] = "retrieved"
            except ClashAPIError as e:
                availability["card_catalog"] = f"unavailable ({e.code})"
        else:
            availability["card_catalog"] = "stored"

        await progress("cards", 30)
        cards, support = parse_collection(player, catalog)
        gmax = max((c.max_level + (c.level - c.raw_level)) for c in cards) if cards else 16
        coll = collection_summary(cards, support, gmax)
        availability["card_collection"] = "retrieved" if cards else "unavailable (empty)"
        availability["evolutions"] = "retrieved" if coll["evolution_data_available"] else "not exposed by API response"
        availability["tower_troops"] = "retrieved" if support else "not exposed by API response"
        availability["hero_forms"] = ("retrieved" if any("hero" in k.lower() for c in cards for k in c.extra)
                                      else "not exposed by API response")

        await progress("api_battlelog", 45)
        battles_norm: list[dict] = []
        try:
            battles_norm = [normalize_battle(b, catalog) for b in await self.client.get_battlelog(tag)]
            availability["battle_log"] = f"retrieved ({len(battles_norm)} recent battles)"
        except ClashAPIError as e:
            availability["battle_log"] = f"unavailable ({e.code})"

        chests = None
        if include_chests:
            await progress("api_chests", 55)
            try:
                chests = await self.client.get_upcoming_chests(tag)
                availability["upcoming_chests"] = "retrieved"
            except NotFound:
                availability["upcoming_chests"] = "endpoint not available"
            except ClashAPIError as e:
                availability["upcoming_chests"] = f"unavailable ({e.code})"

        clan = None
        clan_tag = (player.get("clan") or {}).get("tag")
        if include_clan and clan_tag:
            await progress("api_clan", 62)
            try:
                c = await self.client.get_clan(clan_tag)
                clan = {k: c.get(k) for k in ("tag", "name", "type", "clanScore", "clanWarTrophies", "members",
                                              "requiredTrophies", "donationsPerWeek")}
                clan["location"] = (c.get("location") or {}).get("name")
                availability["clan"] = "retrieved"
            except ClashAPIError as e:
                availability["clan"] = f"unavailable ({e.code})"
        elif not clan_tag:
            availability["clan"] = "player not in a clan"

        await progress("persist", 72)
        p = await self.ensure_player(user_id, tag)
        new_battles = await self.store_battles(p.id, battles_norm)
        stored = await self.load_battles(p.id)
        previous = await self.latest_snapshot(p.id)

        await progress("analysis", 82)
        deck_ids = {c.get("id") for c in player.get("currentDeck") or []}
        current_deck = [{"id": c.id, "name": c.name, "level": c.level, "evo": c.evolution_level or 0}
                        for c in cards if c.id in deck_ids]
        profile = {
            "tag": player.get("tag"), "name": player.get("name"), "exp_level": player.get("expLevel"),
            "trophies": player.get("trophies"), "best_trophies": player.get("bestTrophies"),
            "arena": (player.get("arena") or {}).get("name"), "wins": player.get("wins"),
            "losses": player.get("losses"), "battle_count": player.get("battleCount"),
            "three_crown_wins": player.get("threeCrownWins"), "clan_role": player.get("role"),
            "clan": (player.get("clan") or {}).get("name"), "clan_tag": clan_tag,
            "donations": player.get("donations"), "star_points": player.get("starPoints"),
            "favourite_card": (player.get("currentFavouriteCard") or {}).get("name"),
            "path_of_legend": player.get("currentPathOfLegendSeasonResult"),
            "best_path_of_legend": player.get("bestPathOfLegendSeasonResult"),
            "achievements": [{"name": a.get("name"), "stars": a.get("stars"), "value": a.get("value")}
                             for a in (player.get("achievements") or [])][:30],
            "badges_count": len(player.get("badges") or []),
            "unknown_fields": sorted(k for k in player if k not in PROFILE_FIELDS),
            "missing_fields": [f for f in ("trophies", "bestTrophies", "expLevel", "arena", "wins", "losses")
                               if f not in player],
        }
        progress_cmp = None
        if previous:
            prev_coll = (previous.summary or {}).get("collection", {})
            progress_cmp = {
                "since": previous.taken_at.isoformat(),
                "trophies_delta": (player.get("trophies") or 0) - (previous.trophies or 0),
                "exp_delta": (player.get("expLevel") or 0) - (previous.exp_level or 0),
                "avg_level_delta": round((coll["avg_level"] or 0) - (prev_coll.get("avg_level") or 0), 2),
                "cards_delta": coll["total"] - (prev_coll.get("total") or 0),
            }
        result = {
            "player_id": p.id, "profile": profile, "collection": coll, "current_deck": current_deck,
            "upgrade_priorities": upgrade_priorities(cards, gmax, deck_ids),
            "battles_recent": analyze_battles(battles_norm, source="api_recent"),
            "battles_history": analyze_battles(stored, source="local_history") if len(stored) > len(battles_norm)
            else None,
            "new_battles_stored": new_battles, "stored_battles_total": len(stored),
            "chests": [{"index": c.get("index"), "name": c.get("name")} for c in (chests or {}).get("items", [])][:10]
            if chests else None,
            "clan": clan, "progress": progress_cmp, "availability": availability,
            "scanned_at": datetime.now(timezone.utc).isoformat(),
        }
        await progress("snapshot", 92)
        await self.save_snapshot(p.id, player, cards, support, result)
        return result

    async def save_snapshot(self, player_id: int, player: dict, cards: list[OwnedCard], support: list[OwnedCard],
                            result: dict) -> int:
        async with self.db.session() as s:
            raw = {k: v for k, v in player.items() if k not in ("cards", "supportCards", "achievements", "badges")}
            snap = AccountSnapshot(player_id=player_id, trophies=player.get("trophies"),
                                   best_trophies=player.get("bestTrophies"), exp_level=player.get("expLevel"),
                                   arena=(player.get("arena") or {}).get("name"), wins=player.get("wins"),
                                   losses=player.get("losses"), clan_tag=(player.get("clan") or {}).get("tag"),
                                   summary={"collection": result["collection"], "availability": result["availability"]},
                                   raw=raw)
            s.add(snap)
            await s.flush()
            for c in cards + support:
                s.add(CardCollectionSnapshot(snapshot_id=snap.id, card_id=c.id, name=c.name, level=c.level,
                                             raw_level=c.raw_level, max_level=c.max_level, rarity=c.rarity,
                                             count=c.count, evolution_level=c.evolution_level,
                                             max_evolution_level=c.max_evolution_level, star_level=c.star_level,
                                             is_support=c.is_support))
            pp = await s.get(PlayerProfile, player_id)
            if pp:
                pp.name = player.get("name")
                pp.last_scan_at = datetime.now(timezone.utc)
            return snap.id

    async def store_battles(self, player_id: int, battles: list[dict]) -> int:
        if not battles:
            return 0
        added = 0
        async with self.db.session() as s:
            keys = {b["battle_key"] for b in battles}
            existing = set((await s.execute(select(BattleRecord.battle_key).where(
                BattleRecord.player_id == player_id, BattleRecord.battle_key.in_(keys)))).scalars().all())
            for b in battles:
                if b["battle_key"] in existing or not b.get("battle_time"):
                    continue
                existing.add(b["battle_key"])
                s.add(BattleRecord(player_id=player_id, battle_key=b["battle_key"], battle_time=b["battle_time"],
                                   battle_type=b.get("battle_type"), game_mode=b.get("game_mode"), result=b["result"],
                                   crowns=b.get("crowns"), opponent_crowns=b.get("opponent_crowns"),
                                   trophy_change=b.get("trophy_change"), opponent_tag=b.get("opponent_tag"),
                                   player_deck=b.get("player_deck") or [], opponent_deck=b.get("opponent_deck") or [],
                                   raw={k: v for k, v in b.items()
                                        if k not in ("battle_time", "player_deck", "opponent_deck")}))
                added += 1
            pp = await s.get(PlayerProfile, player_id)
            if pp:
                pp.last_battle_poll_at = datetime.now(timezone.utc)
        return added

    async def load_battles(self, player_id: int, days: int | None = None) -> list[dict]:
        async with self.db.session() as s:
            q = select(BattleRecord).where(BattleRecord.player_id == player_id)
            if days:
                q = q.where(BattleRecord.battle_time >= datetime.now(timezone.utc) - timedelta(days=days))
            rows = (await s.execute(q.order_by(BattleRecord.battle_time.desc()))).scalars().all()
        out = []
        for r in rows:
            bt = r.battle_time if r.battle_time.tzinfo else r.battle_time.replace(tzinfo=timezone.utc)
            out.append({"battle_key": r.battle_key, "battle_time": bt, "battle_type": r.battle_type,
                        "game_mode": r.game_mode, "result": r.result, "crowns": r.crowns,
                        "opponent_crowns": r.opponent_crowns, "trophy_change": r.trophy_change,
                        "player_deck": r.player_deck, "opponent_deck": r.opponent_deck})
        return out

    async def latest_snapshot(self, player_id: int) -> AccountSnapshot | None:
        async with self.db.session() as s:
            return (await s.execute(select(AccountSnapshot).where(AccountSnapshot.player_id == player_id)
                                    .order_by(AccountSnapshot.taken_at.desc(), AccountSnapshot.id.desc())
                                    .limit(1))).scalar_one_or_none()

    async def latest_collection(self, player_id: int) -> tuple[list[dict], AccountSnapshot | None]:
        snap = await self.latest_snapshot(player_id)
        if not snap:
            return [], None
        async with self.db.session() as s:
            rows = (await s.execute(select(CardCollectionSnapshot).where(
                CardCollectionSnapshot.snapshot_id == snap.id))).scalars().all()
        return [{"id": r.card_id, "name": r.name, "level": r.level, "raw_level": r.raw_level,
                 "max_level": r.max_level, "rarity": r.rarity, "count": r.count, "evolution_level": r.evolution_level,
                 "max_evolution_level": r.max_evolution_level, "star_level": r.star_level,
                 "is_support": r.is_support} for r in rows], snap

    async def poll_battles(self, player_id: int, tag: str) -> int:
        catalog = await self.kb.catalog()
        raw = await self.client.get_battlelog(tag)
        return await self.store_battles(player_id, [normalize_battle(b, catalog) for b in raw])
