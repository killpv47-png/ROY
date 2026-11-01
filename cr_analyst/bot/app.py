"""Bot assembly: dispatcher, routers, middlewares, localized command menus and an offset-persisting polling loop.

We run our own getUpdates loop (instead of aiogram's start_polling) so the Telegram offset is owned by the
runner and saved in recovery checkpoints; duplicate deliveries after a crash are dropped by the
processed-update idempotency middleware.
"""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter, TelegramUnauthorizedError
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, BotCommandScopeDefault

from ..i18n import LANGUAGES, t
from .handlers import admin, core, results, settings
from .middleware import IdempotencyMiddleware, UserMiddleware
from .sender import TelegramSender

log = logging.getLogger(__name__)

COMMANDS = ["start", "help", "profile", "connect", "scan", "cards", "battles", "decks", "meta", "analyze",
            "settings", "language", "proxy", "api", "status", "cancel"]
ALLOWED_UPDATES = ["message", "callback_query"]


def build_dispatcher(svc) -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(IdempotencyMiddleware(svc))
    um = UserMiddleware(svc)
    dp.message.middleware(um)
    dp.callback_query.middleware(um)
    # order matters: FSM-state handlers in core/settings/admin are specific; results last for generic callbacks
    routers = (admin.router, settings.router, core.router, results.router)
    for r in routers:  # routers are module singletons: detach from a previous dispatcher (rebuild / tests)
        parent = r.parent_router
        if parent is not None:
            parent.sub_routers.remove(r)
            r._parent_router = None
    dp.include_routers(*routers)
    return dp


def build_bot(token: str) -> Bot:
    return Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))


async def register_commands(bot: Bot) -> None:
    default = [BotCommand(command=c, description=t("en", f"cmd_{c}")[:256]) for c in COMMANDS]
    await bot.set_my_commands(default, scope=BotCommandScopeDefault())
    for lang in LANGUAGES:
        cmds = [BotCommand(command=c, description=t(lang, f"cmd_{c}")[:256]) for c in COMMANDS]
        try:
            await bot.set_my_commands(cmds, scope=BotCommandScopeDefault(), language_code=lang)
        except Exception as e:  # not fatal
            log.warning("set_my_commands(%s) failed: %s", lang, type(e).__name__)


class TelegramGateway:
    def __init__(self, svc, bot: Bot, dp: Dispatcher):
        self.svc = svc
        self.bot = bot
        self.dp = dp
        svc.notifier.sender = TelegramSender(bot)

        async def hook(tv, result, lang):
            try:
                await results.deliver_result(bot, svc, tv, result, lang)
            except Exception as e:
                log.warning("result delivery failed: %s", type(e).__name__)
                await svc.errors.record("telegram", "deliver_result", str(e), tv.user_id)
        svc.orchestrator.result_hook = hook

    async def check(self) -> bool:
        try:
            me = await self.bot.get_me()
            self.svc.bot_ok = True
            self.svc.bot_username = me.username
            await self.bot.delete_webhook(drop_pending_updates=False)
            return True
        except TelegramUnauthorizedError:
            log.error("BOT_TOKEN rejected by Telegram")
            self.svc.bot_ok = False
            return False

    def poller(self, runner):
        async def poll(stop: asyncio.Event):
            await register_commands(self.bot)
            backoff = 1.0
            while not stop.is_set():
                try:
                    updates = await self.bot.get_updates(offset=runner.offset, timeout=25,
                                                         allowed_updates=ALLOWED_UPDATES, request_timeout=35)
                    self.svc.bot_ok = True
                    backoff = 1.0
                except TelegramRetryAfter as e:
                    await asyncio.sleep(min(60, e.retry_after))
                    continue
                except (TelegramNetworkError, asyncio.TimeoutError, OSError) as e:
                    self.svc.bot_ok = False
                    log.warning("getUpdates failed (%s); retry in %.0fs", type(e).__name__, backoff)
                    await asyncio.sleep(backoff)
                    backoff = min(60, backoff * 2)
                    continue
                for upd in updates:
                    if stop.is_set():
                        break  # unprocessed updates stay unconfirmed and are delivered to the next runner
                    try:
                        await self.dp.feed_update(self.bot, upd)
                    except Exception as e:
                        log.exception("update %s failed", upd.update_id)
                        await self.svc.errors.record("telegram", type(e).__name__, str(e))
                    runner.on_update_processed(upd.update_id)
            # confirm processed updates to Telegram before exiting (offset acknowledgement)
            if runner.offset:
                try:
                    await self.bot.get_updates(offset=runner.offset, timeout=0, limit=1)
                except Exception:
                    pass
        return poll

    async def close(self) -> None:
        await self.bot.session.close()
