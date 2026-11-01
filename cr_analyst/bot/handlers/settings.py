"""Settings, language, API credential & network (IP / trusted proxy) management, /status."""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from ...i18n import LANGUAGES, button_texts, fmt_date, t
from ...services.credentials import looks_like_clash_key
from ..common import answer, btn, esc, ikb, is_private, main_keyboard, need_perm, need_user
from .core import submit

log = logging.getLogger(__name__)
router = Router(name="settings")


class Cfg(StatesGroup):
    api_key = State()
    proxy_add = State()
    approve_ip = State()


# ---------------------------------------------------------------------------- language
def lang_kb():
    rows, row = [], []
    for code, meta in LANGUAGES.items():
        row.append(btn(f"{meta['flag']} {meta['name']}", f"lang:{code}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return ikb(rows)


@router.message(Command("language"))
@router.message(F.text.in_(button_texts("btn_language")))
async def cmd_language(m: Message, svc, user, lang):
    if await need_user(m, user, lang):
        await answer(m, lang, t(lang, "choose_language"), lang_kb())


@router.callback_query(F.data.startswith("lang:"))
async def cb_lang(c: CallbackQuery, svc, user, lang):
    if not await need_user(c, user, lang):
        return
    code = c.data.split(":", 1)[1]
    if code not in LANGUAGES:
        await c.answer(t(lang, "err_invalid_action"), show_alert=True)
        return
    await svc.users.set_language(user.telegram_id, code)
    await c.answer(t(code, "language_set"))
    await c.message.answer(t(code, "language_set"), reply_markup=main_keyboard(code, user.role))


# ---------------------------------------------------------------------------- settings
def settings_kb(lang: str, st: dict):
    notify = st.get("notify", "normal")
    track = st.get("track_battles", True)
    return ikb([
        [btn(t(lang, "btn_language"), "set:lang")],
        [btn(f"🔔 {t(lang, 'notify_level')}: {t(lang, 'notify_' + notify)}", "set:notify")],
        [btn(f"📊 {t(lang, 'track_battles')}: {t(lang, 'on' if track else 'off')}", "set:track")],
        [btn(t(lang, "btn_api"), "api:menu"), btn(t(lang, "btn_network"), "net:menu")],
        [btn(t(lang, "btn_memory"), "mem:view")],
    ])


@router.message(Command("settings"))
@router.message(F.text.in_(button_texts("btn_settings")))
async def cmd_settings(m: Message, svc, user, lang):
    if await need_user(m, user, lang):
        await answer(m, lang, t(lang, "settings_title"), settings_kb(lang, user.settings or {}))


@router.callback_query(F.data.startswith("set:"))
async def cb_settings(c: CallbackQuery, svc, user, lang):
    if not await need_perm(svc, c, user, lang, "settings.own"):
        return
    action = c.data.split(":", 1)[1]
    st = dict(user.settings or {})
    if action == "lang":
        await c.answer()
        await answer(c, lang, t(lang, "choose_language"), lang_kb())
        return
    if action == "notify":
        order = ["normal", "verbose", "quiet"]
        st = await svc.users.update_settings(user.telegram_id, notify=order[(order.index(st.get("notify", "normal")) + 1) % 3])
    elif action == "track":
        st = await svc.users.update_settings(user.telegram_id, track_battles=not st.get("track_battles", True))
        p = await svc.users.primary_player(user.id)
        if p:
            from ...db.models import PlayerProfile
            async with svc.db.session() as s:
                pp = await s.get(PlayerProfile, p.id)
                pp.track_battles = st["track_battles"]
    else:
        await c.answer(t(lang, "err_invalid_action"), show_alert=True)
        return
    await c.answer(t(lang, "saved"))
    await answer(c, lang, t(lang, "settings_title"), settings_kb(lang, st), edit=True)


# ---------------------------------------------------------------------------- API credentials
async def _api_menu_text(svc, user, lang) -> tuple[str, object]:
    info = await svc.credentials.info(user.id, "clash_api")
    _, origin = await svc.credentials.clash_token_for(user.id)
    lines = [f"🔑 <b>{t(lang, 'btn_api')}</b>", t(lang, "api_explainer"), "",
             f"• {t(lang, 'key_in_use')}: <b>{t(lang, 'origin_' + origin)}</b>"]
    if info:
        lines.append(f"• {t(lang, 'your_key')}: <code>#{info['fingerprint']}</code> · "
                     f"{t(lang, 'status')}: {esc(info.get('last_status') or '—')}")
    rows = [[btn(t(lang, "api_set_key"), "api:set"), btn(t(lang, "api_test"), "api:test")]]
    if info:
        rows.append([btn(t(lang, "api_delete_key"), "api:del")])
    rows.append([btn(t(lang, "net_check_ip"), "net:ip"), btn(t(lang, "net_instructions"), "net:help")])
    return "\n".join(lines), ikb(rows)


@router.message(Command("api"))
async def cmd_api(m: Message, svc, user, lang):
    if not await need_perm(svc, m, user, lang, "credentials.own"):
        return
    text, kb = await _api_menu_text(svc, user, lang)
    await answer(m, lang, text, kb)


@router.callback_query(F.data.startswith("api:"))
async def cb_api(c: CallbackQuery, svc, user, lang, state: FSMContext):
    if not await need_perm(svc, c, user, lang, "credentials.own"):
        return
    action = c.data.split(":", 1)[1]
    if action == "menu":
        await c.answer()
        text, kb = await _api_menu_text(svc, user, lang)
        await answer(c, lang, text, kb, edit=True)
    elif action == "set":
        if not is_private(c):
            await c.answer(t(lang, "private_only"), show_alert=True)
            return
        if not svc.box:
            await c.answer(t(lang, "err_no_encryption"), show_alert=True)
            return
        await state.set_state(Cfg.api_key)
        await c.answer()
        await c.message.answer(t(lang, "api_send_key"))
    elif action == "test":
        await c.answer(t(lang, "testing"))
        await submit(c, svc, user, lang, "api_check", perm="credentials.own")
    elif action == "del":
        ok = await svc.credentials.delete(user.id, "clash_api")
        await c.answer(t(lang, "deleted" if ok else "nothing_to_delete"), show_alert=True)
    else:
        await c.answer(t(lang, "err_invalid_action"), show_alert=True)


@router.message(Cfg.api_key, F.text & ~F.text.startswith("/"))
async def on_api_key(m: Message, svc, user, lang, state: FSMContext):
    await state.clear()
    value = (m.text or "").strip()
    try:
        await m.delete()  # never leave the credential visible in the chat history
    except Exception:
        pass
    if not is_private(m):
        await m.answer(t(lang, "private_only"))
        return
    if not looks_like_clash_key(value):
        await m.answer(t(lang, "api_key_invalid_format"))
        return
    fp = await svc.credentials.set(user.id, "clash_api", value)
    await m.answer(t(lang, "api_key_saved", fp=fp), reply_markup=ikb([[btn(t(lang, "api_test"), "api:test")]]))


# ---------------------------------------------------------------------------- network / egress / proxies
async def _net_text(svc, lang) -> tuple[str, object]:
    st = await svc.network.status()
    lines = [f"🌐 <b>{t(lang, 'btn_network')}</b>",
             f"• {t(lang, 'approved_ip')}: <code>{esc(st['approved_ip'] or '—')}</code>",
             f"• {t(lang, 'active_proxy')}: {esc(st['active_profile'] or t(lang, 'direct'))}"
             + (f" ({esc(st['active_region'])})" if st.get("active_region") else ""),
             f"• {t(lang, 'last_checked_ip')}: <code>{esc(st['last_checked_ip'] or '—')}</code>"]
    if st.get("github_runner") and not st.get("active_profile"):
        lines.append(f"⚠️ {t(lang, 'gh_dynamic_ip')}")
    rows = [[btn(t(lang, "net_check_ip"), "net:ip"), btn(t(lang, "api_test"), "api:test")],
            [btn(t(lang, "net_profiles"), "net:profiles"), btn(t(lang, "net_region"), "net:region")],
            [btn(t(lang, "net_instructions"), "net:help"), btn(t(lang, "refresh"), "net:menu")]]
    return "\n".join(lines), ikb(rows)


@router.message(Command("proxy"))
async def cmd_proxy(m: Message, svc, user, lang):
    if not await need_perm(svc, m, user, lang, "credentials.own"):
        return
    text, kb = await _net_text(svc, lang)
    await answer(m, lang, text, kb)


@router.callback_query(F.data.startswith("net:"))
async def cb_net(c: CallbackQuery, svc, user, lang, state: FSMContext):
    if not await need_perm(svc, c, user, lang, "credentials.own"):
        return
    action = c.data.split(":", 2)[1:]
    a = action[0]
    owner_actions = {"profiles", "padd", "ptest", "pact", "pdel", "direct", "approve", "region"}
    if a in owner_actions and not await svc.users.has(user, "admin.proxy.manage"):
        await c.answer(t(lang, "err_permission"), show_alert=True)
        return
    if a == "menu":
        await c.answer()
        text, kb = await _net_text(svc, lang)
        await answer(c, lang, text, kb, edit=True)
    elif a == "ip":
        await c.answer(t(lang, "checking"))
        info = await svc.network.detect_egress()
        st = await svc.network.status()
        lines = [f"🌍 <b>{t(lang, 'net_check_ip')}</b>",
                 f"• {t(lang, 'runner_ip')} IPv4: <code>{esc(info.ipv4 or '—')}</code>",
                 f"• IPv6: <code>{esc(info.ipv6 or '—')}</code>",
                 f"• {t(lang, 'profile_ip')}: <code>{esc(st.get('profile_ip') or '—')}</code>",
                 f"• {t(lang, 'approved_ip')}: <code>{esc(st['approved_ip'] or '—')}</code>"]
        if info.latency_ms is not None:
            lines.append(f"• ⏱ {info.latency_ms} ms")
        if info.error:
            lines.append(f"⚠️ {esc(info.error)}")
        rows = []
        ip = st.get("profile_ip") or info.ipv4
        if ip:
            rows.append([btn(t(lang, "copy_ip"), copy=ip)])
            if await svc.users.has(user, "admin.proxy.manage"):
                rows.append([btn(t(lang, "approve_this_ip"), f"net:approve:{ip}")])
        rows.append([btn(t(lang, "net_instructions"), "net:help")])
        await answer(c, lang, "\n".join(lines), ikb(rows))
    elif a == "approve" and len(action) > 1:
        try:
            ip = await svc.network.approve_ip(action[1])
            await c.answer(t(lang, "ip_approved", ip=ip), show_alert=True)
        except ValueError:
            await c.answer(t(lang, "err_invalid_action"), show_alert=True)
    elif a == "help":
        await c.answer()
        await answer(c, lang, t(lang, "ip_instructions"))
    elif a == "profiles":
        await c.answer()
        await _profiles(c, svc, lang)
    elif a == "padd":
        if not is_private(c):
            await c.answer(t(lang, "private_only"), show_alert=True)
            return
        await state.set_state(Cfg.proxy_add)
        await c.answer()
        await c.message.answer(t(lang, "proxy_add_prompt"))
    elif a in ("ptest", "pact", "pdel") and len(action) > 1 and action[1].isdigit():
        pid = int(action[1])
        if a == "ptest":
            await c.answer(t(lang, "testing"))
            r = await svc.network.test_profile(pid)
            msg = (t(lang, "proxy_test_ok", ip=r.get("ipv4") or r.get("ipv6"), ms=r.get("latency_ms") or 0,
                     rel=r.get("reliability", 0)) if r["ok"] else t(lang, "proxy_test_fail", err=r.get("error") or "?"))
            await c.message.answer(msg)
        elif a == "pact":
            await c.answer(t(lang, "testing"))
            r = await svc.network.activate_profile(pid)
            await c.message.answer(t(lang, "proxy_activated", ip=r.get("ipv4") or r.get("ipv6"))
                                   if r["ok"] else t(lang, "proxy_test_fail", err=r.get("error")))
        else:
            ok = await svc.network.remove_profile(pid)
            await c.answer(t(lang, "deleted" if ok else "nothing_to_delete"), show_alert=True)
        await _profiles(c, svc, lang)
    elif a == "direct":
        await svc.network.activate_profile(None)
        await c.answer(t(lang, "direct_mode_set"), show_alert=True)
    elif a == "region":
        await c.answer()
        profiles = await svc.network.list_profiles()
        regions = sorted({p.region for p in profiles})
        rows = [[btn(f"📍 {esc(r)}", f"net:profiles")] for r in regions] or [[btn(t(lang, "net_profiles"), "net:profiles")]]
        await answer(c, lang, t(lang, "region_info"), ikb(rows))
    else:
        await c.answer(t(lang, "err_invalid_action"), show_alert=True)


async def _profiles(c: CallbackQuery, svc, lang):
    profiles = await svc.network.list_profiles()
    lines = [f"🧭 <b>{t(lang, 'net_profiles')}</b>"]
    rows = []
    for p in profiles:
        rel = f"{round(p.tests_ok / p.tests_total * 100)}%" if p.tests_total else "—"
        last = fmt_date(lang, p.last_success_at, with_time=True) if p.last_success_at else "—"
        lines.append(f"{'🟢' if p.is_active else '⚪'} <b>{esc(p.name)}</b> · {esc(p.region)} · "
                     f"{esc(p.scheme)}://{esc(p.host)}:{p.port}\n   IP <code>{esc(p.observed_ipv4 or p.observed_ipv6 or '—')}</code>"
                     f" · {p.latency_ms or '—'} ms · {rel} · {last}")
        rows.append([btn(f"🧪 {p.name}", f"net:ptest:{p.id}"), btn("✅", f"net:pact:{p.id}"),
                     btn("🗑", f"net:pdel:{p.id}")])
    if not profiles:
        lines.append(t(lang, "no_profiles"))
    rows.append([btn(t(lang, "proxy_add"), "net:padd"), btn(t(lang, "use_direct"), "net:direct")])
    await answer(c, lang, "\n".join(lines), ikb(rows))


@router.message(Cfg.proxy_add, F.text & ~F.text.startswith("/"))
async def on_proxy_add(m: Message, svc, user, lang, state: FSMContext):
    """Format: name | region | scheme://[user:pass@]host:port"""
    await state.clear()
    raw = (m.text or "").strip()
    try:
        await m.delete()
    except Exception:
        pass
    if not await svc.users.has(user, "admin.proxy.manage"):
        await m.answer(t(lang, "err_permission"))
        return
    from urllib.parse import unquote, urlparse
    try:
        name, region, url = [x.strip() for x in raw.split("|", 2)]
        u = urlparse(url)
        if not u.hostname or not u.port:
            raise ValueError
        p = await svc.network.add_profile(name, region, u.scheme, u.hostname, u.port,
                                          unquote(u.username) if u.username else None,
                                          unquote(u.password) if u.password else None)
    except Exception:
        await m.answer(t(lang, "proxy_add_invalid"))
        return
    await m.answer(t(lang, "proxy_added", name=esc(p.name)),
                   reply_markup=ikb([[btn(f"🧪 {t(lang, 'api_test')}", f"net:ptest:{p.id}")]]))


# ---------------------------------------------------------------------------- status
@router.message(Command("status"))
async def cmd_status(m: Message, svc, user, lang):
    if not await need_user(m, user, lang):
        return
    from ...runtime.health import collect_status
    st = await collect_status(svc, deep=user.role in ("owner", "admin"))
    await answer(m, lang, render_status(lang, st, admin=user.role in ("owner", "admin")))


def render_status(lang: str, st: dict, admin: bool) -> str:
    ok = lambda v: "🟢" if v else "🔴"  # noqa: E731
    lines = [f"🩺 <b>{t(lang, 'status_title')}</b>",
             f"{ok(st['db']['ok'])} {t(lang, 'c_db')}: {esc(st['db']['detail'])}",
             f"{ok(st['telegram'])} Telegram",
             f"{ok(st['clash_api']['ok'])} Clash Royale API: {esc(st['clash_api']['detail'])}",
             f"{'🟢' if st['llm']['enabled'] else '⚪'} LLM: {esc(st['llm']['detail'])}",
             f"🔎 {t(lang, 'c_search')}: {esc(st['search'])}",
             f"🗃 {t(lang, 'kb_summary', cards=st['kb']['cards'], changes=st['kb']['balance_changes'])}",
             f"🤖 {t(lang, 'c_tasks')}: {esc(', '.join(f'{k}={v}' for k, v in st['tasks'].items()) or '—')}"]
    rn = st.get("runner") or {}
    if rn:
        lines.append(f"🏃 {t(lang, 'c_runner')}: {esc(rn.get('mode'))} · {t(lang, 'uptime')} {rn.get('uptime_min', 0)} min"
                     + (f" · {t(lang, 'remaining')} {rn['remaining_min']} min" if rn.get("remaining_min") is not None else ""))
        if rn.get("paused"):
            lines.append(f"⏸ {t(lang, 'runner_paused')}")
    if admin:
        net = st.get("network") or {}
        lines.append(f"🌐 {t(lang, 'approved_ip')}: <code>{esc(net.get('approved_ip') or '—')}</code> · "
                     f"{t(lang, 'active_proxy')}: {esc(net.get('active_profile') or t(lang, 'direct'))}")
        if st.get("errors"):
            lines.append(f"⚠️ {t(lang, 'recent_errors')}: " + ", ".join(f"{e['type']}×{e['count']}" for e in st["errors"][:5]))
    if not st["clash_api"]["ok"] and st["clash_api"].get("hint"):
        lines.append(f"💡 {t(lang, st['clash_api']['hint'])}")
    return "\n".join(lines)
