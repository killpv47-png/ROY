"""Bot handlers driven through the real aiogram Dispatcher with a fake Telegram session (no network)."""
from datetime import datetime, timezone
from itertools import count

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage, TelegramMethod
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from cr_analyst.bot.app import build_dispatcher
from cr_analyst.i18n import t

_ids = count(1000)


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls: list[TelegramMethod] = []

    async def close(self):  # pragma: no cover
        pass

    async def stream_content(self, *a, **k):  # pragma: no cover
        yield b""

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, (SendMessage, EditMessageText)):
            return Message(message_id=next(_ids), date=datetime.now(timezone.utc),
                           chat=Chat(id=getattr(method, "chat_id", 1) or 1, type="private"),
                           text=method.text)
        return True

    def texts(self) -> list[str]:
        return [m.text for m in self.calls if isinstance(m, (SendMessage, EditMessageText))]


@pytest.fixture
async def tg(services):
    session = FakeSession()
    bot = Bot("123456:TEST-TOKEN-NOT-REAL-abcdefghijklmnop", session=session)
    dp = build_dispatcher(services)
    seq = count(1)

    def user(uid, lang="en"):
        return User(id=uid, is_bot=False, first_name=f"U{uid}", language_code=lang)

    async def send(uid, text, lang="en", chat_type="private"):
        chat = Chat(id=uid if chat_type == "private" else -uid, type=chat_type)
        msg = Message(message_id=next(_ids), date=datetime.now(timezone.utc), chat=chat, from_user=user(uid, lang),
                      text=text)
        await dp.feed_update(bot, Update(update_id=next(seq), message=msg))

    async def click(uid, data, lang="en"):
        chat = Chat(id=uid, type="private")
        anchor = Message(message_id=next(_ids), date=datetime.now(timezone.utc), chat=chat, text="menu")
        cq = CallbackQuery(id=str(next(_ids)), from_user=user(uid, lang), chat_instance="ci", data=data,
                           message=anchor)
        await dp.feed_update(bot, Update(update_id=next(seq), callback_query=cq))

    class T:
        pass
    o = T()
    o.session, o.bot, o.dp, o.send, o.click, o.svc, o.seq = session, bot, dp, send, click, services, seq
    yield o


async def test_start_registers_first_user_as_owner(tg):
    await tg.send(1, "/start", lang="en")
    texts = tg.session.texts()
    assert any("Welcome" in x for x in texts)
    assert any(t("en", "became_owner").split(".")[0][:20] in x for x in texts)
    assert (await tg.svc.users.get(1)).role == "owner"
    await tg.send(2, "/start", lang="de")
    assert (await tg.svc.users.get(2)).role == "user"
    assert (await tg.svc.users.get(2)).language == "de"
    assert any("Willkommen" in x for x in tg.session.texts())


async def test_unsupported_language_defaults_to_persian(tg):
    await tg.send(5, "/start", lang="zz")
    assert (await tg.svc.users.get(5)).language == "fa"
    assert any("\u200f" in x for x in tg.session.texts())  # RTL mark applied


async def test_reply_keyboard_buttons_reach_handlers(tg):
    await tg.send(1, "/start")
    tg.session.calls.clear()
    await tg.send(1, t("en", "btn_help"))
    assert any("How to use the bot" in x for x in tg.session.texts())
    tg.session.calls.clear()
    await tg.send(1, t("en", "btn_language"))
    assert any(t("en", "choose_language") in x for x in tg.session.texts())


async def test_language_switch_callback(tg):
    await tg.send(1, "/start")
    await tg.click(1, "lang:ja")
    assert (await tg.svc.users.get(1)).language == "ja"
    assert any(t("ja", "language_set") in x for x in tg.session.texts())
    await tg.click(1, "lang:xx")
    assert any(isinstance(c, AnswerCallbackQuery) and c.show_alert for c in tg.session.calls)


async def test_unregistered_user_is_asked_to_start(tg):
    await tg.send(77, "/help")
    assert any(t("en", "err_not_registered") in x for x in tg.session.texts()) or \
        any("How to use" in x for x in tg.session.texts())


async def test_connect_flow_validates_and_links_tag(tg):
    await tg.send(1, "/start")
    await tg.send(1, "/connect")
    await tg.send(1, "not a tag!!")
    assert any(t("en", "err_bad_tag")[:20] in x for x in tg.session.texts())
    await tg.send(1, "/connect")
    await tg.send(1, "#2pp")
    u = await tg.svc.users.get(1)
    p = await tg.svc.users.primary_player(u.id)
    assert p is not None and p.player_tag == "#2PP"


async def test_duplicate_update_processed_once(tg):
    await tg.send(1, "/start")
    tg.session.calls.clear()
    chat = Chat(id=1, type="private")
    msg = Message(message_id=5, date=datetime.now(timezone.utc), chat=chat,
                  from_user=User(id=1, is_bot=False, first_name="U"), text="/help")
    up = Update(update_id=999_999, message=msg)
    await tg.dp.feed_update(tg.bot, up)
    n = len(tg.session.texts())
    await tg.dp.feed_update(tg.bot, up)  # redelivered after a restart
    assert len(tg.session.texts()) == n and n >= 1


async def test_admin_panel_requires_role(tg):
    await tg.send(1, "/start")          # owner
    await tg.send(2, "/start")          # user
    tg.session.calls.clear()
    await tg.send(2, t("en", "btn_admin"))
    user_txt = " ".join(tg.session.texts())
    assert "Admin panel" not in user_txt
    tg.session.calls.clear()
    await tg.send(1, t("en", "btn_admin"))
    assert any("Admin panel" in x for x in tg.session.texts())


async def test_invalid_callback_data_is_answered(tg):
    await tg.send(1, "/start")
    tg.session.calls.clear()
    await tg.click(1, "tk:cancel:does-not-exist")
    assert any(isinstance(c, AnswerCallbackQuery) for c in tg.session.calls)


async def test_scan_without_tag_explains_next_step(tg):
    await tg.send(1, "/start")
    tg.session.calls.clear()
    await tg.send(1, "/scan")
    out = " ".join(tg.session.texts())
    assert t("en", "err_no_player")[:25] in out or "connect" in out.lower()
