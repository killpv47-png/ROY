"""Task controls (pause/resume/cancel/view), agent status, and rendering of completed task results."""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from ...agent.notifier import STATUS_ICON
from ...i18n import button_texts, rtl_wrap, t
from .. import views
from ..common import answer, btn, esc, ikb, main_keyboard, need_user, split_chunks

log = logging.getLogger(__name__)
router = Router(name="results")


# ---------------------------------------------------------------------------- task controls
@router.callback_query(F.data.startswith("tk:"))
async def cb_task(c: CallbackQuery, svc, user, lang):
    if not await need_user(c, user, lang):
        return
    parts = c.data.split(":", 2)
    if len(parts) != 3:
        await c.answer(t(lang, "err_invalid_action"), show_alert=True)
        return
    _, action, tid = parts
    tv = await svc.tasks.get(tid)
    is_admin = user.role in ("owner", "admin")
    if not tv or (tv.user_id != user.id and not is_admin):
        await c.answer(t(lang, "err_task_not_found"), show_alert=True)
        return
    scope = None if is_admin else user.id
    if action == "cancel":
        ok = await svc.tasks.request_cancel(tid, scope)
        await c.answer(t(lang, "task_cancel_requested" if ok else "task_action_unavailable"), show_alert=not ok)
    elif action == "pause":
        ok = await svc.tasks.request_pause(tid, scope)
        await c.answer(t(lang, "task_pause_requested" if ok else "task_action_unavailable"), show_alert=not ok)
    elif action == "resume":
        ok = await svc.tasks.resume(tid, scope)
        if ok:
            svc.orchestrator.wake()
        await c.answer(t(lang, "task_resumed" if ok else "task_action_unavailable"), show_alert=not ok)
    elif action == "view":
        await c.answer()
    else:
        await c.answer(t(lang, "err_invalid_action"), show_alert=True)
        return
    tv = await svc.tasks.get(tid)
    await svc.notifier.publish(lang, tv, force=True)
    if action == "view":
        events = await svc.tasks.events(tid, 12)
        lines = [f"🧾 <b>{t(lang, 'task_log')}</b> <code>{tid[:8]}</code>"]
        for e in events:
            lines.append(f"<code>{e.ts:%H:%M:%S}</code> {esc(e.event)} — {esc(e.message[:90])}")
        await answer(c, lang, "\n".join(lines))


@router.message(Command("cancel"))
async def cmd_cancel(m: Message, svc, user, lang, state: FSMContext):
    await state.clear()
    if not await need_user(m, user, lang):
        return
    active = await svc.tasks.list_for_user(user.id, active_only=True)
    n = 0
    for tv in active:
        if await svc.tasks.request_cancel(tv.id, user.id):
            n += 1
    await m.answer(t(lang, "cancelled_n", n=n), reply_markup=main_keyboard(lang, user.role))


@router.message(F.text.in_(button_texts("btn_agent")))
async def btn_agent(m: Message, svc, user, lang):
    if not await need_user(m, user, lang):
        return
    tasks = await svc.tasks.list_for_user(user.id, limit=8)
    lines = [f"🤖 <b>{t(lang, 'btn_agent')}</b>",
             t(lang, "agent_caps", tools=len(svc.registry.names()), skills=len(await svc.skills.index()),
               llm=t(lang, "on") if svc.llm else t(lang, "off"), search=svc.web.provider)]
    rows = []
    for tv in tasks:
        icon = STATUS_ICON.get(tv.status, "•")
        lines.append(f"{icon} <code>{tv.id[:8]}</code> {t(lang, 'task_' + tv.kind)} · {tv.progress}%")
        rows.append([btn(f"{icon} {t(lang, 'task_' + tv.kind)} {tv.id[:6]}", f"tk:view:{tv.id}")])
    if not tasks:
        lines.append(t(lang, "no_tasks"))
    await answer(m, lang, "\n".join(lines), ikb(rows[:8]))


# ---------------------------------------------------------------------------- result sections
async def _result(c: CallbackQuery, svc, user, lang, tid: str) -> dict | None:
    tv = await svc.tasks.get(tid)
    if not tv or (tv.user_id != user.id and user.role not in ("owner", "admin")):
        await c.answer(t(lang, "err_task_not_found"), show_alert=True)
        return None
    if tv.status != "COMPLETED" or not tv.result:
        await c.answer(t(lang, "result_unavailable"), show_alert=True)
        return None
    return tv.result


SCAN_SECTIONS = {
    "col": views.section_collection, "upg": views.section_upgrades, "av": views.section_availability,
    "clan": views.section_clan,
    "bat": lambda L, x: views.battles_text(L, x.get("battles_history") or x.get("battles_recent") or {}),
}


@router.callback_query(F.data.startswith("rs:"))
async def cb_scan_section(c: CallbackQuery, svc, user, lang):
    if not await need_user(c, user, lang):
        return
    parts = c.data.split(":", 2)
    if len(parts) != 3 or parts[1] not in SCAN_SECTIONS:
        await c.answer(t(lang, "err_invalid_action"), show_alert=True)
        return
    r = await _result(c, svc, user, lang, parts[2])
    if r is None:
        return
    await c.answer()
    await answer(c, lang, SCAN_SECTIONS[parts[1]](lang, r), views.scan_keyboard(lang, parts[2]))


