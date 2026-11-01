"""Worker lifecycle for GitHub Actions (best-effort hosting) — also works unchanged on a VPS (MAX_RUNTIME_MINUTES=0).

startup:  DB → lease (single active worker) → latest checkpoint → interrupted tasks → RECOVERY_REQUIRED → re-queued
          with RESUME_DELAY_SECONDS (continue from the same step) → restore Telegram offset & scheduler state.
running:  long polling with a persisted offset + processed-update idempotency guard, task workers, scheduler,
          lease renewal and periodic checkpoints.
shutdown: at the deadline (or SIGTERM / owner restart): stop polling, give running tasks a grace period, save the
          final checkpoint (offset, scheduler, unfinished tasks), release the lease, then dispatch the next run
          (bounded by MAX_AUTO_RESTARTS and the owner's pause switch).
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
import time
import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from ..db.models import RunnerRun
from .checkpoint import CheckpointState, CheckpointStore
from .dispatch import GitHubDispatcher
from .lease import LeaseLost, WorkerLeaseManager
from .scheduler import Scheduler, register_default_jobs

log = logging.getLogger(__name__)


class Runner:
    def __init__(self, svc, *, chain_index: int = 0, dispatcher: GitHubDispatcher | None = None,
                 updates_source=None):
        self.svc = svc
        st = svc.settings
        self.worker_id = f"{socket.gethostname()}:{os.getpid()}:{st.github_run_id or uuid.uuid4().hex[:8]}"
        self.lease = WorkerLeaseManager(svc.db, self.worker_id, st.lease_ttl_seconds)
        self.checkpoints = CheckpointStore(svc.db)
        self.scheduler = Scheduler(svc)
        register_default_jobs(self.scheduler, svc)
        self.dispatcher = dispatcher or GitHubDispatcher(st.gh_pat, st.github_repository, st.runner_workflow_file,
                                                         st.github_ref_name)
        self.chain_index = chain_index
        self.started = time.monotonic()
        self.deadline = self.started + st.max_runtime_minutes * 60 if st.max_runtime_minutes > 0 else None
        self.stop = asyncio.Event()
        self.exit_reason = "unknown"
        self.offset: int | None = None
        self.updates_processed = 0
        self.run_row_id: int | None = None
        self.updates_source = updates_source  # injectable for tests
        self.last_dispatch: str | None = None
        svc.runtime_status_sync = self.status
        svc.runtime_status = self._status_async

    # ------------------------------------------------------------------ status
    def status(self) -> dict:
        up = int((time.monotonic() - self.started) / 60)
        rem = int((self.deadline - time.monotonic()) / 60) if self.deadline else None
        return {"mode": "github-actions" if os.getenv("GITHUB_ACTIONS") == "true" else "persistent",
                "worker": self.worker_id, "uptime_min": up, "remaining_min": rem, "chain_index": self.chain_index,
                "updates": self.updates_processed, "last_dispatch": self.last_dispatch,
                "paused": getattr(self, "_paused_cache", False)}

    async def _status_async(self) -> dict:
        self._paused_cache = bool(await self.svc.get_config("runner_paused", False))
        return self.status()

    # ------------------------------------------------------------------ lifecycle
    async def startup(self) -> bool:
        await self.svc.init()
        wait = 240 if self.chain_index > 0 else 30  # chained run may overlap the previous run's shutdown
        if not await self.lease.wait_acquire(timeout=wait, interval=5):
            cur = await self.lease.current()
            log.error("another worker holds the lease (%s); exiting", cur.holder if cur else "?")
            self.exit_reason = "lease_held_by_other"
            return False
        cp = await self.checkpoints.latest()
        if cp:
            self.offset = cp.telegram_offset
            self.scheduler.restore(cp.scheduler)
            self.chain_index = max(self.chain_index, cp.chain_index)
        interrupted = await self.svc.tasks.interrupted_to_recovery()
        requeued, failed = await self.svc.tasks.requeue_recoverable(
            max_attempts=self.svc.settings.max_resume_attempts, delay_seconds=self.svc.settings.resume_delay_seconds)
        for tv in requeued:
            user = await self.svc.users.get_by_id(tv.user_id)
            lang = user.language if user else "en"
            from ..i18n import t
            step = await self.svc.tasks.resume_step(tv.id)
            await self.svc.notifier.publish(lang, tv, op=t(lang, "op_waiting_resume",
                                                           seconds=self.svc.settings.resume_delay_seconds,
                                                           step=t(lang, f"op_{step}") if step else "—"), force=True)
        async with self.svc.db.session() as s:
            row = RunnerRun(worker_id=self.worker_id, github_run_id=self.svc.settings.github_run_id or None,
                            chain_index=self.chain_index)
            s.add(row)
            await s.flush()
            self.run_row_id = row.id
        await self.save_checkpoint("startup")
        log.info("worker %s started (chain %d, offset %s, recovered %d, failed %d)", self.worker_id,
                 self.chain_index, self.offset, len(requeued), len(failed))
        owner = await self.svc.users.get_owner_id()
        if owner and (requeued or self.chain_index > 0):
            await self.svc.notifier.direct(owner, "en", f"🏃 Runner started (chain #{self.chain_index}); "
                                                         f"{len(requeued)} interrupted task(s) will resume in "
                                                         f"{self.svc.settings.resume_delay_seconds}s.")
        return True

    async def save_checkpoint(self, reason: str) -> None:
        state = CheckpointState(telegram_offset=self.offset, unfinished_tasks=await self.svc.tasks.unfinished_ids(),
                                running_tasks=self.svc.orchestrator.running_ids(), scheduler=self.scheduler.state(),
                                chain_index=self.chain_index, worker_id=self.worker_id,
                                github_run_id=self.svc.settings.github_run_id or None)
        await self.checkpoints.save(state, reason)

    def on_update_processed(self, update_id: int) -> None:
        """Called after every Telegram update; offset = last update + 1 (persisted with each checkpoint)."""
        self.offset = max(self.offset or 0, update_id + 1)
        self.updates_processed += 1

    async def _housekeeping(self) -> None:
        last_cp = time.monotonic()
        while not self.stop.is_set():
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=min(30, self.svc.settings.lease_ttl_seconds / 3))
                break
            except asyncio.TimeoutError:
                pass
            try:
                await self.lease.renew()
            except LeaseLost:
                log.error("lease lost — another worker took over; stopping")
                self.exit_reason = "lease_lost"
                self.stop.set()
                break
            if time.monotonic() - last_cp >= self.svc.settings.checkpoint_interval_seconds:
                await self.save_checkpoint("periodic")
                await self.checkpoints.prune()
                last_cp = time.monotonic()
            if await self.svc.get_config("restart_requested", False):
                await self.svc.set_config("restart_requested", False)
                self.exit_reason = "owner_restart"
                self.stop.set()
            if self.deadline and time.monotonic() >= self.deadline:
                self.exit_reason = "planned_deadline"
                self.stop.set()

    async def run(self, poll_coro_factory) -> str:
        """`poll_coro_factory(stop_event)` runs Telegram polling until stop is set."""
        if not await self.startup():
            return self.exit_reason
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self._signal_stop)
            except (NotImplementedError, RuntimeError):
                pass
        tasks = [asyncio.create_task(self._housekeeping(), name="housekeeping"),
                 asyncio.create_task(self.scheduler.loop(self.stop), name="scheduler"),
                 asyncio.create_task(self.svc.orchestrator.run_workers(self.svc.settings.task_workers, self.stop),
                                     name="workers"),
                 asyncio.create_task(poll_coro_factory(self.stop), name="polling")]
        await self.stop.wait()
        await self.shutdown(tasks)
        return self.exit_reason

    def _signal_stop(self) -> None:
        self.exit_reason = "signal"
        self.stop.set()

    async def shutdown(self, tasks: list[asyncio.Task]) -> None:
        grace = self.svc.settings.shutdown_grace_seconds
        log.info("graceful shutdown (%s); waiting up to %ss for running tasks", self.exit_reason, grace)
        workers = next((t for t in tasks if t.get_name() == "workers"), None)
        end = time.monotonic() + grace
        while self.svc.orchestrator.running_ids() and time.monotonic() < end:
            await asyncio.sleep(1)
        for t in tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if workers is not None:
            # tasks interrupted mid-step remain non-terminal → next runner marks RECOVERY_REQUIRED and resumes them
            pass
        await self.save_checkpoint("shutdown")
        dispatch_status = await self.maybe_chain()
        async with self.svc.db.session() as s:
            row = await s.get(RunnerRun, self.run_row_id) if self.run_row_id else None
            if row:
                row.ended_at = datetime.now(timezone.utc)
                row.exit_reason = self.exit_reason
                row.updates_processed = self.updates_processed
                row.dispatch_status = dispatch_status[:48]
        await self.lease.release()
        log.info("worker stopped: %s; dispatch: %s", self.exit_reason, dispatch_status)

    async def maybe_chain(self) -> str:
        st = self.svc.settings
        if self.exit_reason not in ("planned_deadline", "owner_restart"):
            return "not_applicable"
        if not st.auto_restart:
            return "auto_restart_disabled"
        if await self.svc.get_config("runner_paused", False):
            await self._notify_owner("⏸ Runner reached its planned limit; auto-restart is paused by the owner.")
            return "paused_by_owner"
        if self.chain_index + 1 > st.max_auto_restarts:
            await self._notify_owner(f"🛑 Max automatic restarts ({st.max_auto_restarts}) reached. Start the "
                                     "bot-runner workflow manually to continue.")
            return "max_restarts_reached"
        if not self.dispatcher.configured:
            await self._notify_owner("⚠️ Cannot chain the next runner: GH_PAT / GITHUB_REPOSITORY not configured.")
            return "dispatch_not_configured"
        res = await self.dispatcher.dispatch({"chain_index": self.chain_index + 1, "reason": self.exit_reason})
        self.last_dispatch = f"{datetime.now(timezone.utc):%H:%M} {'ok' if res.ok else res.status}"
        await self.svc.network.record_health("github", res.ok, res.status, None, None, res.detail)
        if not res.ok:
            await self._notify_owner(f"❌ Failed to dispatch next runner ({res.status}): {res.detail[:120]}. "
                                     "Checkpoint saved; start the workflow manually.")
            return f"dispatch_failed_{res.status}"
        return "dispatched"

    async def _notify_owner(self, text: str) -> None:
        owner = await self.svc.users.get_owner_id()
        if owner:
            await self.svc.notifier.direct(owner, "en", text)


async def last_runs(svc, limit: int = 5) -> list[RunnerRun]:
    async with svc.db.session() as s:
        return list((await s.execute(select(RunnerRun).order_by(RunnerRun.id.desc()).limit(limit))).scalars())
