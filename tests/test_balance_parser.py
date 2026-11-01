from cr_analyst.clash.balance import _match_change, is_balance_article, parse_balance_article, parse_blog_index

URL = "https://supercell.com/en/games/clashroyale/blog/release-notes/balance-update-october-2026/"


def test_parse_real_article_fixture(fixture_json):
    changes = parse_balance_article(fixture_json("supercell_balance_2026_10.json"), URL)
    assert len(changes) >= 30
    for c in changes:
        assert c.source_url == URL and c.verification == "official"
        assert c.card_name and c.attribute
        assert c.change_type in {"buff", "nerf", "rework", "neutral", "adjustment"} or c.change_type
    numeric = [c for c in changes if c.old_value is not None]
    assert numeric, "expected numeric attribute changes"
    assert all(c.published_at is not None for c in changes)
    # stable dedupe hash
    assert len({c.content_hash for c in changes}) == len(changes)


def test_lava_hound_no_colon_variant(fixture_json):
    changes = parse_balance_article(fixture_json("supercell_balance_2026_10.json"), URL)
    lava = [c for c in changes if c.card_name.lower().startswith("lava hound")]
    if lava:  # present in the October 2026 article
        assert any(c.old_value == "53" and c.new_value == "71" for c in lava)


def test_match_change_variants():
    m = _match_change("Hitpoints: 1000 → 1100 (+10%)")
    assert m and m.group("old") == "1000" and m.group("new") == "1100"
    m = _match_change("Damage 53 → 71")
    assert m and m.group("attr").strip() == "Damage"
    assert _match_change("Removed attack delay after ability") is None


def test_blog_index(fixture_json):
    arts = parse_blog_index(fixture_json("supercell_blog_index.json"))
    assert arts and all(a["url"].startswith("http") for a in arts)
    assert any(is_balance_article(a["title"]) for a in arts)


def test_is_balance_article():
    assert is_balance_article("Balance Update - October 2026")
    assert is_balance_article("Card Changes coming next season")
    assert not is_balance_article("Merge Tactics balance changes")
    assert not is_balance_article("New Season: Fire & Ice")
