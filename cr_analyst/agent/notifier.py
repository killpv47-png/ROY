"""Real-time task notifications.

* One live progress message per task, edited in place (Telegram editMessageText); a new message is only sent
  for task start, completion and failure.
* Low-level events are batched: edits are throttled (min interval) unless the status changes.
* State (message id, last hash, counts) is persisted in `notification_state` so a restarted worker keeps
  editing the same message instead of spamming new ones.
* Every outgoing text is passed through secret redaction.
"""
from __future__ import annotations

import hashlib
import logging
import time
from datetime import datetime, timezone
from typing import Any, Protocol

from ..db.models import NotificationState
from ..i18n import fmt_date, rtl_wrap, t
from ..security.redact import redact
from .tasks import COMPLETED, FAILED, TERMINAL_STATES, TaskView

log = logging.getLogger(__name__)

STATUS_ICON = {"QUEUED": "🕓", "RESEARCHING": "🔎", "PLANNING": "🧭", "EXECUTING": "⚙️", "VERIFYING": "🧪",
               "COMPLETED": "✅", "FAILED": "❌", "PAUSED": "⏸", "CANCELLED": "🚫", "RECOVERY_REQUIRED": "♻️"}


class MessageSender(Protocol):
    async def send(self, chat_id: int, text: str, controls: list[list[tuple[str, str]]] | None = None) -> int | None: ...
    async def edit(self, chat_id: int, message_id: int, text: str,
                   controls: list[list[tuple[str, str]]] | None = None) -> bool: ...


class NullSender:
    """Used when no Telegram bot is attached (CLI / tests)."""
    def __init__(self):
        self.sent: list[tuple[int, str]] = []
        self.edits: list[tuple[int, int, str]] = []
        self._n = 0

    async def send(self, chat_id, text, controls=None):
        self._n += 1
        self.sent.append((chat_id, text))
        return self._n

    async def edit(self, chat_id, message_id, text, controls=None):
        self.edits.append((chat_id, message_id, text))
        return True


def progress_bar(pct: int, width: int = 10) -> str:
    pct = max(0, min(100, pct))
    full = round(pct / 100 * width)
    return "▰" * full + "▱" * (width - full)


def task_controls(lang: str, tv: TaskView) -> list[list[tuple[str, str]]]:
    if tv.status in TERMINAL_STATES:
        return [[(t(lang, "ctl_details"), f"tk:view:{tv.id}")]]
    row = [(t(lang, "ctl_refresh"), f"tk:view:{tv.id}")]
    if tv.status == "PAUSED" or tv.status == "RECOVERY_REQUIRED":
        row.append((t(lang, "ctl_resume"), f"tk:resume:{tv.id}"))
    else:
        row.append((t(lang, "ctl_pause"), f"tk:pause:{tv.id}"))
    row.append((t(lang, "ctl_cancel"), f"tk:cancel:{tv.id}"))
    return [row]


def render_task(lang: str, tv: TaskView, op: str | None = None, note: str | None = None) -> str:
    icon = STATUS_ICON.get(tv.status, "•")
    goal = t(lang, f"task_{tv.kind}") if tv.kind else tv.goal
    lines = [f"{icon} <b>{goal}</b>",
             f"🆔 <code>{tv.id[:8]}</code> · {t(lang, 'st_' + tv.status.lower())}",
             f"{progress_bar(tv.progress)} {tv.progress}%"]
    if op:
        lines.append(f"▶️ {op}")
    done = [p for p in tv.plan if p.get("status") == "done"]
    if tv.plan:
        lines.append(t(lang, "task_steps", done=len(done), total=len(tv.plan)))
    if tv.started_at:
        lines.append(f"🕐 {t(lang, 'task_started')}: {fmt_date(lang, tv.started_at, with_time=True)}")
    if tv.finished_at:
        lines.append(f"🏁 {t(lang, 'task_finished')}: {fmt_date(lang, tv.finished_at, with_time=True)}")
    if note:
        lines.append(note)
    if tv.status == FAILED and tv.error:
        lines.append(f"⚠️ {redact(tv.error)[:300]}")
    return rtl_wrap(lang, "\n".join(lines))


class Notifier:
    def __init__(self, db, sender: MessageSender | None = None, min_interval: float = 2.5):
        self.db = db
        self.sender: MessageSender = sender or NullSender()
        self.min_interval = min_interval
        self._last_edit: dict[str, float] = {}
        self._last_status: dict[str, str] = {}

    async def _state(self, task_id: str) -> NotificationState | None:
        async with self.db.session() as s:
            return await s.get(NotificationState, task_id)

    async def _save(self, task_id: str, chat_id: int, message_id: int | None, text: str) -> None:
        h = hashlib.sha256(text.encode()).hexdigest()
        async with self.db.session() as s:
            st = await s.get(NotificationState, task_id)
            if st is None:
                s.add(NotificationState(task_id=task_id, chat_id=chat_id, message_id=message_id, last_text_hash=h,
                                        last_sent_at=datetime.now(timezone.utc), sent_count=1))
            else:
                st.message_id = message_id or st.message_id
                st.last_text_hash = h
                st.last_sent_at = datetime.now(timezone.utc)
                st.sent_count += 1

    async def publish(self, lang: str, tv: TaskView, op: str | None = None, note: str | None = None,
                      force: bool = False) -> None:
        """Create or edit the task's live progress message."""
        text = redact(render_task(lang, tv, op, note))
        controls = task_controls(lang, tv)
        now = time.monotonic()
        status_changed = self._last_status.get(tv.id) != tv.status
        if not force and not status_changed and now - self._last_edit.get(tv.id, 0) < self.min_interval:
            return  # batched: intermediate low-level events are coalesced into the next edit
        st = await self._state(tv.id)
        h = hashlib.sha256(text.encode()).hexdigest()
        if st and st.last_text_hash == h:
            return
        try:
            if st and st.message_id:
                ok = await self.sender.edit(tv.chat_id, st.message_id, text, controls)
                if not ok:  # message deleted / too old → send a fresh one
                    mid = await self.sender.send(tv.chat_id, text, controls)
                    await self._save(tv.id, tv.chat_id, mid, text)
                else:
                    await self._save(tv.id, tv.chat_id, st.message_id, text)
            else:
                mid = await self.sender.send(tv.chat_id, text, controls)
                await self._save(tv.id, tv.chat_id, mid, text)
        except Exception as e:  # Telegram delivery failure must never break the task
            log.warning("notification delivery failed: %s", type(e).__name__)
        self._last_edit[tv.id] = now
        self._last_status[tv.id] = tv.status
        if tv.status in TERMINAL_STATES:
            self._last_edit.pop(tv.id, None)
            self._last_status.pop(tv.id, None)

    async def event(self, lang: str, tv: TaskView, text: str) -> None:
        """A standalone notification for meaningful milestones (file generated, terminal started, error)."""
        try:
            await self.sender.send(tv.chat_id, rtl_wrap(lang, redact(text)))
        except Exception as e:
            log.warning("notification delivery failed: %s", type(e).__name__)

    async def direct(self, chat_id: int, lang: str, text: str, controls: Any = None) -> int | None:
        try:
            return await self.sender.send(chat_id, rtl_wrap(lang, redact(text)), controls)
        except Exception as e:
            log.warning("direct notification failed: %s", type(e).__name__)
            return None


__all__ = ["Notifier", "NullSender", "render_task", "progress_bar", "task_controls", "COMPLETED"]
