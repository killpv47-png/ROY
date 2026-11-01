"""Core user flows: /start, help, profile/connect, memory, scan, battles, meta, decks, ask, cards."""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from ...clash.tags import InvalidTag, normalize_tag
from ...i18n import button_texts, fmt_date, fmt_number, t
from .. import views
from ..common import answer, btn, esc, ikb, main_keyboard, need_perm, need_user

log = logging.getLogger(__name__)
router = Router(name="core")


class Flow(StatesGroup):
    connect_tag = State()
    ask_question = State()
    custom_deck = State()


# ---------------------------------------------------------------------------- start / help
@router.message(CommandStart())
async def cmd_start(m: Message, svc, user, lang, state: FSMContext, command: CommandObject | None = None,
                    registration=None):
    await state.clear()
    if user is None:
        await m.answer(t(lang, "err_not_registered"))
        return
    if command and command.args and command.args.startswith("recover_"):
        if m.chat.type != "private":
            await m.answer(t(lang, "private_only"))
            return
        ok = await svc.users.recover_owner(m.from_user.id, command.args[len("recover_"):])
        await m.answer(t(lang, "owner_recovered" if ok else "owner_recover_failed"))
        user = await svc.users.get(m.from_user.id)
    text = t(lang, "welcome", name=esc(m.from_user.first_name or ""))
    if registration and registration.became_owner:
        text += "\n\n" + t(lang, "became_owner")
    player = await svc.users.primary_player(user.id)
    if not player:
        text += "\n\n" + t(lang, "connect_hint")
    await answer(m, lang, text, main_keyboard(lang, user.role))
    if not player:
        await m.answer(t(lang, "connect_prompt"), reply_markup=ikb([[btn(t(lang, "btn_connect"), "connect")]]))


@router.message(Command("help"))
@router.message(F.text.in_(button_texts("btn_help")))
async def cmd_help(m: Message, svc, user, lang):
    await answer(m, lang, t(lang, "help_text"))


# ---------------------------------------------------------------------------- connect / profile
async def _start_connect(event, lang, state: FSMContext):
    await state.set_state(Flow.connect_tag)
    target = event.message if isinstance(event, CallbackQuery) else event
    await target.answer(t(lang, "ask_tag"))


@router.message(Command("connect"))
async def cmd_connect(m: Message, svc, user, lang, state: FSMContext, command: CommandObject):
    if not await need_user(m, user, lang):
        return
    if command.args:
        await _link_tag(m, svc, user, lang, command.args, state)
    else:
        await _start_connect(m, lang, state)


@router.callback_query(F.data == "connect")
async def cb_connect(c: CallbackQuery, svc, user, lang, state: FSMContext):
    await c.answer()
    if await need_user(c, user, lang):
        await _start_connect(c, lang, state)


@router.message(Flow.connect_tag, F.text & ~F.text.startswith("/"))
async def on_tag(m: Message, svc, user, lang, state: FSMContext):
    await _link_tag(m, svc, user, lang, m.text, state)


async def _link_tag(m: Message, svc, user, lang, raw: str, state: FSMContext):
    try:
        tag = normalize_tag(raw)
    except InvalidTag:
        await m.answer(t(lang, "err_bad_tag"))
        return
    await state.clear()
    await svc.scanner_for(user.id).ensure_player(user.id, tag)
    await svc.memory.remember(user.id, "player_tag", tag, importance=9)
    await m.answer(t(lang, "tag_linked", tag=tag), reply_markup=ikb([[btn(t(lang, "btn_scan"), "go:scan")]]))


@router.message(Command("profile"))
@router.message(F.text.in_(button_texts("btn_account")))
async def cmd_profile(m: Message, svc, user, lang):
    if not await need_user(m, user, lang):
        return
    p = await svc.users.primary_player(user.id)
    if not p:
        await m.answer(t(lang, "err_no_player"), reply_markup=ikb([[btn(t(lang, "btn_connect"), "connect")]]))
        return
    snap = await svc.scanner_for(user.id).latest_snapshot(p.id)
    lines = [f"👤 <b>{esc(p.name or '—')}</b> <code>{esc(p.player_tag)}</code>"]
    if snap:
        lines += [f"🏆 {t(lang, 'f_trophies')}: {fmt_number(lang, snap.trophies or 0)} · "
                  f"{t(lang, 'f_best')}: {fmt_number(lang, snap.best_trophies or 0)}",
                  f"🏟 {esc(snap.arena or '—')} · ⭐ {fmt_number(lang, snap.exp_level or 0)}",
                  f"🕒 {t(lang, 'last_scan')}: {fmt_date(lang, snap.taken_at, with_time=True)}"]
    else:
        lines.append(t(lang, "no_scan_yet"))
    mem = await svc.memory.usage(user.id)
    lines.append(f"🧠 {t(lang, 'memory_usage', n=mem['entries'], pct=mem['pct'])}")
    await answer(m, lang, "\n".join(lines), ikb([[btn(t(lang, "btn_scan"), "go:scan"),
                                                  btn(t(lang, "btn_change_tag"), "connect")],
                                                 [btn(t(lang, "btn_memory"), "mem:view")]]))