@router.callback_query(F.data.startswith(("dk:", "dki:", "dks:")))
async def cb_deck_nav(c: CallbackQuery, svc, user, lang):
    if not await need_user(c, user, lang):
        return
    try:
        kind, idx, tid = c.data.split(":", 2)
        idx = int(idx)
    except ValueError:
        await c.answer(t(lang, "err_invalid_action"), show_alert=True)
        return
    r = await _result(c, svc, user, lang, tid)
    if r is None:
        return
    if not 0 <= idx < len(r.get("decks", [])):
        await c.answer(t(lang, "err_invalid_action"), show_alert=True)
        return
    if kind == "dk":
        await c.answer()
        await answer(c, lang, views.deck_text(lang, r, idx), views.deck_keyboard(lang, r, idx, tid), edit=True)
    elif kind == "dks":
        await c.answer()
        await answer(c, lang, views.subs_text(lang, r["decks"][idx]))
    else:
        await c.answer(t(lang, "rendering_image"))
        png = await deck_png(svc, lang, r["decks"][idx])
        caption = ", ".join(f"{x['name']} {x['level']}" for x in r["decks"][idx]["cards"])
        await c.message.answer_photo(BufferedInputFile(png, filename=f"deck_{idx + 1}.png"), caption=esc(caption)[:1000])


@router.callback_query(F.data.startswith(("dkc:", "dku:")))
async def cb_deck_extra(c: CallbackQuery, svc, user, lang):
    if not await need_user(c, user, lang):
        return
    kind, tid = c.data.split(":", 1)
    r = await _result(c, svc, user, lang, tid)
    if r is None:
        return
    await c.answer()
    await answer(c, lang, views.compare_text(lang, r) if kind == "dkc" else views.upgrades_text(lang, r))


def meta_kb(lang: str, page: int, pages: int, tid: str):
    nav = []
    if page > 0:
        nav.append(btn("◀️", f"mp:{page - 1}:{tid}"))
    nav.append(btn(f"{page + 1}/{pages}", "x:noop"))
    if page + 1 < pages:
        nav.append(btn("▶️", f"mp:{page + 1}:{tid}"))
    return ikb([nav, [btn(t(lang, "btn_deck_builder"), "dmenu")]])


@router.callback_query(F.data.startswith("mp:"))
async def cb_meta_page(c: CallbackQuery, svc, user, lang):
    if not await need_user(c, user, lang):
        return
    try:
        _, page, tid = c.data.split(":", 2)
        page = int(page)
    except ValueError:
        await c.answer(t(lang, "err_invalid_action"), show_alert=True)
        return
    r = await _result(c, svc, user, lang, tid)
    if r is None:
        return
    text, pages = views.meta_text(lang, r, page)
    await c.answer()
    await answer(c, lang, text, meta_kb(lang, page, pages, tid), edit=True)


@router.callback_query(F.data == "x:noop")
async def cb_noop(c: CallbackQuery):
    await c.answer()


# ---------------------------------------------------------------------------- delivery
async def deck_png(svc, lang: str, d: dict) -> bytes:
    from ...decks.render import render_deck
    catalog = await svc.kb.catalog()
    for card in d["cards"]:
        cat = catalog.get(card["id"], {})
        card.setdefault("icon_url", cat.get("icon_url"))
        card.setdefault("evo_icon_url", cat.get("evo_icon_url"))
    title = f"{t('en', 'cat_' + d['category'])} #{d['rank']}"
    subtitle = f"Avg elixir {d['avg_elixir']:.2f} | Score {d['score']:.1f}/100 | Cycle {d['cycle_cost']}"
    return await render_deck(d, title, subtitle, svc.settings.cache_dir)


async def deliver_result(bot, svc, tv, result: dict, lang: str) -> None:
    """Orchestrator result hook → final rendered message(s)."""
    async def send(text, kb=None):
        for i, chunk in enumerate(split_chunks(rtl_wrap(lang, text))):
            await bot.send_message(tv.chat_id, chunk, reply_markup=kb if i == 0 else None)

    if tv.kind == "scan":
        await send(views.scan_summary(lang, result), views.scan_keyboard(lang, tv.id))
    elif tv.kind == "deck":
        await send(views.deck_text(lang, result, 0), views.deck_keyboard(lang, result, 0, tv.id))
        try:
            png = await deck_png(svc, lang, result["decks"][0])
            await bot.send_photo(tv.chat_id, BufferedInputFile(png, filename="deck.png"))
            await svc.tasks.event(tv.id, "file", "deck image generated")
        except Exception as e:
            log.warning("deck image failed: %s", type(e).__name__)
    elif tv.kind == "battles":
        await send(views.battles_text(lang, result))
    elif tv.kind == "meta":
        text, pages = views.meta_text(lang, result, 0)
        await send(text, meta_kb(lang, 0, pages, tv.id))
    elif tv.kind == "api_check":
        await send(views.api_check_text(lang, result))
    elif tv.kind == "ask":
        await send(views.ask_text(lang, result))
