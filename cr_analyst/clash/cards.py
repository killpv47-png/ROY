"""Card parsing and level normalisation for official API card objects."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

DEFAULT_GLOBAL_MAX_LEVEL = 16

# Official card id ranges (verified from /cards ids: 26xxxxxx troops, 27xxxxxx buildings, 28xxxxxx spells,
# 159xxxxxx tower troops / support items).
def card_type_from_id(card_id: int) -> str:
    prefix = str(card_id)[:2]
    if prefix == "26":
        return "Troop"
    if prefix == "27":
        return "Building"
    if prefix == "28":
        return "Spell"
    if str(card_id).startswith("159"):
        return "Tower"
    return "Unknown"


RARITY_BY_MAXLEVEL_16 = {16: "common", 14: "rare", 11: "epic", 8: "legendary", 6: "champion"}


@dataclass
class OwnedCard:
    id: int
    name: str
    level: int            # normalised display level (1..global max)
    raw_level: int
    max_level: int        # rarity-relative max from API
    rarity: str | None
    elixir: int | None
    card_type: str
    count: int | None = None
    evolution_level: int | None = None
    max_evolution_level: int | None = None
    star_level: int | None = None
    icon_url: str | None = None
    evo_icon_url: str | None = None
    is_support: bool = False
    roles: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)  # unknown/new fields retained, never dropped silently

    @property
    def is_evolved(self) -> bool:
        return bool(self.evolution_level and self.evolution_level > 0)

    @property
    def can_evolve(self) -> bool:
        return bool(self.max_evolution_level and self.max_evolution_level > 0)

    @property
    def is_champion(self) -> bool:
        return (self.rarity or "").lower() == "champion"

    def to_dict(self) -> dict:
        return asdict(self)


KNOWN_FIELDS = {"name", "id", "level", "maxLevel", "count", "iconUrls", "starLevel", "evolutionLevel",
                "maxEvolutionLevel", "elixirCost", "rarity"}


def global_max_level(cards: list[dict], catalog_max: int | None = None) -> int:
    vals = [c.get("maxLevel") for c in cards if isinstance(c.get("maxLevel"), int)]
    m = max(vals) if vals else 0
    return max(m, catalog_max or 0) or DEFAULT_GLOBAL_MAX_LEVEL


def parse_card(raw: dict, gmax: int, catalog: dict[int, dict] | None = None, *, is_support: bool = False) -> OwnedCard:
    cid = int(raw["id"])
    cat = (catalog or {}).get(cid, {})
    max_level = int(raw.get("maxLevel") or cat.get("max_level") or gmax)
    raw_level = int(raw.get("level") or 1)
    level = raw_level + (gmax - max_level) if not is_support else raw_level
    rarity = (raw.get("rarity") or cat.get("rarity") or RARITY_BY_MAXLEVEL_16.get(max_level + (16 - gmax)) or None)
    icons = raw.get("iconUrls") or {}
    extra = {k: v for k, v in raw.items() if k not in KNOWN_FIELDS}
    return OwnedCard(
        id=cid, name=raw.get("name") or cat.get("name") or str(cid), level=level, raw_level=raw_level,
        max_level=max_level, rarity=(rarity or "").lower() or None,
        elixir=raw.get("elixirCost", cat.get("elixir")),
        card_type="Tower" if is_support else (cat.get("card_type") or card_type_from_id(cid)),
        count=raw.get("count"), evolution_level=raw.get("evolutionLevel"),
        max_evolution_level=raw.get("maxEvolutionLevel", cat.get("max_evolution_level")),
        star_level=raw.get("starLevel"), icon_url=icons.get("medium") or cat.get("icon_url"),
        evo_icon_url=icons.get("evolutionMedium") or cat.get("evo_icon_url"),
        is_support=is_support, roles=list(cat.get("roles") or []), extra=extra,
    )


def parse_collection(player: dict, catalog: dict[int, dict] | None = None) -> tuple[list[OwnedCard], list[OwnedCard]]:
    cards_raw = player.get("cards") or []
    cat_max = max((c.get("max_level") or 0 for c in (catalog or {}).values()), default=0)
    gmax = global_max_level(cards_raw, cat_max)
    cards = [parse_card(c, gmax, catalog) for c in cards_raw]
    support = [parse_card(c, gmax, catalog, is_support=True) for c in (player.get("supportCards") or [])]
    return cards, support


def parse_deck_cards(cards_raw: list[dict], catalog: dict[int, dict] | None = None, gmax: int | None = None) -> list[dict]:
    """Compact deck representation used in battle records."""
    g = gmax or global_max_level(cards_raw, max((c.get("max_level") or 0 for c in (catalog or {}).values()), default=0))
    out = []
    for c in cards_raw:
        oc = parse_card(c, g, catalog)
        out.append({"id": oc.id, "name": oc.name, "level": oc.level, "evo": oc.evolution_level or 0,
                    "elixir": oc.elixir})
    return out
