"""Alembic environment (async). DATABASE_URL is normalised exactly like the application does."""
import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from cr_analyst.db.models import Base
from cr_analyst.db.session import normalize_url

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)
target_metadata = Base.metadata


def _url() -> str:
    return normalize_url(os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./data/dev.db"))


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True,
                      render_as_batch=_url().startswith("sqlite"))
    with context.begin_transaction():
        context.run_migrations()


def _do_run(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata,
                      render_as_batch=connection.dialect.name == "sqlite", compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    url = _url()
    if url.startswith("sqlite"):
        path = url.split("///", 1)[-1]
        if path and not path.startswith(":"):
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    engine = create_async_engine(url)
    async with engine.connect() as conn:
        await conn.run_sync(_do_run)
        await conn.commit()
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
