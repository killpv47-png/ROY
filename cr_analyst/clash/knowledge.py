"""Curated *qualitative* tactical knowledge (roles, archetypes, synergy pairs).

These are strategic classifications used by the deterministic deck engine — NOT numeric stats.
Numeric stats only come from the versioned `card_stat_versions` table (official / secondary sources).
Cards missing from this table are classified by `infer_roles()` from verifiable catalog data
(type, elixir, targets) and are flagged `inferred` so the analysis can disclose the uncertainty.
"""
from __future__ import annotations

import re

ROLE_NAMES = {
    "W": "win_condition", "T": "tank", "M": "mini_tank", "A": "air_defense", "S": "splash",
    "D": "anti_tank_dps", "R": "swarm", "B": "building", "s": "small_spell", "G": "big_spell",
    "F": "flying", "C": "control", "P": "spawner", "U": "support_buff",
}

_TABLE = """
knight:M archers:A goblins:R giant:WT pekka:TD minions:FAR balloon:WF witch:SAP barbarians:RD golem:WT
skeletons:R valkyrie:SM skeleton-army:RD bomber:S musketeer:A baby-dragon:FAS prince:D wizard:AS
mini-pekka:D spear-goblins:AR giant-skeleton:TS hog-rider:W minion-horde:FAR ice-wizard:ASC
royal-giant:WT guards:RM princess:AS dark-prince:SM three-musketeers:WA lava-hound:WTF ice-spirit:C
fire-spirit:SA miner:WM sparky:SD bowler:SM lumberjack:D battle-ram:W inferno-dragon:FAD ice-golem:MC
mega-minion:FAD dart-goblin:A goblin-gang:RA electro-wizard:AC elite-barbarians:D hunter:AD executioner:AS
bandit:MD royal-recruits:RM night-witch:APD bats:FAR royal-ghost:SM ram-rider:WC zappies:ACR rascals:ARM
cannon-cart:D mega-knight:STD skeleton-barrel:WFR flying-machine:FA wall-breakers:WR royal-hogs:WR
goblin-giant:WTA fisherman:CM magic-archer:AS electro-dragon:FASC firecracker:AS mighty-miner:DM
elixir-golem:WT battle-healer:M skeleton-king:SPT archer-queen:AD golden-knight:DM monk:MC
skeleton-dragons:FAS mother-witch:AP electro-spirit:CA electro-giant:WTC phoenix:FAS little-prince:AD
goblin-demolisher:S goblin-machine:MD suspicious-bush:W goblinstein:WT rune-giant:TU berserker:DM
boss-bandit:DM minion-giant:WFT heal-spirit:U
cannon:BD goblin-hut:PB mortar:WBS inferno-tower:BDA bomb-tower:BS barbarian-hut:PB tesla:BAD
elixir-collector:B x-bow:WB tombstone:BP furnace:BPSA goblin-cage:BM goblin-drill:WB
fireball:GSA arrows:sAS rage:U rocket:G goblin-barrel:W freeze:CG mirror:U lightning:GC zap:sCA
poison:GSA graveyard:W the-log:sS tornado:CAS clone:U earthquake:G barbarian-barrel:sS
giant-snowball:sCA royal-delivery:sS void:GA goblin-curse:sS vines:C
"""

CARD_ROLES: dict[str, set[str]] = {}
for _tok in _TABLE.split():
    _k, _r = _tok.split(":")
    CARD_ROLES[_k] = {ROLE_NAMES[c] for c in _r}

# Win-condition archetypes (qualitative strategy families)
ARCHETYPE = {
    "beatdown": {"golem", "giant", "lava-hound", "electro-giant", "elixir-golem", "goblin-giant", "royal-giant",
                 "goblinstein", "minion-giant", "pekka", "mega-knight"},
    "siege": {"x-bow", "mortar"},
    "cycle": {"hog-rider", "ram-rider", "wall-breakers", "miner", "goblin-drill", "royal-hogs", "skeleton-barrel",
              "suspicious-bush"},
    "bait": {"goblin-barrel", "princess", "goblin-gang", "dart-goblin", "skeleton-barrel"},
    "bridge_spam": {"battle-ram", "bandit", "royal-ghost", "ram-rider", "pekka", "elite-barbarians"},
    "air": {"balloon", "lava-hound", "minion-giant"},
    "graveyard": {"graveyard"},
    "spell_heavy": {"three-musketeers", "sparky"},
}

