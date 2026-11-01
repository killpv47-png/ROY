"""Shared Telegram helpers: keyboards, safe sending (HTML, RTL, chunking), permission guards."""
from __future__ import annotations

import html
from typing import Iterable

from aiogram.types import (
    CallbackQuery, CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, Message,
    ReplyKeyboardMarkup,
)

from ..i18n import rtl_wrap, t

MAX_LEN = 3900

ADMIN_ROLES = ("owner", "admin")


def esc(v) -> str:
    return html.escape("" if v is None else str(v), quote=False)


def btn(text: str, data: str | None = None, *, url: str | None = None, copy: str | None = None) -> InlineKeyboardButton:
    if url:
        return InlineKeyboardButton(text=text, url=url)
    if copy is not None:
        return InlineKeyboardButton(text=text, copy_text=CopyTextButton(text=copy[:256]))
    return InlineKeyboardButton(text=text, callback_data=data[:64])


def ikb(rows: Iterable[Iterable[InlineKeyboardButton | tuple]]) -> InlineKeyboardMarkup:
    out = []
    for row in rows:
        r = [b if isinstance(b, InlineKeyboardButton) else btn(b[0], b[1]) for b in row]
        if r:
            out.append(r)
    return InlineKeyboardMarkup(inline_keyboard=out)


MAIN_LAYOUT = [["btn_account", "btn_scan"], ["btn_cards", "btn_battles"], ["btn_deck_builder"],
               ["btn_deck_def", "btn_deck_off", "btn_deck_bal"], ["btn_meta", "btn_ask"],
               ["btn_agent", "btn_settings"], ["btn_language", "btn_help"]]


def main_keyboard(lang: str, role: str) -> ReplyKeyboardMarkup:
    rows = [[KeyboardButton(text=t(lang, k)) for k in row] for row in MAIN_LAYOUT]
    if role in ADMIN_ROLES:
        rows.append([KeyboardButton(text=t(lang, "btn_admin"))])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=True,
                               input_field_placeholder=t(lang, "input_placeholder"))


def split_chunks(text: str, limit: int = MAX_LEN) -> list[str]:
    if len(text) <= limit:
        return [text]
    parts, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            if cur:
                parts.append(cur)
                cur = ""
            parts.append(line[:limit])
            line = line[limit:]
        if len(cur) + len(line) + 1 > limit:
            parts.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        parts.append(cur)
    return parts


async def answer(event: Message | CallbackQuery, lang: str, text: str, kb=None, *, edit: bool = False) -> Message | None:
    """Send (or edit, for callbacks) an HTML message with RTL handling and automatic chunking."""
    text = rtl_wrap(lang, text)
    msg = event.message if isinstance(event, CallbackQuery) else event
    chunks = split_chunks(text)
    if edit and isinstance(event, CallbackQuery) and len(chunks) == 1 and event.message:
        try:
            return await event.message.edit_text(chunks[0], reply_markup=kb)
        except Exception:
            pass  # not modified / too old → send new
    last = None
    for i, ch in enumerate(chunks):
        last = await msg.answer(ch, reply_markup=kb if i == len(chunks) - 1 else None)
    return last


async def need_user(event: Message | CallbackQuery, user, lang: str) -> bool:
    if user is None:
        if isinstance(event, CallbackQuery):
            await event.answer(t(lang, "err_not_registered"), show_alert=True)
        else:
            await event.answer(t(lang, "err_not_registered"))
        return False
    return True


async def need_perm(svc, event: Message | CallbackQuery, user, lang: str, permission: str) -> bool:
    """Backend authorisation check (buttons being hidden is never relied on)."""
    if not await need_user(event, user, lang):
        return False
    if await svc.users.has(user, permission):
        return True
    if isinstance(event, CallbackQuery):
        await event.answer(t(lang, "err_permission"), show_alert=True)
    else:
        await event.answer(t(lang, "err_permission"))
    return False


def is_private(event: Message | CallbackQuery) -> bool:
    msg = event.message if isinstance(event, CallbackQuery) else event
    return bool(msg and msg.chat.type == "private")


def pager(prefix: str, page: int, pages: int, lang: str) -> list[InlineKeyboardButton]:
    row = []
    if page > 0:
        row.append(btn("◀️", f"{prefix}{page - 1}"))
    row.append(btn(f"{page + 1}/{max(1, pages)}", "x:noop"))
    if page + 1 < pages:
        row.append(btn("▶️", f"{prefix}{page + 1}"))
    return row
