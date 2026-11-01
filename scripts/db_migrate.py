"""Bring the database schema to Alembic head.

* fresh database                      -> alembic upgrade head
* database created earlier by init-db  -> alembic stamp head (tables already exist, no alembic_version)
* already managed by Alembic           -> alembic upgrade head
Exit code 0 on success. Prints the action taken. Never prints DATABASE_URL.
"""
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from sqlalchemy import inspect  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from cr_analyst.db.session import normalize_url  # noqa: E402


async def _tables(url: str) -> list[str]:
    if url.startswith("sqlite"):
        path = url.split("///", 1)[-1]
        if path and not path.startswith(":"):
            Path(path).parent.mkdir(parents=True, exist_ok=True)
    eng = create_async_engine(url)
    try:
        async with eng.connect() as c:
            return await c.run_sync(lambda sc: inspect(sc).get_table_names())
    finally:
        await eng.dispose()


def decide(tables: list[str]) -> str:
    if "alembic_version" in tables:
        return "upgrade"
    if "users" in tables:
        return "stamp"
    return "upgrade"


def main() -> int:
    url = normalize_url(os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./data/dev.db"))
    action = decide(asyncio.run(_tables(url)))
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    if action == "stamp":
        command.stamp(cfg, "head")
    else:
        command.upgrade(cfg, "head")
    print(f"db_migrate: {action} -> head")
    return 0


if __name__ == "__main__":
    sys.exit(main())