@router.callback_query(F.data == "mem:view")
async def cb_memory(c: CallbackQuery, svc, user, lang):
    if not await need_user(c, user, lang):
        return
    entries = await svc.memory.entries(user.id)
    lines = [f"🧠 <b>{t(lang, 'btn_memory')}</b>"] + [f"• <b>{esc(e.key)}</b>: {esc(e.content)}" for e in entries]
    if not entries:
        lines.append(t(lang, "memory_empty"))
    await c.answer()
    await answer(c, lang, "\n".join(lines), ikb([[btn(t(lang, "memory_wipe"), "mem:wipe")]]), edit=True)


@router.callback_query(F.data == "mem:wipe")
async def cb_memory_wipe(c: CallbackQuery, svc, user, lang):
    if not await need_user(c, user, lang):
        return
    await c.answer(t(lang, "confirm_q"), show_alert=False)
    await answer(c, lang, t(lang, "memory_wipe_confirm"),
                 ikb([[btn(t(lang, "yes"), "mem:wipe!"), btn(t(lang, "no"), "mem:view")]]), edit=True)


@router.callback_query(F.data == "mem:wipe!")
async def cb_memory_wipe_do(c: CallbackQuery, svc, user, lang):
    if not await need_user(c, user, lang):
        return
    n = await svc.memory.wipe_user(user.id)
    await c.answer(t(lang, "memory_wiped", n=n), show_alert=True)


# ---------------------------------------------------------------------------- agent task submission
async def submit(event, svc, user, lang, kind: str, params: dict | None = None, perm: str = "account.use"):
    if not await need_perm(svc, event, user, lang, perm):
        return None
    target = event.message if isinstance(event, CallbackQuery) else event
    if kind in ("scan", "deck", "battles") and not await svc.users.primary_player(user.id):
        await target.answer(t(lang, "err_no_player"), reply_markup=ikb([[btn(t(lang, "btn_connect"), "connect")]]))
        return None
    active = await svc.tasks.list_for_user(user.id, active_only=True)
    if len(active) >= 3:
        await target.answer(t(lang, "too_many_tasks"))
        return None
    return await svc.orchestrator.submit(user, target.chat.id, kind, params or {}, goal=t(lang, f"task_{kind}"))


@router.message(Command("scan"))
@router.message(F.text.in_(button_texts("btn_scan")))
async def cmd_scan(m: Message, svc, user, lang):
    await submit(m, svc, user, lang, "scan", perm="scan.run")


@router.callback_query(F.data == "go:scan")
async def cb_scan(c: CallbackQuery, svc, user, lang):
    await c.answer()
    await submit(c, svc, user, lang, "scan", perm="scan.run")


@router.message(Command("battles"))
@router.message(F.text.in_(button_texts("btn_battles")))
async def cmd_battles(m: Message, svc, user, lang):
    await submit(m, svc, user, lang, "battles", perm="battles.view")


@router.message(Command("meta"))
@router.message(F.text.in_(button_texts("btn_meta")))
async def cmd_meta(m: Message, svc, user, lang):
    await submit(m, svc, user, lang, "meta", perm="meta.view")


@router.message(Command("decks"))
@router.message(F.text.in_(button_texts("btn_deck_builder")))
async def cmd_decks(m: Message, svc, user, lang):
    if await need_perm(svc, m, user, lang, "decks.generate"):
        await answer(m, lang, t(lang, "deck_menu_title"), views.deck_menu(lang))


@router.callback_query(F.data == "dmenu")
async def cb_dmenu(c: CallbackQuery, svc, user, lang):
    await c.answer()
    if await need_perm(svc, c, user, lang, "decks.generate"):
        await answer(c, lang, t(lang, "deck_menu_title"), views.deck_menu(lang))


DIRECT_DECKS = {"btn_deck_def": "defensive", "btn_deck_off": "offensive", "btn_deck_bal": "balanced"}


@router.message(F.text.in_(button_texts("btn_deck_def") | button_texts("btn_deck_off") | button_texts("btn_deck_bal")))
async def btn_direct_deck(m: Message, svc, user, lang):
    cat = next(c for k, c in DIRECT_DECKS.items() if m.text in button_texts(k))
    await submit(m, svc, user, lang, "deck", {"category": cat}, perm="decks.generate")


@router.callback_query(F.data.startswith("dc:"))
async def cb_deck_cat(c: CallbackQuery, svc, user, lang):
    cat = c.data.split(":", 1)[1]
    if cat not in views.CATEGORIES:
        await c.answer(t(lang, "err_invalid_action"), show_alert=True)
        return
    await c.answer(t(lang, "deck_generating"))
    await submit(c, svc, user, lang, "deck", {"category": cat}, perm="decks.generate")


PRESETS = {"air": {"air_defense": 0.25, "spells": 0.15}, "spell": {"spells": 0.3},
           "lowcost": {"elixir": 0.35}, "evo": {"evolution": 0.3, "level": 0.25}}


