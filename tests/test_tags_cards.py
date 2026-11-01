import pytest

from cr_analyst.clash.cards import card_type_from_id, global_max_level, parse_card, parse_collection
from cr_analyst.clash.tags import InvalidTag, encode_tag, is_valid_tag, normalize_tag


@pytest.mark.parametrize("raw,expected", [
    ("#2PP", "#2PP"), ("2pp", "#2PP"), ("  #2pp ", "#2PP"), ("#2PO", "#2P0"), ("%232PP", "#2PP"),
    ("https://royaleapi.com/player/2PPGQ8", "#2PPGQ8"),
])
def test_normalize_tag(raw, expected):
    assert normalize_tag(raw) == expected


@pytest.mark.parametrize("raw", ["", "#", "#AB", "#2PP!", "#XYZ123", None, "#" + "2" * 20])
def test_invalid_tags(raw):
    with pytest.raises(InvalidTag):
        normalize_tag(raw)
    if raw is not None:
        assert not is_valid_tag(raw)


def test_encode_tag_url_safe():
    assert encode_tag("#2PP") == "%232PP"
    assert encode_tag("2pp") == "%232PP"


def test_card_type_from_id():
    assert card_type_from_id(26000000) == "Troop"
    assert card_type_from_id(27000000) == "Building"
    assert card_type_from_id(28000000) == "Spell"
    assert card_type_from_id(159000000) == "Tower"
    assert card_type_from_id(12345) == "Unknown"


def test_level_normalisation_rarity_relative():
    # API levels are rarity-relative: display = level + (globalMax - maxLevel)
    assert parse_card({"id": 28000011, "name": "The Log", "level": 5, "maxLevel": 8}, 16).level == 13
    assert parse_card({"id": 26000021, "name": "Hog Rider", "level": 11, "maxLevel": 14}, 16).level == 13
    assert parse_card({"id": 26000000, "name": "Knight", "level": 16, "maxLevel": 16}, 16).level == 16
    c = parse_card({"id": 26000012, "name": "Skeleton Army", "level": 8, "maxLevel": 11}, 16)
    assert c.level == 13 and c.raw_level == 8 and c.rarity == "epic"


def test_global_max_level():
    assert global_max_level([{"maxLevel": 14}, {"maxLevel": 16}]) == 16
    assert global_max_level([]) == 16
    assert global_max_level([{"maxLevel": 14}], catalog_max=15) == 15


def test_parse_collection_keeps_unknown_fields_and_evolutions(player_payload):
    player_payload["cards"][0]["futureField"] = 42
    cards, support = parse_collection(player_payload)
    assert len(cards) == len(player_payload["cards"])
    assert cards[0].extra == {"futureField": 42}
    knight = next(c for c in cards if c.name == "Knight")
    assert knight.is_evolved and knight.can_evolve
    archers = next(c for c in cards if c.name == "Archers")
    assert not archers.is_evolved and archers.can_evolve
    assert len(support) == 1 and support[0].card_type == "Tower" and support[0].is_support
    assert support[0].level == 11  # support items are not re-normalised
