"""Durable recovery checkpoints (Telegram offset, scheduler state, unfinished tasks, runner chain info)."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select

from ..db.models import RecoveryCheckpoint


@dataclass
class CheckpointState:
    telegram_offset: int | None = None
    unfinished_tasks: list[str] = field(default_factory=list)
    running_tasks: list[str] = field(default_factory=list)
    scheduler: dict = field(default_factory=dict)       # job -> last run iso
    chain_index: int = 0
    worker_id: str = ""
    github_run_id: str | None = None
    version: int = 1

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict | None) -> "CheckpointState":
        d = dict(d or {})
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


class CheckpointStore:
    def __init__(self, db):
        self.db = db

    async def save(self, state: CheckpointState, reason: str) -> int:
        async with self.db.session() as s:
            cp = RecoveryCheckpoint(worker_id=state.worker_id, reason=reason, telegram_offset=state.telegram_offset,
                                    state=state.to_dict(), created_at=datetime.now(timezone.utc))
            s.add(cp)
            await s.flush()
            return cp.id

    async def latest(self) -> CheckpointState | None:
        async with self.db.session() as s:
            cp = (await s.execute(select(RecoveryCheckpoint).order_by(RecoveryCheckpoint.id.desc()).limit(1))
                  ).scalar_one_or_none()
        if not cp:
            return None
        st = CheckpointState.from_dict(cp.state)
        if st.telegram_offset is None:
            st.telegram_offset = cp.telegram_offset
        return st

    async def prune(self, keep: int = 50) -> int:
        async with self.db.session() as s:
            ids = (await s.execute(select(RecoveryCheckpoint.id).order_by(RecoveryCheckpoint.id.desc()))).scalars().all()
            old = ids[keep:]
            for i in old:
                obj = await s.get(RecoveryCheckpoint, i)
                if obj:
                    await s.delete(obj)
            return len(old)
