import pytest

from cr_analyst.clash.cards import parse_collection
from cr_analyst.decks.engine import (CATEGORIES, DECK_SIZE, DeckEngine, build_share_link, load_weights,
                                     missing_roles, to_deck_cards, upgrade_plan)


@pytest.fixture
def pool(player_payload):
    cards, _ = parse_collection(player_payload)
    return to_deck_cards([c.to_dict() for c in cards], {})


def test_eight_categories_defined():
    assert len(CATEGORIES) == 8
    assert set(CATEGORIES) == {"defensive", "offensive", "balanced", "cycle", "counter_attack", "high_level",
                               "meta", "custom"}


@pytest.mark.parametrize("category", sorted(CATEGORIES))
def test_generate_every_category_only_owned_cards(pool, category):
    owned = {c.id for c in pool}
    decks = DeckEngine().generate(pool, category, 16, top_n=3)
    assert decks, f"no deck for {category}"
    for rank, d in enumerate(decks, 1):
        ids = [c.id for c in d.cards]
        assert len(ids) == DECK_SIZE and len(set(ids)) == DECK_SIZE
        assert set(ids) <= owned
        assert 0 <= d.score <= 100
        assert d.rank == rank
        assert any("win_condition" in c.roles for c in d.cards)
        assert d.avg_elixir == pytest.approx(sum(c.elixir for c in d.cards) / 8, abs=0.01)
        if d.share_link:
            assert d.share_link.startswith("https://link.clashroyale.com/deck/")
    scores = [d.score for d in decks]
    assert scores == sorted(scores, reverse=True)


def test_generation_is_deterministic(pool):
    a = [c.id for c in DeckEngine().generate(pool, "balanced", 16)[0].cards]
    b = [c.id for c in DeckEngine().generate(pool, "balanced", 16)[0].cards]
    assert a == b


def test_cycle_category_is_cheaper_than_defensive(pool):
    eng = DeckEngine()
    assert eng.generate(pool, "cycle", 16)[0].avg_elixir <= eng.generate(pool, "defensive", 16)[0].avg_elixir


def test_must_include_and_exclude(pool):
    log_id, rocket_id = 28000011, 28000003
    d = DeckEngine().generate(pool, "custom", 16, must_include=[log_id], exclude=[rocket_id])[0]
    ids = {c.id for c in d.cards}
    assert log_id in ids and rocket_id not in ids


def test_insufficient_cards_returns_empty(pool):
    assert DeckEngine().generate(pool[:5], "balanced", 16) == []
    with pytest.raises(ValueError):
        DeckEngine().generate(pool, "nonsense", 16)


def test_share_link_validation():
    ids = [26000021, 26000000, 26000001, 26000010, 27000000, 28000000, 28000008, 26000030]
    link = build_share_link(ids)
    assert link and link.endswith(";".join(map(str, ids)))
    assert build_share_link(ids[:7]) is None
    assert build_share_link(ids[:7] + [ids[0]]) is None           # duplicate
    assert build_share_link(ids[:7] + [159000000]) is None        # tower troop is not a deck card


def test_weights_normalised():
    w = load_weights(overrides={"level": 5})
    assert sum(w.values()) == pytest.approx(1.0)
    assert w["level"] == max(w.values())


def test_upgrade_plan_and_missing_roles(pool):
    decks = DeckEngine().generate(pool, "balanced", 16)
    plan = upgrade_plan(decks, 16)
    assert all(p["target"] == p["level"] + 1 and p["level"] < 16 for p in plan)
    assert isinstance(missing_roles(pool), list)


def test_compare(pool):
    eng = DeckEngine()
    cmp = eng.compare(eng.generate(pool, "balanced", 16, top_n=3))
    assert cmp["rows"] and set(cmp["factor_winners"]) 
