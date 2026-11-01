"""Single-active-worker lease with expiry and fencing token (works on PostgreSQL and SQLite).

Acquire = atomic conditional UPDATE (expired or same holder) or INSERT of the singleton row. The fencing token
increments on every takeover so a stale worker can detect it lost the lease and stop processing updates.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError

from ..db.models import WorkerLease

log = logging.getLogger(__name__)
LEASE_NAME = "telegram-worker"


class LeaseLost(Exception):
    pass


class WorkerLeaseManager:
    def __init__(self, db, holder: str, ttl_seconds: int = 120, name: str = LEASE_NAME):
        self.db = db
        self.holder = holder
        self.ttl = ttl_seconds
        self.name = name
        self.token: int | None = None

    def _exp(self) -> datetime:
        return datetime.now(timezone.utc) + timedelta(seconds=self.ttl)

    async def acquire(self) -> bool:
        now = datetime.now(timezone.utc)
        async with self.db.session() as s:
            res = await s.execute(
                update(WorkerLease)
                .where(WorkerLease.name == self.name,
                       or_(WorkerLease.expires_at < now, WorkerLease.holder == self.holder))
                .values(holder=self.holder, acquired_at=now, expires_at=self._exp(),
                        fencing_token=WorkerLease.fencing_token + 1))
            if res.rowcount == 1:
                row = await s.get(WorkerLease, self.name)
                await s.refresh(row)
                self.token = row.fencing_token
                return True
        try:
            async with self.db.session() as s:
                s.add(WorkerLease(name=self.name, holder=self.holder, acquired_at=now, expires_at=self._exp(),
                                  fencing_token=1))
            self.token = 1
            return True
        except IntegrityError:
            return False

    async def renew(self) -> None:
        async with self.db.session() as s:
            res = await s.execute(update(WorkerLease).where(WorkerLease.name == self.name,
                                                            WorkerLease.holder == self.holder,
                                                            WorkerLease.fencing_token == self.token)
                                  .values(expires_at=self._exp()))
            if res.rowcount != 1:
                raise LeaseLost(self.holder)

    async def release(self) -> None:
        async with self.db.session() as s:
            await s.execute(update(WorkerLease).where(WorkerLease.name == self.name,
                                                      WorkerLease.holder == self.holder)
                            .values(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)))

    async def current(self) -> WorkerLease | None:
        async with self.db.session() as s:
            return (await s.execute(select(WorkerLease).where(WorkerLease.name == self.name))).scalar_one_or_none()

    async def wait_acquire(self, timeout: float, interval: float = 5.0) -> bool:
        """Used by a freshly chained runner while the previous one finishes its graceful shutdown."""
        import asyncio
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        while True:
            if await self.acquire():
                return True
            if loop.time() >= end:
                return False
            await asyncio.sleep(interval)
