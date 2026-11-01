"""Async engine / session management with retry on transient DB failures."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from .models import Base

log = logging.getLogger(__name__)


def normalize_url(url: str) -> str:
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url[len("postgresql://"):]
    if url.startswith("postgresql+asyncpg://") and "sslmode=" in url:
        # asyncpg uses `ssl=` instead of libpq's sslmode
        url = url.replace("sslmode=require", "ssl=require").replace("sslmode=disable", "ssl=disable")
    return url


class Database:
    def __init__(self, url: str, echo: bool = False):
        self.url = normalize_url(url)
        kwargs: dict = {"echo": echo, "pool_pre_ping": True}
        if self.url.startswith("sqlite"):
            path = self.url.split("///", 1)[-1]
            if path and path != ":memory:" and not path.startswith(":"):
                Path(path).parent.mkdir(parents=True, exist_ok=True)
        else:
            kwargs.update(pool_size=5, max_overflow=5, pool_recycle=1800)
        self.engine: AsyncEngine = create_async_engine(self.url, **kwargs)
        if self.url.startswith("sqlite"):
            @event.listens_for(self.engine.sync_engine, "connect")
            def _fk(dbapi_conn, _):  # pragma: no cover - driver hook
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA foreign_keys=ON")
                cur.execute("PRAGMA journal_mode=WAL")
                cur.close()
        self.sessionmaker = async_sessionmaker(self.engine, expire_on_commit=False)

    @property
    def dialect(self) -> str:
        return self.engine.dialect.name

    async def create_all(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def ping(self) -> tuple[bool, str]:
        try:
            async with self.engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return True, self.dialect
        except Exception as e:  # pragma: no cover - depends on infra
            return False, type(e).__name__

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.sessionmaker() as s:
            try:
                yield s
                await s.commit()
            except Exception:
                await s.rollback()
                raise

    async def run_with_retry(self, fn, *, attempts: int = 4, base_delay: float = 0.5):
        """Execute `await fn(session)` in its own transaction; retry transient connection errors."""
        for i in range(attempts):
            try:
                async with self.session() as s:
                    return await fn(s)
            except (OperationalError, DBAPIError, ConnectionError, OSError) as e:
                if isinstance(e, DBAPIError) and not getattr(e, "connection_invalidated", False) \
                        and not isinstance(e, OperationalError):
                    raise
                if i == attempts - 1:
                    raise
                delay = base_delay * (2 ** i)
                log.warning("DB transient error (%s), retry in %.1fs", type(e).__name__, delay)
                await asyncio.sleep(delay)

    async def dispose(self) -> None:
        await self.engine.dispose()
