"""Deterministic deck generation and scoring engine.

Inputs are the player's *actual* collection (normalised levels), the curated qualitative role table,
recent official balance direction (meta signal) and optional battle-history pressure.
No numeric gameplay stats are invented: scoring uses levels, roles, elixir costs from the catalog,
synergy pairs and verifiable balance-change direction only. Weights are configurable and versioned.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from itertools import combinations
from pathlib import Path
from typing import Any

from ..clash.knowledge import archetypes_of, card_key, roles_for, synergy

DECK_SIZE = 8
SHARE_BASE = "https://link.clashroyale.com/deck/en?deck="

FACTORS = ["level", "synergy", "win_condition", "defense", "offense", "air_defense", "ground_defense", "spells",
           "elixir", "evolution", "meta", "matchup"]

DEFAULT_WEIGHTS: dict[str, float] = {
    "level": 0.20, "synergy": 0.10, "win_condition": 0.10, "defense": 0.09, "offense": 0.08, "air_defense": 0.09,
    "ground_defense": 0.07, "spells": 0.08, "elixir": 0.07, "evolution": 0.05, "meta": 0.04, "matchup": 0.03,
}


@dataclass(frozen=True)
class Category:
    key: str
    elixir_range: tuple[float, float]
    weight_mods: dict[str, float]
    prefer_roles: tuple[str, ...] = ()
    prefer_archetypes: tuple[str, ...] = ()


CATEGORIES: dict[str, Category] = {
    "defensive": Category("defensive", (3.5, 4.3), {"defense": 1.8, "air_defense": 1.4, "ground_defense": 1.4,
                                                   "offense": 0.6}, ("building", "anti_tank_dps", "splash")),
    "offensive": Category("offensive", (3.3, 4.2), {"offense": 1.9, "win_condition": 1.4, "defense": 0.7},
                          ("win_condition", "tank"), ("beatdown", "bridge_spam")),
    "balanced": Category("balanced", (3.2, 4.0), {}),
    "cycle": Category("cycle", (2.6, 3.2), {"elixir": 2.2, "offense": 1.1}, ("cycle", "small_spell"), ("cycle",)),
    "counter_attack": Category("counter_attack", (3.2, 3.9), {"defense": 1.3, "offense": 1.3, "synergy": 1.2},
                               ("mini_tank", "anti_tank_dps"), ("bridge_spam",)),
    "high_level": Category("high_level", (3.0, 4.4), {"level": 2.6, "synergy": 0.8, "meta": 0.6}),
    "meta": Category("meta", (3.0, 4.1), {"meta": 3.0, "evolution": 1.5}),
    "custom": Category("custom", (3.0, 4.2), {}),
}


@dataclass
class DeckCard:
    id: int
    name: str
    level: int
    elixir: int | None
    rarity: str | None
    card_type: str | None
    roles: frozenset[str]
    curated: bool
    evolution_level: int = 0
    max_evolution_level: int = 0
    icon_url: str | None = None
    evo_icon_url: str | None = None

    @property
    def key(self) -> str:
        return card_key(self.name)

    @property
    def evolved(self) -> bool:
        return self.evolution_level > 0


@dataclass
class DeckResult:
    category: str
    cards: list[DeckCard]
    evolution_ids: list[int]
    avg_elixir: float
    cycle_cost: int
    score: float
    breakdown: dict[str, float]
    strengths: list[str]
    weaknesses: list[str]
    substitutions: list[dict]
    archetype: str | None
    share_link: str | None
    uncertainty: list[str] = field(default_factory=list)
    rank: int = 1

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for c in d["cards"]:
            c["roles"] = sorted(c["roles"])
        return d


def load_weights(path: str | None = None, overrides: dict[str, float] | None = None) -> dict[str, float]:
    w = dict(DEFAULT_WEIGHTS)
    if path and Path(path).is_file():
        w.update({k: float(v) for k, v in json.loads(Path(path).read_text()).items() if k in w})
    if overrides:
        w.update({k: float(v) for k, v in overrides.items() if k in w})
    total = sum(max(0.0, v) for v in w.values()) or 1.0
    return {k: max(0.0, v) / total for k, v in w.items()}


def to_deck_cards(collection: list[dict], catalog: dict[int, dict]) -> list[DeckCard]:
    out = []
    for c in collection:
        if c.get("is_support"):
            continue
        cat = catalog.get(int(c["id"]), {})
        elixir = c.get("elixir", cat.get("elixir"))
        ctype = c.get("card_type") or cat.get("card_type")
        roles, curated = roles_for(c["name"], ctype, elixir)
        if cat.get("roles") and not curated:
            roles |= set(cat["roles"])
        out.append(DeckCard(id=int(c["id"]), name=c["name"], level=int(c["level"]), elixir=elixir,
                            rarity=c.get("rarity") or cat.get("rarity"), card_type=ctype, roles=frozenset(roles),
                            curated=curated, evolution_level=int(c.get("evolution_level") or 0),
                            max_evolution_level=int(c.get("max_evolution_level") or cat.get("max_evolution_level") or 0),
                            icon_url=cat.get("icon_url"), evo_icon_url=cat.get("evo_icon_url")))
    return out


def _has(deck: list[DeckCard], role: str) -> int:
    return sum(1 for c in deck if role in c.roles)


class DeckEngine:
    def __init__(self, weights: dict[str, float] | None = None, evolution_slots: int = 2):
        self.base_weights = load_weights(overrides=weights) if weights else load_weights()
        self.evolution_slots = evolution_slots

    # ------------------------------------------------------------------ scoring
    def category_weights(self, category: str, custom: dict[str, float] | None = None) -> dict[str, float]:
        cat = CATEGORIES.get(category, CATEGORIES["balanced"])
        w = {k: v * cat.weight_mods.get(k, 1.0) for k, v in self.base_weights.items()}
        if category == "custom" and custom:
            w.update({k: float(v) for k, v in custom.items() if k in w})
        total = sum(w.values()) or 1.0
        return {k: v / total for k, v in w.items()}

    def evaluate(self, deck: list[DeckCard], category: str, gmax: int, meta_trend: dict[str, int] | None = None,
                 pressure: dict[str, int] | None = None, custom: dict[str, float] | None = None) -> tuple[float, dict]:
        cat = CATEGORIES.get(category, CATEGORIES["balanced"])
        meta_trend = meta_trend or {}
        b: dict[str, float] = {}
        # level: 1.0 when all cards at gmax; -0.12 per level below
        b["level"] = max(0.0, sum(max(0.0, 1 - (gmax - c.level) * 0.12) for c in deck) / len(deck))
        pairs = sum(1 for a, c in combinations(deck, 2) if synergy(a.name, c.name))
        b["synergy"] = min(1.0, pairs / 3)
        wc = _has(deck, "win_condition")
        b["win_condition"] = 1.0 if wc in (1, 2) else 0.5 if wc == 3 else 0.0
        air = _has(deck, "air_defense")
        b["air_defense"] = min(1.0, air / 3)
        anti_tank = _has(deck, "anti_tank_dps") + 0.5 * _has(deck, "building")
        splash = _has(deck, "splash")
        mini = _has(deck, "mini_tank")
        b["ground_defense"] = min(1.0, (min(anti_tank, 1.5) / 1.5) * 0.5 + min(splash, 2) / 2 * 0.3 + min(mini, 1) * 0.2)
        b["defense"] = round((b["air_defense"] + b["ground_defense"] + min(1.0, _has(deck, "building") * 0.5 +
                                                                            _has(deck, "control") * 0.25)) / 3, 4)
        tank = _has(deck, "tank")
        b["offense"] = min(1.0, (0.55 if wc else 0) + 0.15 * min(tank, 1) + 0.1 * min(_has(deck, "swarm"), 2) +
                           0.2 * (1 if pairs and wc else 0))
        small, big = _has(deck, "small_spell"), _has(deck, "big_spell")
        b["spells"] = 1.0 if (small >= 1 and big >= 1) else 0.6 if (small + big) >= 1 else 0.0
        if small + big > 3:
            b["spells"] -= 0.3
        avg = self.avg_elixir(deck)
        lo, hi = cat.elixir_range
        b["elixir"] = 1.0 if lo <= avg <= hi else max(0.0, 1 - min(abs(avg - lo), abs(avg - hi)) / 1.2)
        evo_owned = sorted((c for c in deck if c.evolved), key=lambda c: -c.level)[: self.evolution_slots]
        b["evolution"] = len(evo_owned) / self.evolution_slots if self.evolution_slots else 0.0
        trend = sum(meta_trend.get(c.key, 0) for c in deck)
        b["meta"] = max(0.0, min(1.0, 0.5 + trend * 0.1))
        if pressure:
            from ..clash.battles import ANSWER_ROLES
            needed = [ANSWER_ROLES[r] for r in pressure if r in ANSWER_ROLES]
            b["matchup"] = sum(1 for r in needed if _has(deck, r)) / len(needed) if needed else 0.5
        else:
            b["matchup"] = 0.5
        # category role preference bonus folded into offense/defense
        pref = sum(1 for r in cat.prefer_roles if _has(deck, r)) / len(cat.prefer_roles) if cat.prefer_roles else 0
        arch = sum(1 for c in deck for a in archetypes_of(c.name) if a in cat.prefer_archetypes)
        w = self.category_weights(category, custom)
        score = sum(w[k] * b[k] for k in FACTORS) + 0.04 * pref + 0.02 * min(arch, 2)
        penalties = 0.0
        if wc == 0:
            penalties += 0.15
        if air < 2:
            penalties += 0.08 * (2 - air)
        if small + big == 0:
            penalties += 0.08
        # Incoherent win-condition mixes: siege + any other win condition, or two heavy (>=6 elixir) tanks.
        wc_cards = [c for c in deck if "win_condition" in c.roles]
        siege = [c for c in wc_cards if "siege" in archetypes_of(c.name)]
        if siege and len(wc_cards) > len(siege):
            penalties += 0.10
        if sum(1 for c in deck if "tank" in c.roles and (c.elixir or 0) >= 6) >= 2:
            penalties += 0.10
        score = max(0.0, score - penalties)
        return round(score * 100, 2), {k: round(v, 3) for k, v in b.items()}

    @staticmethod
    def avg_elixir(deck: list[DeckCard]) -> float:
        vals = [c.elixir for c in deck if c.elixir is not None]
        return round(sum(vals) / len(vals), 2) if vals else 0.0

    @staticmethod
    def cycle_cost(deck: list[DeckCard]) -> int:
        vals = sorted(c.elixir for c in deck if c.elixir is not None)
        return sum(vals[:4])

    # ------------------------------------------------------------------ generation
    def generate(self, collection: list[DeckCard], category: str, gmax: int, *, meta_trend: dict[str, int] | None = None,
                 pressure: dict[str, int] | None = None, custom: dict[str, float] | None = None, top_n: int = 3,
                 must_include: list[int] | None = None, exclude: list[int] | None = None) -> list[DeckResult]:
        if category not in CATEGORIES:
            raise ValueError(f"unknown category {category}")
        pool = [c for c in collection if c.elixir is not None and c.id not in set(exclude or [])]
        if len(pool) < DECK_SIZE:
            return []
        must = [c for c in pool if c.id in set(must_include or [])][:DECK_SIZE]
        wcs = [c for c in pool if "win_condition" in c.roles]
        wcs.sort(key=lambda c: (-(c.level + (1 if c.evolved else 0) + (meta_trend or {}).get(c.key, 0) * 0.5), c.id))
        seeds = wcs[:8] or sorted(pool, key=lambda c: (-c.level, c.id))[:3]
        candidates: dict[frozenset, tuple[float, list[DeckCard], dict]] = {}
        ev = lambda d: self.evaluate(d, category, gmax, meta_trend, pressure, custom)  # noqa: E731
        for seed in seeds:
            deck = list({c.id: c for c in must + [seed]}.values())
            deck = self._greedy_fill(deck, pool, ev)
            deck = self._improve(deck, pool, ev, locked={c.id for c in must})
            s, br = ev(deck)
            sig = frozenset(c.id for c in deck)
            if sig not in candidates or candidates[sig][0] < s:
                candidates[sig] = (s, deck, br)
        ranked = sorted(candidates.values(), key=lambda x: (-x[0], sorted(c.id for c in x[1])))
        results, seen_wc = [], []
        for s, deck, br in ranked:
            # diversity: prefer decks with a different primary win condition
            wc_ids = frozenset(c.id for c in deck if "win_condition" in c.roles)
            if wc_ids in seen_wc and len(results) < top_n and len(ranked) > top_n:
                continue
            seen_wc.append(wc_ids)
            results.append(self._describe(deck, category, s, br, pool, gmax, meta_trend))
            if len(results) >= top_n:
                break
        for i, r in enumerate(results, 1):
            r.rank = i
        return results

    def _greedy_fill(self, deck: list[DeckCard], pool: list[DeckCard], ev) -> list[DeckCard]:
        deck = list(deck)
        while len(deck) < DECK_SIZE:
            best, best_s = None, -1.0
            ids = {c.id for c in deck}
            n_wc = _has(deck, "win_condition")
            for c in pool:
                if c.id in ids or (n_wc >= 2 and "win_condition" in c.roles and "tank" not in c.roles):
                    continue
                trial = deck + [c]
                padded = trial + self._pad(trial, pool)
                s, _ = ev(padded)
                if s > best_s or (s == best_s and best is not None and c.id < best.id):
                    best, best_s = c, s
            if best is None:
                break
            deck.append(best)
        return deck

    @staticmethod
    def _pad(deck: list[DeckCard], pool: list[DeckCard]) -> list[DeckCard]:
        """Neutral filler so partial decks can be evaluated (lowest-impact, highest-level cards)."""
        ids = {c.id for c in deck}
        need = DECK_SIZE - len(deck)
        filler = [c for c in sorted(pool, key=lambda c: (-c.level, c.id)) if c.id not in ids and not c.roles][:need]
        if len(filler) < need:
            filler += [c for c in sorted(pool, key=lambda c: (-c.level, c.id))
                       if c.id not in ids and c not in filler][: need - len(filler)]
        return filler

    def _improve(self, deck: list[DeckCard], pool: list[DeckCard], ev, locked: set[int], rounds: int = 3):
        best_s, _ = ev(deck)
        for _ in range(rounds):
            improved = False
            for i in range(len(deck)):
                if deck[i].id in locked:
                    continue
                ids = {c.id for c in deck}
                for c in pool:
                    if c.id in ids:
                        continue
                    trial = deck[:i] + [c] + deck[i + 1:]
                    s, _ = ev(trial)
                    if s > best_s + 1e-9:
                        deck, best_s, improved = trial, s, True
                        ids = {x.id for x in deck}
            if not improved:
                break
        return deck

    # ------------------------------------------------------------------ explanation
    def _describe(self, deck: list[DeckCard], category: str, score: float, br: dict, pool: list[DeckCard], gmax: int,
                  meta_trend: dict[str, int] | None) -> DeckResult:
        strengths, weaknesses, uncertainty = [], [], []
        labels = {"level": "card_levels", "synergy": "synergy", "air_defense": "air_defense",
                  "ground_defense": "ground_defense", "spells": "spell_coverage", "elixir": "elixir_curve",
                  "evolution": "evolutions", "offense": "offensive_pressure", "defense": "defensive_coverage",
                  "meta": "meta_fit", "win_condition": "win_condition"}
        for k, lbl in labels.items():
            v = br.get(k, 0)
            if v >= 0.8:
                strengths.append(lbl)
            elif v < 0.45:
                weaknesses.append(lbl)
        if _has(deck, "building") == 0 and _has(deck, "anti_tank_dps") < 1:
            weaknesses.append("weak_vs_tanks")
        if _has(deck, "splash") == 0:
            weaknesses.append("weak_vs_swarms")
        if sum(1 for c in deck if "flying" in c.roles or "air_defense" in c.roles) < 2:
            weaknesses.append("weak_vs_air")
        for c in deck:
            if not c.curated:
                uncertainty.append(f"roles_inferred:{c.name}")
        evo = [c.id for c in sorted((c for c in deck if c.evolved), key=lambda c: -c.level)][: self.evolution_slots]
        subs = self.substitutions(deck, pool)
        archs = [a for c in deck if "win_condition" in c.roles for a in archetypes_of(c.name)]
        return DeckResult(category=category, cards=deck, evolution_ids=evo, avg_elixir=self.avg_elixir(deck),
                          cycle_cost=self.cycle_cost(deck), score=score, breakdown=br, strengths=strengths,
                          weaknesses=sorted(set(weaknesses)), substitutions=subs,
                          archetype=archs[0] if archs else None, share_link=build_share_link([c.id for c in deck]),
                          uncertainty=uncertainty)

    @staticmethod
    def substitutions(deck: list[DeckCard], pool: list[DeckCard], per_card: int = 1) -> list[dict]:
        ids = {c.id for c in deck}
        out = []
        for c in deck:
            if not c.roles:
                continue
            alts = [p for p in pool if p.id not in ids and (p.roles & c.roles) and p.elixir is not None
                    and c.elixir is not None and abs(p.elixir - c.elixir) <= 1]
            alts.sort(key=lambda p: (-len(p.roles & c.roles), -p.level, p.id))
            for a in alts[:per_card]:
                out.append({"replace": c.name, "with": a.name, "level": a.level,
                            "shared_roles": sorted(a.roles & c.roles)})
        return out

    def compare(self, decks: list[DeckResult]) -> dict:
        if not decks:
            return {}
        rows = []
        for d in decks:
            rows.append({"rank": d.rank, "score": d.score, "avg_elixir": d.avg_elixir,
                         "cards": [c.name for c in d.cards], "best": max(d.breakdown, key=d.breakdown.get),
                         "worst": min(d.breakdown, key=d.breakdown.get)})
        factor_winners = {f: max(decks, key=lambda d: d.breakdown.get(f, 0)).rank for f in FACTORS}
        return {"rows": rows, "factor_winners": factor_winners}


def build_share_link(card_ids: list[int]) -> str | None:
    """Official in-game copy link. Only produced for 8 valid official card ids (26/27/28 prefixes, 8 digits)."""
    if len(card_ids) != DECK_SIZE or len(set(card_ids)) != DECK_SIZE:
        return None
    if not all(len(str(i)) == 8 and str(i)[:2] in ("26", "27", "28") for i in card_ids):
        return None
    return SHARE_BASE + ";".join(str(i) for i in card_ids)


def upgrade_plan(decks: list[DeckResult], gmax: int, limit: int = 6) -> list[dict]:
    """Cards that appear in recommended decks and are below max, weighted by deck rank."""
    score: dict[int, float] = {}
    info: dict[int, DeckCard] = {}
    for d in decks:
        for c in d.cards:
            if c.level < gmax:
                score[c.id] = score.get(c.id, 0) + (1 / d.rank) * (1 + (gmax - c.level) * 0.1)
                info[c.id] = c
    ranked = sorted(score.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
    return [{"name": info[i].name, "level": info[i].level, "target": info[i].level + 1, "priority": round(s, 2)}
            for i, s in ranked]


def missing_roles(collection: list[DeckCard]) -> list[str]:
    needed = ["win_condition", "air_defense", "splash", "small_spell", "big_spell", "building", "anti_tank_dps"]
    return [r for r in needed if not any(r in c.roles for c in collection)]