# Widely documented synergy pairs (qualitative). Symmetric.
SYNERGY_PAIRS = [
    ("hog-rider", "ice-spirit"), ("hog-rider", "earthquake"), ("hog-rider", "the-log"), ("hog-rider", "musketeer"),
    ("golem", "night-witch"), ("golem", "lumberjack"), ("golem", "baby-dragon"), ("golem", "tornado"),
    ("lava-hound", "balloon"), ("lava-hound", "miner"), ("lava-hound", "skeleton-dragons"),
    ("lava-hound", "inferno-dragon"), ("miner", "poison"), ("miner", "wall-breakers"), ("miner", "bats"),
    ("x-bow", "tesla"), ("x-bow", "archers"), ("mortar", "cannon-cart"), ("graveyard", "poison"),
    ("graveyard", "freeze"), ("graveyard", "baby-dragon"), ("royal-giant", "fisherman"), ("royal-giant", "lightning"),
    ("pekka", "battle-ram"), ("pekka", "electro-wizard"), ("pekka", "bandit"), ("goblin-barrel", "princess"),
    ("goblin-barrel", "the-log"), ("goblin-barrel", "goblin-gang"), ("balloon", "freeze"), ("balloon", "lumberjack"),
    ("electro-giant", "tornado"), ("electro-giant", "lightning"), ("three-musketeers", "battle-ram"),
    ("three-musketeers", "elixir-collector"), ("royal-hogs", "earthquake"), ("royal-hogs", "royal-recruits"),
    ("elixir-golem", "battle-healer"), ("giant", "sparky"), ("giant", "mini-pekka"), ("giant", "musketeer"),
    ("goblin-giant", "sparky"), ("ram-rider", "bandit"), ("wall-breakers", "goblin-drill"),
    ("skeleton-barrel", "goblin-barrel"), ("minion-giant", "rune-giant"), ("minion-giant", "little-prince"),
    ("rune-giant", "little-prince"), ("mega-knight", "inferno-dragon"), ("goblin-drill", "bomber"),
    ("ice-golem", "hog-rider"), ("tornado", "executioner"), ("tornado", "ice-wizard"), ("cannon", "the-log"),
]
_SYN = {frozenset(p) for p in SYNERGY_PAIRS}

TOWER_TROOPS = {"tower-princess", "cannoneer", "dagger-duchess", "royal-chef"}


def card_key(name: str) -> str:
    s = name.lower().replace(".", "").replace("'", "").replace("’", "")
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s


def infer_roles(card_type: str | None, elixir: int | None, stats: dict | None = None) -> set[str]:
    roles: set[str] = set()
    t = (card_type or "").lower()
    if t == "spell":
        roles.add("small_spell" if (elixir or 0) <= 3 else "big_spell")
    elif t == "building":
        roles.add("building")
    elif t == "troop":
        if stats:
            if stats.get("attacks_air"):
                roles.add("air_defense")
            if stats.get("area_damage_radius"):
                roles.add("splash")
            if stats.get("flying_height") or stats.get("is_flying"):
                roles.add("flying")
        if (elixir or 0) >= 6:
            roles.add("tank")
    return roles


def roles_for(name: str, card_type: str | None = None, elixir: int | None = None,
              stats: dict | None = None) -> tuple[set[str], bool]:
    """Return (roles, curated?)."""
    k = card_key(name)
    if k in CARD_ROLES:
        r = set(CARD_ROLES[k])
        curated = True
    else:
        r = infer_roles(card_type, elixir, stats)
        curated = False
    if elixir is not None and elixir <= 2 and card_type and card_type.lower() == "troop":
        r.add("cycle")
    return r, curated


def synergy(a: str, b: str) -> bool:
    return frozenset((card_key(a), card_key(b))) in _SYN


def archetypes_of(name: str) -> list[str]:
    k = card_key(name)
    return [a for a, members in ARCHETYPE.items() if k in members]
