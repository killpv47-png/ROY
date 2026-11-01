"""Agent task model: structured tasks with a validated state machine, events and recovery.

Lifecycle:  QUEUED → RESEARCHING → PLANNING → EXECUTING → VERIFYING → COMPLETED
Side states: FAILED, PAUSED, CANCELLED, RECOVERY_REQUIRED
"""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select, update

from ..db.models import AgentTask, TaskEvent
from ..security.redact import redact

QUEUED, RESEARCHING, PLANNING, EXECUTING, VERIFYING, COMPLETED = (
    "QUEUED", "RESEARCHING", "PLANNING", "EXECUTING", "VERIFYING", "COMPLETED")
FAILED, PAUSED, CANCELLED, RECOVERY_REQUIRED = "FAILED", "PAUSED", "CANCELLED", "RECOVERY_REQUIRED"

ACTIVE_STATES = {RESEARCHING, PLANNING, EXECUTING, VERIFYING}
TERMINAL_STATES = {COMPLETED, FAILED, CANCELLED}
ALL_STATES = ACTIVE_STATES | TERMINAL_STATES | {QUEUED, PAUSED, RECOVERY_REQUIRED}

TRANSITIONS: dict[str, set[str]] = {
    QUEUED: {RESEARCHING, PLANNING, EXECUTING, CANCELLED, PAUSED, FAILED},
    RESEARCHING: {PLANNING, EXECUTING, FAILED, CANCELLED, PAUSED, RECOVERY_REQUIRED},
    PLANNING: {RESEARCHING, EXECUTING, FAILED, CANCELLED, PAUSED, RECOVERY_REQUIRED},
    EXECUTING: {RESEARCHING, VERIFYING, COMPLETED, FAILED, CANCELLED, PAUSED, RECOVERY_REQUIRED},
    VERIFYING: {EXECUTING, COMPLETED, FAILED, CANCELLED, RECOVERY_REQUIRED},
    PAUSED: {QUEUED, CANCELLED},
    RECOVERY_REQUIRED: {QUEUED, CANCELLED, FAILED},
    COMPLETED: set(), FAILED: {QUEUED}, CANCELLED: set(),
}


class InvalidTransition(Exception):
    pass


class TaskCancelled(Exception):
    pass


class TaskPaused(Exception):
    pass


def can_transition(src: str, dst: str) -> bool:
    return dst in TRANSITIONS.get(src, set())


@dataclass
class PlanStep:
    key: str            # i18n step key, e.g. "api_profile"
    tool: str | None
    status: str = "pending"  # pending|running|done|skipped|failed
    detail: str | None = None

    def to_dict(self) -> dict:
        return {"key": self.key, "tool": self.tool, "status": self.status, "detail": self.detail}


@dataclass
class TaskView:
    id: str
    user_id: int
    chat_id: int
    kind: str
    goal: str
    status: str
    progress: int
    plan: list[dict]
    current_step: int
    params: dict
    result: dict
    error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    required_tools: list[str] = field(default_factory=list)

    @classmethod
    def of(cls, t: AgentTask) -> "TaskView":
        return cls(id=t.id, user_id=t.user_id, chat_id=t.chat_id, kind=t.kind, goal=t.goal, status=t.status,
                   progress=t.progress, plan=list(t.plan or []), current_step=t.current_step,
                   params=dict(t.params or {}), result=dict(t.result or {}), error=t.error,
                   created_at=t.created_at, started_at=t.started_at, finished_at=t.finished_at,
                   required_tools=list(t.required_tools or []))


