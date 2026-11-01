"""Battle-log ingestion and deterministic battle analysis.

The official battle log exposes only the most recent ~25 battles and NO in-game placement / timing data.
We persist every observed battle so long-term history grows from the moment tracking starts — never before.
"""
from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any

from .cards import parse_deck_cards
from .knowledge import card_key, roles_for


def parse_battle_time(s: str | None) -> datetime | None:
    """Official format: 20261101T174512.000Z"""
    if not s:
        return None
    for fmt in ("%Y%m%dT%H%M%S.%fZ", "%Y%m%dT%H%M%SZ"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def battle_key(b: dict) -> str:
    team = (b.get("team") or [{}])[0]
    opp = (b.get("opponent") or [{}])[0]
    raw = f"{b.get('battleTime')}|{team.get('tag')}|{opp.get('tag')}|{b.get('type')}"
    return hashlib.sha1(raw.encode()).hexdigest()


def normalize_battle(b: dict, catalog: dict[int, dict] | None = None) -> dict:
    team = b.get("team") or [{}]
    opp = b.get("opponent") or [{}]
    me, them = team[0], opp[0]
    # In 2v2 both teammates report the team's crowns; take the first player's value.
    my_crowns = int(me.get("crowns") or 0)
    op_crowns = int(them.get("crowns") or 0)
    result = "win" if my_crowns > op_crowns else "loss" if my_crowns < op_crowns else "draw"
    return {
        "battle_key": battle_key(b),
        "battle_time": parse_battle_time(b.get("battleTime")),
        "battle_type": b.get("type"),
        "game_mode": (b.get("gameMode") or {}).get("name"),
        "result": result,
        "crowns": my_crowns,
        "opponent_crowns": op_crowns,
        "trophy_change": me.get("trophyChange"),
        "opponent_tag": them.get("tag"),
        "opponent_name": them.get("name"),
        "player_deck": parse_deck_cards(me.get("cards") or [], catalog),
        "opponent_deck": parse_deck_cards(them.get("cards") or [], catalog),
        "arena": (b.get("arena") or {}).get("name"),
        "elixir_leaked": me.get("elixirLeaked"),
        "king_tower_hp": me.get("kingTowerHitPoints"),
        "team_size": len(team),
    }


def _deck_sig(deck: list[dict]) -> str:
    return ",".join(sorted(str(c["id"]) for c in deck))


def analyze_battles(battles: list[dict], *, source: str = "api") -> dict[str, Any]:
    """`battles` are normalized dicts (from API or DB). Returns deterministic statistics."""
    n = len(battles)
    if n == 0:
        return {"sample_size": 0, "source": source, "note": "no_battles"}
    res = Counter(b["result"] for b in battles)
    by_mode: dict[str, Counter] = defaultdict(Counter)
    for b in battles:
        by_mode[b.get("game_mode") or "unknown"][b["result"]] += 1

    opp_seen: Counter = Counter()
    opp_in_losses: Counter = Counter()
    names: dict[int, str] = {}
    for b in battles:
        for c in b.get("opponent_deck") or []:
            names[c["id"]] = c["name"]
            opp_seen[c["id"]] += 1
            if b["result"] == "loss":
                opp_in_losses[c["id"]] += 1
    losses = res.get("loss", 0)
    base = losses / n
    problem_cards = []
    for cid, seen in opp_seen.items():
        if seen < 2 or opp_in_losses[cid] < 2:
            continue
        lr = opp_in_losses[cid] / seen
        if lr > base:
            problem_cards.append({"id": cid, "name": names[cid], "seen": seen, "losses_with": opp_in_losses[cid],
                                  "loss_rate_with": round(lr * 100, 1), "lift_pct": round((lr - base) * 100, 1)})
    problem_cards.sort(key=lambda x: (-x["lift_pct"], -x["seen"]))

    decks: dict[str, dict] = {}
    for b in battles:
        if not b.get("player_deck"):
            continue
        d = decks.setdefault(_deck_sig(b["player_deck"]),
                             {"cards": [c["name"] for c in b["player_deck"]], "w": 0, "l": 0, "d": 0})
        d[{"win": "w", "loss": "l", "draw": "d"}[b["result"]]] += 1
    deck_perf = sorted(({**v, "games": v["w"] + v["l"] + v["d"],
                         "win_rate": round(v["w"] / max(1, v["w"] + v["l"] + v["d"]) * 100, 1)}
                        for v in decks.values()), key=lambda x: -x["games"])

    role_pressure: Counter = Counter()
    for b in battles:
        if b["result"] != "loss":
            continue
        for c in b.get("opponent_deck") or []:
            roles, _ = roles_for(c["name"], None, c.get("elixir"))
            for r in roles & {"win_condition", "flying", "swarm", "splash", "tank", "building"}:
                role_pressure[r] += 1

    diffs = []
    for b in battles:
        if b.get("player_deck") and b.get("opponent_deck"):
            my = sum(c["level"] for c in b["player_deck"]) / len(b["player_deck"])
            op = sum(c["level"] for c in b["opponent_deck"]) / len(b["opponent_deck"])
            diffs.append((my - op, b["result"]))
    level_note = None
    if diffs:
        level_note = {"avg_level_diff": round(sum(d for d, _ in diffs) / len(diffs), 2),
                      "losses_while_underleveled": sum(1 for d, r in diffs if d < -0.5 and r == "loss")}

    epoch = datetime.min.replace(tzinfo=timezone.utc)
    recent = sorted(battles, key=lambda b: b.get("battle_time") or epoch)[-10:]
    trophy = [b.get("trophy_change") for b in battles if isinstance(b.get("trophy_change"), int)]
    times = [b["battle_time"] for b in battles if b.get("battle_time")]
    return {
        "sample_size": n,
        "source": source,
        "period": {"from": min(times).isoformat() if times else None, "to": max(times).isoformat() if times else None},
        "wins": res.get("win", 0), "losses": losses, "draws": res.get("draw", 0),
        "win_rate": round(res.get("win", 0) / n * 100, 1),
        "by_mode": {m: {"games": sum(c.values()), "wins": c.get("win", 0), "losses": c.get("loss", 0),
                        "win_rate": round(c.get("win", 0) / max(1, sum(c.values())) * 100, 1)}
                    for m, c in sorted(by_mode.items(), key=lambda kv: -sum(kv[1].values()))},
        "most_common_opponent_cards": [{"id": cid, "name": names[cid], "seen": s} for cid, s in opp_seen.most_common(10)],
        "problem_cards": problem_cards[:8],
        "deck_performance": deck_perf[:5],
        "loss_role_pressure": dict(role_pressure.most_common(6)),
        "level_differential": level_note,
        "trend": {"last_10_wins": sum(1 for b in recent if b["result"] == "win"), "last_10_games": len(recent)},
        "trophy_net": sum(trophy) if trophy else None,
        "limitations": ["Battle log API returns only recent battles (~25).",
                        "No placement, timing or elixir-usage data is exposed; tactical mistakes cannot be observed."],
    }


ANSWER_ROLES = {"flying": "air_defense", "swarm": "splash", "tank": "anti_tank_dps", "building": "big_spell",
                "win_condition": "building", "splash": "mini_tank"}


def counter_suggestions(analysis: dict, owned_names: set[str]) -> list[dict]:
    """Suggest owned cards that cover the roles most present in losses (qualitative, explained)."""
    out = []
    for role, count in (analysis.get("loss_role_pressure") or {}).items():
        need = ANSWER_ROLES.get(role)
        if not need:
            continue
        cands = sorted(n for n in owned_names if need in roles_for(n)[0])
        if cands:
            out.append({"threat_role": role, "occurrences_in_losses": count, "answer_role": need,
                        "owned_candidates": cands[:5]})
    return out


def card_key_set(deck: list[dict]) -> set[str]:
    return {card_key(c["name"]) for c in deck}