@router.callback_query(F.data == "dcu")
async def cb_custom(c: CallbackQuery, svc, user, lang):
    await c.answer()
    rows = [[btn(t(lang, f"preset_{k}"), f"dcp:{k}") for k in ("air", "spell")],
            [btn(t(lang, f"preset_{k}"), f"dcp:{k}") for k in ("lowcost", "evo")],
            [btn(t(lang, "preset_include"), "dcp:include")]]
    await answer(c, lang, t(lang, "custom_title"), ikb(rows))


@router.callback_query(F.data.startswith("dcp:"))
async def cb_custom_preset(c: CallbackQuery, svc, user, lang, state: FSMContext):
    key = c.data.split(":", 1)[1]
    if key == "include":
        await state.set_state(Flow.custom_deck)
        await c.answer()
        await c.message.answer(t(lang, "custom_include_prompt"))
        return
    if key not in PRESETS:
        await c.answer(t(lang, "err_invalid_action"), show_alert=True)
        return
    await c.answer(t(lang, "deck_generating"))
    await submit(c, svc, user, lang, "deck", {"category": "custom", "weights": PRESETS[key]}, perm="decks.generate")


@router.message(Flow.custom_deck, F.text & ~F.text.startswith("/"))
async def on_custom_include(m: Message, svc, user, lang, state: FSMContext):
    await state.clear()
    names = [x.strip().lower() for x in m.text.replace("،", ",").split(",") if x.strip()][:4]
    p = await svc.users.primary_player(user.id)
    coll = (await svc.scanner_for(user.id).latest_collection(p.id))[0] if p else []
    ids = [c["id"] for c in coll if c["name"].lower() in names]
    if not ids:
        await m.answer(t(lang, "custom_none_found"))
        return
    await submit(m, svc, user, lang, "deck", {"category": "custom", "must_include": ids}, perm="decks.generate")


@router.callback_query(F.data == "drecent")
async def cb_recent_decks(c: CallbackQuery, svc, user, lang):
    if not await need_user(c, user, lang):
        return
    decks = await svc.decks.recent(user.id)
    catalog = await svc.kb.catalog()
    lines = [f"🗂 <b>{t(lang, 'recent_decks')}</b>"]
    for d in decks:
        names = ", ".join(catalog.get(i, {}).get("name", str(i)) for i in d.card_ids)
        lines.append(f"\n{views.CAT_ICON.get(d.category, '🎴')} {t(lang, 'cat_' + d.category)} #{d.rank} · "
                     f"⭐ {fmt_number(lang, d.score, 1)} · 💧 {fmt_number(lang, d.avg_elixir, 2)}\n<i>{esc(names)}</i>")
    if not decks:
        lines.append(t(lang, "no_decks_yet"))
    await c.answer()
    await answer(c, lang, "\n".join(lines))


@router.message(Command("analyze"))
@router.message(F.text.in_(button_texts("btn_ask")))
async def cmd_ask(m: Message, svc, user, lang, state: FSMContext, command: CommandObject | None = None):
    if not await need_perm(svc, m, user, lang, "agent.ask"):
        return
    if command and command.args:
        await submit(m, svc, user, lang, "ask", {"question": command.args[:2000]}, perm="agent.ask")
        return
    await state.set_state(Flow.ask_question)
    await m.answer(t(lang, "ask_prompt"))


@router.message(Flow.ask_question, F.text & ~F.text.startswith("/"))
async def on_question(m: Message, svc, user, lang, state: FSMContext):
    await state.clear()
    await submit(m, svc, user, lang, "ask", {"question": m.text[:2000]}, perm="agent.ask")


# ---------------------------------------------------------------------------- cards
@router.message(Command("cards"))
@router.message(F.text.in_(button_texts("btn_cards")))
async def cmd_cards(m: Message, svc, user, lang):
    await _cards(m, svc, user, lang, "all", "level", 0)


@router.callback_query(F.data.startswith("cd:"))
async def cb_cards(c: CallbackQuery, svc, user, lang):
    try:
        _, flt, sort, page = c.data.split(":")
        page = int(page)
    except ValueError:
        await c.answer(t(lang, "err_invalid_action"), show_alert=True)
        return
    valid_flt = {"all", "evo", "tower", "r_common", "r_rare", "r_epic", "r_legendary", "r_champion"}
    if flt not in valid_flt or sort not in ("level", "name"):
        await c.answer(t(lang, "err_invalid_action"), show_alert=True)
        return
    await c.answer()
    await _cards(c, svc, user, lang, flt, sort, page, edit=True)


async def _cards(event, svc, user, lang, flt, sort, page, edit=False):
    if not await need_user(event, user, lang):
        return
    p = await svc.users.primary_player(user.id)
    coll = (await svc.scanner_for(user.id).latest_collection(p.id))[0] if p else []
    if not coll:
        target = event.message if isinstance(event, CallbackQuery) else event
        await target.answer(t(lang, "err_no_snapshot"), reply_markup=ikb([[btn(t(lang, "btn_scan"), "go:scan")]]))
        return
    text, kb = views.cards_page(lang, coll, flt, sort, page)
    await answer(event, lang, text, kb, edit=edit)
