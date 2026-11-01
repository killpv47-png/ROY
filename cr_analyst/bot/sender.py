"""Telegram implementation of the Notifier's MessageSender (send / edit with inline controls)."""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions

log = logging.getLogger(__name__)


def to_markup(controls) -> InlineKeyboardMarkup | None:
    if not controls:
        return None
    if isinstance(controls, InlineKeyboardMarkup):
        return controls
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=a, callback_data=b) for a, b in row]
                                                 for row in controls])


class TelegramSender:
    def __init__(self, bot: Bot):
        self.bot = bot

    async def send(self, chat_id: int, text: str, controls=None) -> int | None:
        for attempt in range(3):
            try:
                m = await self.bot.send_message(chat_id, text[:4096], reply_markup=to_markup(controls),
                                                link_preview_options=LinkPreviewOptions(is_disabled=True))
                return m.message_id
            except TelegramRetryAfter as e:
                await asyncio.sleep(min(30, e.retry_after))
            except TelegramForbiddenError:
                log.info("user %s blocked the bot", chat_id)
                return None
        return None

    async def edit(self, chat_id: int, message_id: int, text: str, controls=None) -> bool:
        try:
            await self.bot.edit_message_text(text=text[:4096], chat_id=chat_id, message_id=message_id,
                                             reply_markup=to_markup(controls),
                                             link_preview_options=LinkPreviewOptions(is_disabled=True))
            return True
        except TelegramRetryAfter as e:
            await asyncio.sleep(min(30, e.retry_after))
            return True  # skip this intermediate edit; next update will carry the latest state
        except TelegramBadRequest as e:
            if "not modified" in str(e).lower():
                return True
            return False
        except TelegramForbiddenError:
            return True
