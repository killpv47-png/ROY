"""Shared fixtures. No test touches the network or real secrets."""
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
FIX = Path(__file__).resolve().parent / "fixtures"

# Make sure a developer's real .env never leaks into tests.
for _k in ("BOT_TOKEN", "LLM_API_KEY", "TAVILY_KEY", "TAVILY_API_KEY", "CLASH_API_TOKEN", "GH_PAT", "ADMIN_ID",
           "SEARCH_API_KEY"):
    os.environ[_k] = ""


def load_fixture(name: str):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


@pytest.fixture
def fixture_json():
    return load_fixture


@pytest.fixture
async def db(tmp_path):
    from cr_analyst.db.session import Database
    url = os.getenv("TEST_DATABASE_URL") or f"sqlite+aiosqlite:///{tmp_path}/test.db"
    d = Database(url)
    if not url.startswith("sqlite"):
        from cr_analyst.db.models import Base
        async with d.engine.begin() as c:
            await c.run_sync(Base.metadata.drop_all)
    await d.create_all()
    yield d
    await d.dispose()


def make_settings(tmp_path, **kw):
    from cr_analyst.config import Settings
    base = dict(BOT_TOKEN="", DATABASE_URL=f"sqlite+aiosqlite:///{tmp_path}/svc.db",
                APP_ENCRYPTION_KEY="unit-test-passphrase-123456", LLM_API_KEY="", TAVILY_KEY="",
                WORKSPACE_DIR=str(tmp_path / "ws"), CACHE_DIR=str(tmp_path / "cache"),
                SKILLS_DIR=str(ROOT / "skills"), RESUME_DELAY_SECONDS=0)
    base.update(kw)
    return Settings(_env_file=None, **base)


@pytest.fixture
def settings_factory(tmp_path):
    return lambda **kw: make_settings(tmp_path, **kw)


@pytest.fixture
async def services(tmp_path):
    from cr_analyst.app import Services
    svc = Services(make_settings(tmp_path))
    await svc.init()
    yield svc
    await svc.close()


def raw_card(cid, name, level, max_level, elixir, rarity=None, evo=None, max_evo=None):
    d = {"id": cid, "name": name, "level": level, "maxLevel": max_level, "elixirCost": elixir,
         "iconUrls": {"medium": f"https://example.invalid/{cid}.png"}}
    if rarity:
        d["rarity"] = rarity
    if evo is not None:
        d["evolutionLevel"] = evo
    if max_evo is not None:
        d["maxEvolutionLevel"] = max_evo
    return d


# A realistic owned collection (official ids) used by deck / battle tests.
COLLECTION = [
    raw_card(26000021, "Hog Rider", 11, 14, 4, "rare"),
    raw_card(26000003, "Giant", 14, 16, 5, "common"),
    raw_card(26000000, "Knight", 15, 16, 3, "common", evo=1, max_evo=1),
    raw_card(26000001, "Archers", 14, 16, 3, "common", max_evo=1),
    raw_card(26000005, "Minions", 14, 16, 3, "common"),
    raw_card(26000010, "Skeletons", 15, 16, 1, "common"),
    raw_card(26000008, "Barbarians", 13, 16, 5, "common"),
    raw_card(26000014, "Musketeer", 12, 14, 4, "rare"),
    raw_card(26000011, "Valkyrie", 12, 14, 4, "rare"),
    raw_card(26000017, "Wizard", 11, 14, 5, "rare"),
    raw_card(26000012, "Skeleton Army", 8, 11, 3, "epic"),
    raw_card(26000015, "Baby Dragon", 8, 11, 4, "epic"),
    raw_card(26000013, "Bomber", 14, 16, 2, "common"),
    raw_card(26000018, "Mini P.E.K.K.A", 12, 14, 4, "rare"),
    raw_card(26000024, "Royal Giant", 14, 16, 6, "common"),
    raw_card(26000030, "Ice Spirit", 14, 16, 1, "common"),
    raw_card(26000036, "Balloon", 8, 11, 5, "epic"),
    raw_card(27000000, "Cannon", 14, 16, 3, "common"),
    raw_card(27000006, "Inferno Tower", 11, 14, 5, "rare"),
    raw_card(27000003, "Tesla", 14, 16, 4, "common"),
    raw_card(28000000, "Fireball", 12, 14, 4, "rare"),
    raw_card(28000008, "Zap", 15, 16, 2, "common"),
    raw_card(28000001, "Arrows", 14, 16, 3, "common"),
    raw_card(28000011, "The Log", 5, 8, 2, "legendary"),
    raw_card(28000009, "Poison", 8, 11, 4, "epic"),
    raw_card(28000004, "Goblin Barrel", 8, 11, 3, "epic"),
    raw_card(28000003, "Rocket", 11, 14, 6, "rare"),
]


@pytest.fixture
def player_payload():
    return {"tag": "#2PP", "name": "Tester", "expLevel": 50, "trophies": 7000, "bestTrophies": 7500,
            "wins": 1000, "losses": 900, "cards": [dict(c) for c in COLLECTION],
            "supportCards": [{"id": 159000000, "name": "Tower Princess", "level": 11, "maxLevel": 16}],
            "currentDeck": [dict(c) for c in COLLECTION[:8]], "brandNewField": {"x": 1}}


@pytest.fixture
async def udb(db):
    """Database with two real users (ids 1 and 2) so foreign keys are satisfied."""
    from cr_analyst.db.models import User
    async with db.session() as s:
        s.add_all([User(id=1, telegram_id=900001, language="en", role="user", settings={}),
                   User(id=2, telegram_id=900002, language="en", role="user", settings={})])
    return db