class TaskManager:
    def __init__(self, db):
        self.db = db
        self._cancel: dict[str, asyncio.Event] = {}
        self._pause: dict[str, asyncio.Event] = {}

    async def create(self, user_id: int, chat_id: int, kind: str, goal: str, *, params: dict | None = None,
                     description: str | None = None, plan: list[PlanStep] | None = None,
                     required_tools: list[str] | None = None) -> TaskView:
        tid = str(uuid.uuid4())
        async with self.db.session() as s:
            t = AgentTask(id=tid, user_id=user_id, chat_id=chat_id, kind=kind, goal=goal[:256],
                          description=description, params=params or {},
                          plan=[p.to_dict() for p in (plan or [])],
                          required_tools=required_tools or sorted({p.tool for p in plan or [] if p.tool}),
                          status=QUEUED, progress=0)
            s.add(t)
            s.add(TaskEvent(task_id=tid, event="created", message=goal[:500]))
            await s.flush()
            return TaskView.of(t)

    async def get(self, task_id: str) -> TaskView | None:
        async with self.db.session() as s:
            t = await s.get(AgentTask, task_id)
            return TaskView.of(t) if t else None

    async def transition(self, task_id: str, dst: str, *, message: str = "", error: str | None = None,
                         progress: int | None = None, result: dict | None = None) -> TaskView:
        async with self.db.session() as s:
            t = await s.get(AgentTask, task_id)
            if t is None:
                raise LookupError(task_id)
            if t.status == dst:
                return TaskView.of(t)
            if not can_transition(t.status, dst):
                raise InvalidTransition(f"{t.status} -> {dst}")
            now = datetime.now(timezone.utc)
            src, t.status = t.status, dst
            if dst in ACTIVE_STATES and t.started_at is None:
                t.started_at = now
            if dst in TERMINAL_STATES:
                t.finished_at = now
                if dst == COMPLETED:
                    t.progress = 100
            if dst == QUEUED:
                t.attempts += 1
                t.finished_at = None
            if progress is not None:
                t.progress = max(0, min(100, progress))
            if error is not None:
                t.error = redact(error)[:2000]
            if result is not None:
                t.result = result
            s.add(TaskEvent(task_id=task_id, event=f"status:{dst}", level="error" if dst == FAILED else "info",
                            message=redact(message or f"{src} -> {dst}")[:1000]))
            return TaskView.of(t)

    async def set_plan(self, task_id: str, plan: list[PlanStep]) -> None:
        async with self.db.session() as s:
            t = await s.get(AgentTask, task_id)
            t.plan = [p.to_dict() for p in plan]
            t.required_tools = sorted({p.tool for p in plan if p.tool})
            s.add(TaskEvent(task_id=task_id, event="plan", message=", ".join(p.key for p in plan)))

    async def step(self, task_id: str, key: str, status: str, *, progress: int | None = None,
                   detail: str | None = None) -> TaskView:
        async with self.db.session() as s:
            t = await s.get(AgentTask, task_id)
            plan = [dict(p) for p in (t.plan or [])]
            idx = next((i for i, p in enumerate(plan) if p["key"] == key), None)
            if idx is None:
                plan.append({"key": key, "tool": None, "status": status, "detail": detail})
                idx = len(plan) - 1
            else:
                plan[idx]["status"] = status
                if detail is not None:
                    plan[idx]["detail"] = redact(detail)[:300]
            t.plan = plan
            t.current_step = idx
            if progress is not None:
                t.progress = max(t.progress, min(99, progress))
            s.add(TaskEvent(task_id=task_id, event=f"step:{status}", message=key,
                            data={"detail": redact(detail)[:300]} if detail else {}))
            return TaskView.of(t)

    async def event(self, task_id: str, event: str, message: str, level: str = "info", data: dict | None = None):
        async with self.db.session() as s:
            s.add(TaskEvent(task_id=task_id, event=event[:48], message=redact(message)[:1000], level=level,
                            data=data or {}))

    async def events(self, task_id: str, limit: int = 30) -> list[TaskEvent]:
        async with self.db.session() as s:
            return list((await s.execute(select(TaskEvent).where(TaskEvent.task_id == task_id)
                                         .order_by(TaskEvent.id.desc()).limit(limit))).scalars())[::-1]

    async def checkpoint_result(self, task_id: str, partial: dict) -> None:
        """Persist intermediate results so a task resumed after restart can skip completed steps."""
        async with self.db.session() as s:
            t = await s.get(AgentTask, task_id)
            # Build NEW dicts (no shared nested objects): mutating the loaded JSON in place makes the old and
            # new values compare equal, so SQLAlchemy would silently skip the UPDATE and lose the checkpoint.
            r = {k: v for k, v in (t.result or {}).items() if k != "_partial"}
            r["_partial"] = {**((t.result or {}).get("_partial") or {}), **partial}
            t.result = r

    # ------------------------------------------------------------------ control
    def control_handles(self, task_id: str) -> tuple[asyncio.Event, asyncio.Event]:
        c = self._cancel.setdefault(task_id, asyncio.Event())
        p = self._pause.setdefault(task_id, asyncio.Event())
        return c, p

    def release(self, task_id: str) -> None:
        self._cancel.pop(task_id, None)
        self._pause.pop(task_id, None)

    async def request_cancel(self, task_id: str, user_id: int | None = None) -> bool:
        tv = await self.get(task_id)
        if not tv or (user_id is not None and tv.user_id != user_id) or tv.status in TERMINAL_STATES:
            return False
        self.control_handles(task_id)[0].set()
        if tv.status in (QUEUED, PAUSED, RECOVERY_REQUIRED):
            await self.transition(task_id, CANCELLED, message="cancelled by user")
        return True

    async def request_pause(self, task_id: str, user_id: int | None = None) -> bool:
        tv = await self.get(task_id)
        if not tv or (user_id is not None and tv.user_id != user_id):
            return False
        if tv.status == QUEUED:
            await self.transition(task_id, PAUSED, message="paused by user")
            return True
        if tv.status in (RESEARCHING, PLANNING, EXECUTING):
            self.control_handles(task_id)[1].set()
            return True
        return False

    async def resume(self, task_id: str, user_id: int | None = None) -> bool:
        tv = await self.get(task_id)
        if not tv or (user_id is not None and tv.user_id != user_id):
            return False
        if tv.status in (PAUSED, RECOVERY_REQUIRED, FAILED):
            self._pause.pop(task_id, None)
            self._cancel.pop(task_id, None)
            await self.transition(task_id, QUEUED, message="resumed")
            return True
        return False

    async def checkpoint_guard(self, task_id: str) -> None:
        """Called between steps: raises if cancellation or pause was requested."""
        c, p = self.control_handles(task_id)
        if c.is_set():
            raise TaskCancelled(task_id)
        if p.is_set():
            raise TaskPaused(task_id)

    # ------------------------------------------------------------------ queue & recovery
    async def claim_next(self, exclude: set[str] | None = None) -> TaskView | None:
        """Atomically move the oldest QUEUED task to PLANNING (compare-and-set on status)."""
        async with self.db.session() as s:
            q = select(AgentTask).where(AgentTask.status == QUEUED)
            if exclude:
                q = q.where(AgentTask.id.not_in(exclude))
            rows = (await s.execute(q.order_by(AgentTask.created_at).limit(20))).scalars().all()
        now = datetime.now(timezone.utc)
        ids = []
        for row in rows:
            ra = (row.params or {}).get("_resume_after")
            if ra and datetime.fromisoformat(ra) > now:
                continue  # waiting for the resume delay
            ids.append(row.id)
        for tid in ids[:5]:
            async with self.db.session() as s:
                res = await s.execute(update(AgentTask).where(AgentTask.id == tid, AgentTask.status == QUEUED)
                                      .values(status=PLANNING, started_at=func.coalesce(AgentTask.started_at,
                                                                                        datetime.now(timezone.utc))))
                if res.rowcount == 1:
                    s.add(TaskEvent(task_id=tid, event=f"status:{PLANNING}", message="claimed by worker"))
                    t = await s.get(AgentTask, tid)
                    await s.refresh(t)
                    return TaskView.of(t)
        return None

    async def interrupted_to_recovery(self) -> list[TaskView]:
        """On startup: active tasks left by a previous worker → RECOVERY_REQUIRED (then auto re-queued)."""
        async with self.db.session() as s:
            rows = (await s.execute(select(AgentTask).where(AgentTask.status.in_(ACTIVE_STATES)))).scalars().all()
            out = []
            for t in rows:
                t.status = RECOVERY_REQUIRED
                s.add(TaskEvent(task_id=t.id, event=f"status:{RECOVERY_REQUIRED}",
                                message="worker restarted while task was running"))
                out.append(TaskView.of(t))
            return out

    async def requeue_recoverable(self, max_attempts: int = 5, delay_seconds: float = 10.0
                                  ) -> tuple[list[TaskView], list[TaskView]]:
        """RECOVERY_REQUIRED → QUEUED with a resume delay. Completed steps and their persisted partial
        results are kept, so the task continues from the same step instead of starting over."""
        requeued, failed = [], []
        async with self.db.session() as s:
            ids = (await s.execute(select(AgentTask.id).where(AgentTask.status == RECOVERY_REQUIRED))).scalars().all()
        for tid in ids:
            async with self.db.session() as s:
                t = await s.get(AgentTask, tid)
                attempts = t.attempts
            if attempts >= max_attempts:
                failed.append(await self.transition(tid, FAILED, error="max recovery attempts reached"))
                continue
            await self.schedule_resume(tid, delay_seconds)
            requeued.append(await self.transition(tid, QUEUED, message=f"auto-resume in {int(delay_seconds)}s "
                                                                       f"from step {await self.resume_step(tid)}"))
        return requeued, failed

    async def schedule_resume(self, task_id: str, delay_seconds: float) -> datetime:
        when = datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
        async with self.db.session() as s:
            t = await s.get(AgentTask, task_id)
            params = dict(t.params or {})
            params["_resume_after"] = when.isoformat()
            t.params = params
        return when

    async def resume_step(self, task_id: str) -> str | None:
        """First plan step that is not done (the step the task will continue from)."""
        tv = await self.get(task_id)
        if not tv:
            return None
        for p in tv.plan:
            if p.get("status") not in ("done", "skipped"):
                return p["key"]
        return None

    async def partial(self, task_id: str) -> dict:
        tv = await self.get(task_id)
        return dict((tv.result or {}).get("_partial") or {}) if tv else {}

    async def unfinished_ids(self) -> list[str]:
        async with self.db.session() as s:
            return list((await s.execute(select(AgentTask.id).where(
                AgentTask.status.not_in(TERMINAL_STATES)))).scalars())

    async def list_for_user(self, user_id: int, limit: int = 10, active_only: bool = False) -> list[TaskView]:
        async with self.db.session() as s:
            q = select(AgentTask).where(AgentTask.user_id == user_id)
            if active_only:
                q = q.where(AgentTask.status.not_in(TERMINAL_STATES))
            rows = (await s.execute(q.order_by(AgentTask.created_at.desc()).limit(limit))).scalars().all()
        return [TaskView.of(t) for t in rows]

    async def list_recent(self, limit: int = 15, active_only: bool = False) -> list[TaskView]:
        async with self.db.session() as s:
            q = select(AgentTask)
            if active_only:
                q = q.where(AgentTask.status.not_in(TERMINAL_STATES))
            rows = (await s.execute(q.order_by(AgentTask.created_at.desc()).limit(limit))).scalars().all()
        return [TaskView.of(t) for t in rows]

    async def counts(self) -> dict[str, int]:
        async with self.db.session() as s:
            rows = (await s.execute(select(AgentTask.status, func.count()).group_by(AgentTask.status))).all()
        return {st: n for st, n in rows}

    async def purge_old(self, days: int) -> int:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        async with self.db.session() as s:
            rows = (await s.execute(select(AgentTask).where(AgentTask.status.in_(TERMINAL_STATES),
                                                            AgentTask.created_at < cutoff))).scalars().all()
            for t in rows:
                await s.delete(t)
            return len(rows)


def summarize_events(events: list[Any], max_chars: int = 600) -> str:
    """Compact task-history summary (keeps agent context bounded)."""
    parts, total = [], 0
    for e in events[::-1]:
        line = f"{e.event}: {e.message}"[:120]
        if total + len(line) > max_chars:
            parts.append(f"... ({len(events) - len(parts)} earlier events)")
            break
        parts.append(line)
        total += len(line)
    return "\n".join(parts[::-1])
