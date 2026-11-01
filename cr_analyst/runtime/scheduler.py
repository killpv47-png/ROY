"""Persistent lightweight scheduler. Job last-run times live in the checkpoint/app_config, so a restarted
runner catches up instead of re-running everything (no cron triggers needed)."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

log = logging.getLogger(__name__)
Job = Callable[[], Awaitable[object]]


class Scheduler:
    def __init__(self, svc):
        self.svc = svc
        self.jobs: dict[str, tuple[float, Job]] = {}
        self.last_run: dict[str, str] = {}

    def add(self, name: str, interval_minutes: float, fn: Job) -> None:
        self.jobs[name] = (interval_minutes, fn)

    def restore(self, state: dict | None) -> None:
        self.last_run.update({k: v for k, v in (state or {}).items() if isinstance(v, str)})

    def state(self) -> dict:
        return dict(self.last_run)

    def due(self, now: datetime | None = None) -> list[str]:
        now = now or datetime.now(timezone.utc)
        out = []
        for name, (interval, _) in self.jobs.items():
            last = self.last_run.get(name)
            if not last or now - datetime.fromisoformat(last) >= timedelta(minutes=interval):
                out.append(name)
        return out

    async def run_due(self) -> list[str]:
        ran = []
        for name in self.due():
            _, fn = self.jobs[name]
            try:
                await asyncio.wait_for(fn(), timeout=600)
                ran.append(name)
            except Exception as e:
                log.warning("scheduled job %s failed: %s", name, type(e).__name__)
                await self.svc.errors.record("scheduler", f"{name}:{type(e).__name__}", str(e))
            finally:
                self.last_run[name] = datetime.now(timezone.utc).isoformat()
        return ran

    async def loop(self, stop: asyncio.Event, tick: float = 30.0) -> None:
        while not stop.is_set():
            await self.run_due()
            try:
                await asyncio.wait_for(stop.wait(), timeout=tick)
            except asyncio.TimeoutError:
                pass


def register_default_jobs(sch: Scheduler, svc) -> None:
    async def meta_update():
        if not await svc.get_config("meta_auto_update", True):
            return
        rep = await svc.kb.research_official_balance()
        await svc.set_config("meta_last_refresh", datetime.now(timezone.utc).isoformat())
        if rep.get("changes_added"):
            owner = await svc.users.get_owner_id()
            if owner:
                await svc.notifier.direct(owner, "en", f"📈 Meta update: {rep['changes_added']} new official balance "
                                                       f"changes ingested from {len(rep['new_articles'])} article(s).")

    async def battle_tracking():
        from sqlalchemy import select
        from ..db.models import PlayerProfile
        async with svc.db.session() as s:
            players = (await s.execute(select(PlayerProfile).where(PlayerProfile.track_battles.is_(True)))).scalars().all()
        for p in players[:200]:
            try:
                await svc.scanner_for(p.user_id).poll_battles(p.id, p.player_tag)
            except Exception as e:  # one failing player must not stop the job
                log.debug("battle poll failed for %s: %s", p.id, type(e).__name__)
                if type(e).__name__ in ("NotConfigured", "RouteRejected", "InvalidIP"):
                    break
            await asyncio.sleep(0.3)

    async def retention():
        from sqlalchemy import delete
        from ..db.models import AccountSnapshot, ApiHealthRecord, BattleRecord, ProcessedUpdate
        st = svc.settings
        now = datetime.now(timezone.utc)
        async with svc.db.session() as s:
            await s.execute(delete(BattleRecord).where(BattleRecord.battle_time < now - timedelta(days=st.battle_retention_days)))
            await s.execute(delete(AccountSnapshot).where(AccountSnapshot.taken_at < now - timedelta(days=st.snapshot_retention_days)))
            await s.execute(delete(ApiHealthRecord).where(ApiHealthRecord.ts < now - timedelta(days=14)))
            await s.execute(delete(ProcessedUpdate).where(ProcessedUpdate.processed_at < now - timedelta(days=3)))
        await svc.tasks.purge_old(st.task_retention_days)
        await svc.web.cache.purge_expired()

    sch.add("meta_update", 6 * 60, meta_update)
    sch.add("battle_tracking", 60, battle_tracking)
    sch.add("retention", 24 * 60, retention)
