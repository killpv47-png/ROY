"""Update middlewares: idempotency (processed-update guard), user registration/injection, error capture."""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, Update
from sqlalchemy.exc import IntegrityError

from ..db.models import ProcessedUpdate
from ..i18n import SUPPORTED, normalize_lang, t

log = logging.getLogger(__name__)


class IdempotencyMiddleware(BaseMiddleware):
    """Outer middleware on Update: an update_id is processed at most once, even across worker restarts."""

    def __init__(self, svc):
        self.svc = svc

    async def __call__(self, handler: Callable[[TelegramObject, dict], Awaitable[Any]], event: Update,
                       data: dict[str, Any]) -> Any:
        try:
            async with self.svc.db.session() as s:
                s.add(ProcessedUpdate(update_id=event.update_id))
        except IntegrityError:
            log.info("duplicate update %s skipped", event.update_id)
            return None
        return await handler(event, data)


class UserMiddleware(BaseMiddleware):
    """Registers / refreshes the user and injects `svc`, `user`, `lang` into handler data."""

    def __init__(self, svc):
        self.svc = svc

    async def __call__(self, handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        data["svc"] = self.svc
        tg_user = getattr(event, "from_user", None)
        chat = event.message.chat if isinstance(event, CallbackQuery) and event.message else getattr(event, "chat", None)
        user = None
        if tg_user is not None and not tg_user.is_bot:
            user = await self.svc.users.get(tg_user.id)
            is_start = isinstance(event, Message) and (event.text or "").startswith("/start")
            if user is None and is_start:
                res = await self.svc.users.register(
                    tg_user.id, tg_user.username, tg_user.first_name, tg_user.language_code,
                    chat.id if chat else tg_user.id, chat.type if chat else "private", SUPPORTED)
                user = res.user
                data["registration"] = res
            elif user is not None:
                await self.svc.users.touch(tg_user.id)
        data["user"] = user
        data["lang"] = normalize_lang(user.language if user else (tg_user.language_code if tg_user else None))
        if user is not None and user.role == "banned":
            if isinstance(event, CallbackQuery):
                await event.answer(t(data["lang"], "err_banned"), show_alert=True)
            elif isinstance(event, Message):
                await event.answer(t(data["lang"], "err_banned"))
            return None
        try:
            return await handler(event, data)
        except Exception as e:
            log.exception("handler error")
            await self.svc.errors.record("telegram", type(e).__name__, str(e), user.id if user else None)
            try:
                if isinstance(event, CallbackQuery):
                    await event.answer(t(data["lang"], "err_generic", code=type(e).__name__), show_alert=True)
                elif isinstance(event, Message):
                    await event.answer(t(data["lang"], "err_generic", code=type(e).__name__))
            except Exception:
                pass
            return None
