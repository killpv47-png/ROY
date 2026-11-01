from cr_analyst.clash.battles import analyze_battles, battle_key, counter_suggestions, normalize_battle
from tests.conftest import COLLECTION

HOG = {"id": 26000021, "name": "Hog Rider", "level": 11, "maxLevel": 14}
BALLOON = {"id": 26000036, "name": "Balloon", "level": 8, "maxLevel": 11}
KNIGHT = {"id": 26000000, "name": "Knight", "level": 14, "maxLevel": 16}


def raw_battle(i, my, op, opp_cards, mode="Ladder", trophy=30):
    return {"type": "PvP", "battleTime": f"20261020T1{i:02d}000.000Z", "gameMode": {"name": mode},
            "arena": {"name": "Legendary Arena"},
            "team": [{"tag": "#2PP", "crowns": my, "trophyChange": trophy if my > op else -trophy,
                      "cards": COLLECTION[:8]}],
            "opponent": [{"tag": f"#OPP{i}", "name": f"opp{i}", "crowns": op, "cards": opp_cards}]}


def battles():
    raws = [raw_battle(1, 3, 0, [KNIGHT] * 1), raw_battle(2, 0, 1, [BALLOON, KNIGHT]),
            raw_battle(3, 1, 2, [BALLOON, HOG]), raw_battle(4, 2, 1, [HOG], mode="Path of Legend"),
            raw_battle(5, 0, 3, [BALLOON]), raw_battle(6, 1, 1, [KNIGHT])]
    return [normalize_battle(b) for b in raws]


def test_normalize_battle_result_and_key():
    b = normalize_battle(raw_battle(1, 3, 1, [HOG]))
    assert b["result"] == "win" and b["crowns"] == 3 and b["opponent_crowns"] == 1
    assert b["battle_time"].year == 2026 and len(b["player_deck"]) == 8
    assert normalize_battle(raw_battle(2, 1, 1, [HOG]))["result"] == "draw"
    assert battle_key(raw_battle(1, 3, 1, [HOG])) == battle_key(raw_battle(1, 0, 3, [KNIGHT]))  # same time/tags


def test_analyze_statistics():
    a = analyze_battles(battles())
    assert a["sample_size"] == 6
    assert (a["wins"], a["losses"], a["draws"]) == (2, 3, 1)
    assert a["win_rate"] == round(2 / 6 * 100, 1)
    assert set(a["by_mode"]) == {"Ladder", "Path of Legend"}
    names = [p["name"] for p in a["problem_cards"]]
    assert "Balloon" in names  # appeared in 3 losses out of 3
    assert a["trend"]["last_10_games"] == 6
    assert a["limitations"]


def test_empty_battles():
    assert analyze_battles([])["sample_size"] == 0


def test_counter_suggestions_only_owned_cards():
    a = analyze_battles(battles())
    sugg = counter_suggestions(a, {"Musketeer", "Archers", "Tesla"})
    assert isinstance(sugg, list)
    for s in sugg:
        for name in s.get("cards", []):
            assert name in {"Musketeer", "Archers", "Tesla"}
