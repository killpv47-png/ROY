"""Formatting of agent results into compact, localised Telegram HTML (no oversized messages)."""
from __future__ import annotations

from datetime import datetime

from ..i18n import fmt_date, fmt_number, fmt_pct, t
from .common import btn, esc, ikb

RARITY_ICON = {"common": "⚪", "rare": "🟠", "epic": "🟣", "legendary": "🔷", "champion": "👑"}
CATEGORIES = ["defensive", "offensive", "balanced", "cycle", "counter_attack", "high_level", "meta", "custom"]
CAT_ICON = {"defensive": "🛡", "offensive": "⚔️", "balanced": "⚖️", "cycle": "🔄", "counter_attack": "↩️",
            "high_level": "⬆️", "meta": "📈", "custom": "🎯"}


def _dt(v) -> datetime | None:
    if isinstance(v, datetime):
        return v
    try:
        return datetime.fromisoformat(str(v))
    except (TypeError, ValueError):
        return None


def n(lang, v, d=None):
    return "—" if v is None else fmt_number(lang, v, d)


def tr_avail(lang: str, value: str) -> str:
    v = (value or "").lower()
    extra = ""
    if "(" in value:
        extra = " (" + value.split("(", 1)[1]
    if v.startswith("retrieved"):
        return "✅ " + t(lang, "av_retrieved") + extra
    if v.startswith("stored"):
        return "💾 " + t(lang, "av_stored")
    if "not exposed" in v:
        return "➖ " + t(lang, "av_not_exposed")
    if "endpoint not available" in v:
        return "➖ " + t(lang, "av_endpoint_na")
    if "not in a clan" in v:
        return "➖ " + t(lang, "av_no_clan")
    return "⚠️ " + t(lang, "av_unavailable") + extra


# ---------------------------------------------------------------------------- scan
def scan_summary(lang: str, r: dict) -> str:
    p, c = r["profile"], r["collection"]
    lines = [f"🏰 <b>{esc(p.get('name'))}</b> <code>{esc(p.get('tag'))}</code>",
             f"🏆 {t(lang, 'f_trophies')}: <b>{n(lang, p.get('trophies'))}</b> · {t(lang, 'f_best')}: {n(lang, p.get('best_trophies'))}",
             f"🏟 {t(lang, 'f_arena')}: {esc(p.get('arena') or '—')} · ⭐ {t(lang, 'f_level')}: {n(lang, p.get('exp_level'))}"]
    if p.get("wins") is not None:
        lines.append(f"⚔️ {t(lang, 'f_wins')}: {n(lang, p.get('wins'))} · {t(lang, 'f_losses')}: {n(lang, p.get('losses'))}"
                     f" · 👑 {n(lang, p.get('three_crown_wins'))}")
    if p.get("clan"):
        lines.append(f"🛡 {esc(p['clan'])} ({esc(p.get('clan_role') or '')})")
    lines += ["", f"🃏 <b>{t(lang, 'sec_collection')}</b>: {n(lang, c['total'])} · {t(lang, 'f_avg_level')}: "
              f"<b>{n(lang, c.get('avg_level'), 2)}</b> · {t(lang, 'f_maxed')}: {n(lang, c.get('maxed'))}"]
    if c.get("evolutions_unlocked"):
        lines.append(f"🧬 {t(lang, 'f_evolutions')}: {esc(', '.join(c['evolutions_unlocked'][:8]))}")
    br = r.get("battles_recent") or {}
    if br.get("sample_size"):
        lines.append(f"📊 {t(lang, 'f_recent_battles', n=br['sample_size'])}: {fmt_pct(lang, br['win_rate'])} "
                     f"({n(lang, br['wins'])}-{n(lang, br['losses'])}-{n(lang, br['draws'])})")
    if r.get("progress"):
        pr = r["progress"]
        lines.append(f"📈 {t(lang, 'f_since_last')}: 🏆 {pr['trophies_delta']:+d} · ⭐ {pr['exp_delta']:+d} · "
                     f"Ø {pr['avg_level_delta']:+.2f}")
    if r.get("ai_summary"):
        lines += ["", f"🤖 <b>{t(lang, 'ai_insights')}</b>", esc(r["ai_summary"])]
    unavailable = [k for k, v in (r.get("availability") or {}).items() if not str(v).startswith(("retrieved", "stored"))]
    lines += ["", f"ℹ️ {t(lang, 'scan_data_note', n=len(unavailable))}"]
    return "\n".join(lines)


def scan_keyboard(lang: str, task_id: str):
    return ikb([[btn(t(lang, "sec_collection"), f"rs:col:{task_id}"), btn(t(lang, "sec_upgrades"), f"rs:upg:{task_id}")],
                [btn(t(lang, "sec_battles"), f"rs:bat:{task_id}"), btn(t(lang, "sec_availability"), f"rs:av:{task_id}")],
                [btn(t(lang, "sec_clan"), f"rs:clan:{task_id}"), btn(t(lang, "btn_deck_builder"), "dmenu")]])


def section_collection(lang: str, r: dict) -> str:
    c = r["collection"]
    lines = [f"🃏 <b>{t(lang, 'sec_collection')}</b> (max {n(lang, c['global_max_level'])})"]
    for rar, d in sorted(c.get("by_rarity", {}).items()):
        lines.append(f"{RARITY_ICON.get(rar, '▫️')} {t(lang, 'rar_' + rar)}: {n(lang, d['count'])} · Ø {n(lang, d['avg_level'], 2)}")
    lines.append("")
    lines.append("📶 " + " · ".join(f"L{lvl}: {n(lang, cnt)}" for lvl, cnt in list(c.get("level_histogram", {}).items())[:8]))
    lines.append("🔝 " + esc(", ".join(f"{x['name']} {x['level']}" for x in c.get("top_cards", [])[:10])))
    if c.get("evolvable_not_unlocked"):
        lines.append(f"🧬 {t(lang, 'f_evo_available')}: {esc(', '.join(c['evolvable_not_unlocked'][:10]))}")
    if not c.get("evolution_data_available"):
        lines.append(f"ℹ️ {t(lang, 'evo_not_exposed')}")
    if c.get("champions"):
        lines.append(f"👑 {esc(', '.join(c['champions']))}")
    if c.get("tower_troops"):
        lines.append("🗼 " + esc(", ".join(f"{x['name']} {x['level']}" for x in c["tower_troops"])))
    return "\n".join(lines)


def section_upgrades(lang: str, r: dict) -> str:
    lines = [f"⬆️ <b>{t(lang, 'sec_upgrades')}</b>"]
    for i, u in enumerate(r.get("upgrade_priorities", [])[:8], 1):
        why = ", ".join(t(lang, "why_" + x) for x in u.get("reasons", []))
        lines.append(f"{i}. {RARITY_ICON.get(u.get('rarity') or '', '▫️')} {esc(u['name'])} — L{u['level']}"
                     + (f" <i>({why})</i>" if why else ""))
    if r.get("current_deck"):
        lines += ["", f"🎴 {t(lang, 'f_current_deck')}: " + esc(", ".join(f"{c['name']} {c['level']}" for c in r["current_deck"]))]
    return "\n".join(lines)


def section_availability(lang: str, r: dict) -> str:
    lines = [f"🔍 <b>{t(lang, 'sec_availability')}</b>"]
    for k, v in (r.get("availability") or {}).items():
        lines.append(f"• {t(lang, 'avk_' + k)}: {tr_avail(lang, str(v))}")
    p = r.get("profile", {})
    if p.get("unknown_fields"):
        lines.append(f"🆕 {t(lang, 'unknown_fields')}: <code>{esc(', '.join(p['unknown_fields'][:10]))}</code>")
    lines += ["", t(lang, "provenance_note"),
              f"💾 {t(lang, 'stored_battles', n=r.get('stored_battles_total', 0), new=r.get('new_battles_stored', 0))}"]
    return "\n".join(lines)


def section_clan(lang: str, r: dict) -> str:
    c = r.get("clan")
    if not c:
        return f"🛡 {t(lang, 'av_no_clan')}"
    return (f"🛡 <b>{esc(c.get('name'))}</b> <code>{esc(c.get('tag'))}</code>\n"
            f"👥 {n(lang, c.get('members'))}/50 · 🏆 {n(lang, c.get('clanScore'))} · ⚔️ {n(lang, c.get('clanWarTrophies'))}\n"
            f"📍 {esc(c.get('location') or '—')} · 🎁 {n(lang, c.get('donationsPerWeek'))}/wk")


# ---------------------------------------------------------------------------- battles
def battles_text(lang: str, b: dict) -> str:
    if not b or not b.get("sample_size"):
        return f"📊 {t(lang, 'no_battles')}"
    per = b.get("period") or {}
    lines = [f"📊 <b>{t(lang, 'sec_battles')}</b> — {t(lang, 'sample', n=b['sample_size'])}",
             f"🗓 {fmt_date(lang, _dt(per.get('from')))} → {fmt_date(lang, _dt(per.get('to')))}",
             f"✅ {n(lang, b['wins'])} · ❌ {n(lang, b['losses'])} · ➖ {n(lang, b['draws'])} · "
             f"<b>{fmt_pct(lang, b['win_rate'])}</b>"]
    if b.get("trophy_net") is not None:
        lines.append(f"🏆 {t(lang, 'trophy_net')}: {b['trophy_net']:+d}")
    tr = b.get("trend") or {}
    if tr.get("last_10_games"):
        lines.append(f"📈 {t(lang, 'last10', w=tr['last_10_wins'], n=tr['last_10_games'])}")
    if b.get("by_mode"):
        lines += ["", f"🎮 <b>{t(lang, 'by_mode')}</b>"]
        for m, d in list(b["by_mode"].items())[:5]:
            lines.append(f"• {esc(m)}: {n(lang, d['games'])} · {fmt_pct(lang, d['win_rate'])}")
    if b.get("problem_cards"):
        lines += ["", f"⚠️ <b>{t(lang, 'problem_cards')}</b>"]
        for p in b["problem_cards"][:5]:
            lines.append(f"• {esc(p['name'])}: {t(lang, 'problem_line', losses=p['losses_with'], seen=p['seen'], rate=fmt_pct(lang, p['loss_rate_with']))}")
    if b.get("deck_performance"):
        d = b["deck_performance"][0]
        lines += ["", f"🎴 {t(lang, 'main_deck')}: {fmt_pct(lang, d['win_rate'])} ({n(lang, d['games'])})",
                  f"<i>{esc(', '.join(d['cards']))}</i>"]
    lv = b.get("level_differential")
    if lv:
        lines.append(f"📶 {t(lang, 'level_diff')}: {lv['avg_level_diff']:+.2f} · {t(lang, 'underleveled_losses', n=lv['losses_while_underleveled'])}")
    for cs in (b.get("counter_suggestions") or [])[:3]:
        lines.append(f"💡 {t(lang, 'counter_tip', threat=t(lang, 'role_' + cs['threat_role']), role=t(lang, 'role_' + cs['answer_role']))}: "
                     f"{esc(', '.join(cs['owned_candidates'][:4]))}")
    if b.get("ai_summary"):
        lines += ["", f"🤖 <b>{t(lang, 'ai_insights')}</b>", esc(b["ai_summary"])]
    lines += ["", f"ℹ️ {t(lang, 'battle_limits')}"]
    return "\n".join(lines)


# ---------------------------------------------------------------------------- decks
def deck_text(lang: str, data: dict, idx: int) -> str:
    d = data["decks"][idx]
    evo = set(d.get("evolution_ids") or [])
    lines = [f"{CAT_ICON.get(d['category'], '🎴')} <b>{t(lang, 'cat_' + d['category'])}</b> — #{d['rank']}/{len(data['decks'])}",
             f"⭐ {t(lang, 'score')}: <b>{n(lang, d['score'], 1)}</b>/100 · 💧 {n(lang, d['avg_elixir'], 2)} · "
             f"🔄 {n(lang, d['cycle_cost'])}"]
    if d.get("archetype"):
        lines.append(f"🧩 {t(lang, 'archetype')}: {t(lang, 'arch_' + d['archetype'])}")
    lines.append("")
    for c in d["cards"]:
        mark = " 🧬" if c["id"] in evo else ""
        lines.append(f"{RARITY_ICON.get(c.get('rarity') or '', '▫️')} {esc(c['name'])} — L{c['level']} · 💧{c.get('elixir') if c.get('elixir') is not None else '?'}{mark}")
    if d.get("strengths"):
        lines += ["", "✅ " + ", ".join(t(lang, "q_" + s) for s in d["strengths"][:6])]
    if d.get("weaknesses"):
        lines.append("⚠️ " + ", ".join(t(lang, "q_" + s) for s in d["weaknesses"][:6]))
    if idx == 0 and data.get("ai_explanation"):
        lines += ["", f"🤖 <b>{t(lang, 'why_deck')}</b>", esc(data["ai_explanation"])]
    if d.get("uncertainty"):
        lines.append(f"ℹ️ {t(lang, 'roles_inferred', n=len(d['uncertainty']))}")
    if evo:
        lines.append(f"🧬 {t(lang, 'evo_manual')}")
    if not d.get("share_link"):
        lines.append(f"🔗 {t(lang, 'no_share_link')}")
    if data.get("snapshot_age_hours", 0) > 24:
        lines.append(f"🕒 {t(lang, 'snapshot_age', h=round(data['snapshot_age_hours']))}")
    return "\n".join(lines)


def deck_keyboard(lang: str, data: dict, idx: int, task_id: str):
    d = data["decks"][idx]
    nav = []
    if idx > 0:
        nav.append(btn("◀️", f"dk:{idx - 1}:{task_id}"))
    nav.append(btn(f"{idx + 1}/{len(data['decks'])}", "x:noop"))
    if idx + 1 < len(data["decks"]):
        nav.append(btn("▶️", f"dk:{idx + 1}:{task_id}"))
    rows = [nav, [btn(t(lang, "deck_image"), f"dki:{idx}:{task_id}"), btn(t(lang, "deck_subs"), f"dks:{idx}:{task_id}")],
            [btn(t(lang, "deck_compare"), f"dkc:{task_id}"), btn(t(lang, "deck_upgrades"), f"dku:{task_id}")],
            [btn(t(lang, "deck_copy_list"), copy=", ".join(c["name"] for c in d["cards"]))]]
    if d.get("share_link"):
        rows.append([btn(t(lang, "deck_open_game"), url=d["share_link"])])
    rows.append([btn(t(lang, "deck_regen"), f"dc:{d['category']}"), btn(t(lang, "btn_deck_builder"), "dmenu")])
    return ikb(rows)


def subs_text(lang: str, d: dict) -> str:
    lines = [f"🔁 <b>{t(lang, 'deck_subs')}</b>"]
    if not d.get("substitutions"):
        lines.append(t(lang, "no_subs"))
    for s in d.get("substitutions", [])[:8]:
        roles = ", ".join(t(lang, "role_" + r) for r in s["shared_roles"][:3])
        lines.append(f"• {esc(s['replace'])} → <b>{esc(s['with'])}</b> L{s['level']} <i>({roles})</i>")
    return "\n".join(lines)


def compare_text(lang: str, data: dict) -> str:
    cmp = data.get("comparison") or {}
    lines = [f"📊 <b>{t(lang, 'deck_compare')}</b>"]
    for row in cmp.get("rows", []):
        lines.append(f"\n#{row['rank']} ⭐ {n(lang, row['score'], 1)} · 💧 {n(lang, row['avg_elixir'], 2)}\n"
                     f"<i>{esc(', '.join(row['cards']))}</i>\n"
                     f"✅ {t(lang, 'f_' + row['best'])} · ⚠️ {t(lang, 'f_' + row['worst'])}")
    winners = cmp.get("factor_winners") or {}
    if winners:
        lines.append("\n🏅 " + " · ".join(f"{t(lang, 'f_' + f)}: #{r}" for f, r in list(winners.items())[:8]))
    return "\n".join(lines)


def upgrades_text(lang: str, data: dict) -> str:
    lines = [f"⬆️ <b>{t(lang, 'deck_upgrades')}</b>"]
    for u in data.get("upgrade_plan", [])[:8]:
        lines.append(f"• {esc(u['name'])}: L{u['level']} → L{u['target']}")
    if data.get("missing_roles"):
        lines.append(f"\n🧩 {t(lang, 'missing_roles')}: " + ", ".join(t(lang, "role_" + r) for r in data["missing_roles"]))
    if data.get("meta_signal_cards"):
        sig = sorted(data["meta_signal_cards"].items(), key=lambda kv: -abs(kv[1]))[:8]
        lines.append(f"\n📈 {t(lang, 'meta_signal')}: " + ", ".join(f"{esc(k)} {'⬆' if v > 0 else '⬇'}" for k, v in sig))
    return "\n".join(lines)


def deck_menu(lang: str):
    rows, row = [], []
    for c in CATEGORIES:
        row.append(btn(f"{CAT_ICON[c]} {t(lang, 'cat_' + c)}", "dcu" if c == "custom" else f"dc:{c}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    rows.append([btn(t(lang, "recent_decks"), "drecent")])
    return ikb(rows)


# ---------------------------------------------------------------------------- meta
def meta_text(lang: str, data: dict, page: int = 0, per_page: int = 10) -> tuple[str, int]:
    changes = data.get("changes") or []
    pages = max(1, (len(changes) + per_page - 1) // per_page)
    s = data.get("summary") or {}
    lines = [f"📈 <b>{t(lang, 'meta_title')}</b>",
             f"🗃 {t(lang, 'kb_summary', cards=s.get('cards', 0), changes=s.get('balance_changes', 0))}"]
    ref = data.get("refresh") or {}
    if ref.get("new_articles"):
        lines.append(f"🆕 {t(lang, 'new_articles', n=len(ref['new_articles']))}")
    lines.append("")
    icon = {"buff": "🟢", "nerf": "🔴", "rework": "🟡", "bugfix": "🔧"}
    for c in changes[page * per_page:(page + 1) * per_page]:
        form = f" ({t(lang, 'form_' + c['form'])})" if c.get("form") in ("evolution", "hero") else ""
        val = f"{esc(c['old'])} → {esc(c['new'])}" if c.get("old") else ""
        pct = f" ({c['pct']:+.0f}%)" if c.get("pct") is not None else ""
        lines.append(f"{icon.get(c['type'], '▫️')} <b>{esc(c['card'])}</b>{form} · {esc(c['attribute'])} {val}{pct} "
                     f"<i>{fmt_date(lang, _dt(c.get('effective')))}</i>")
    if not changes:
        lines.append(t(lang, "no_changes"))
    if page == 0 and data.get("ai_summary"):
        lines += ["", f"🤖 <b>{t(lang, 'ai_insights')}</b>", esc(data["ai_summary"])]
    web = data.get("web_context") or []
    if page == 0 and web:
        lines += ["", f"🌐 <b>{t(lang, 'web_context')}</b>"]
        for w in web[:4]:
            lines.append(f"• [{t(lang, 'tier_' + w.get('tier', 'unverified'))}] <a href=\"{esc(w['url'])}\">{esc(w['title'][:70])}</a>")
    lines += ["", f"📚 {t(lang, 'source_official')}"]
    return "\n".join(lines), pages


# ---------------------------------------------------------------------------- misc
def api_check_text(lang: str, data: dict) -> str:
    eg = data.get("egress") or {}
    lines = [f"🌐 <b>{t(lang, 'task_api_check')}</b>",
             f"• IPv4: <code>{esc(eg.get('ipv4') or '—')}</code> · IPv6: <code>{esc(eg.get('ipv6') or '—')}</code>",
             f"• {t(lang, 'approved_ip')}: <code>{esc(eg.get('approved_ip') or '—')}</code>",
             f"• {t(lang, 'active_proxy')}: {esc(eg.get('active_profile') or t(lang, 'direct'))}"]
    if data.get("api_ok"):
        lines.append(f"✅ {t(lang, 'api_ok', n=(data.get('api') or {}).get('cards', 0))}")
    else:
        code = data.get("api_error") or "unknown"
        key = {"invalid_ip": "err_invalid_ip", "not_configured": "err_no_api_key", "access_denied": "err_access",
               "route_rejected": "err_route"}.get(code, "err_generic")
        lines.append(f"❌ {t(lang, key, code=code)}")
    if eg.get("github_runner") and not eg.get("active_profile"):
        lines.append(f"⚠️ {t(lang, 'gh_dynamic_ip')}")
    return "\n".join(lines)


def ask_text(lang: str, data: dict) -> str:
    if data.get("no_llm"):
        return t(lang, "no_llm")
    out = f"🤖 {esc(data.get('answer') or '')}"
    if data.get("researched"):
        out += f"\n\n🔎 {t(lang, 'researched_note')}"
    if data.get("unverified_numbers"):
        out += f"\n⚠️ {t(lang, 'unverified_numbers', nums=', '.join(data['unverified_numbers'][:5]))}"
    return out


def cards_page(lang: str, cards: list[dict], flt: str, sort: str, page: int, per_page: int = 15):
    items = [c for c in cards if not c.get("is_support")]
    if flt.startswith("r_"):
        items = [c for c in items if (c.get("rarity") or "") == flt[2:]]
    elif flt == "evo":
        items = [c for c in items if (c.get("max_evolution_level") or 0) > 0]
    elif flt == "tower":
        items = [c for c in cards if c.get("is_support")]
    if sort == "name":
        items.sort(key=lambda c: c["name"])
    else:
        items.sort(key=lambda c: (-c["level"], c["name"]))
    pages = max(1, (len(items) + per_page - 1) // per_page)
    page = max(0, min(page, pages - 1))
    lines = [f"🃏 <b>{t(lang, 'btn_cards')}</b> — {t(lang, 'flt_' + flt)} ({n(lang, len(items))})", ""]
    for c in items[page * per_page:(page + 1) * per_page]:
        evo = " 🧬" if (c.get("evolution_level") or 0) > 0 else (" ◌" if (c.get("max_evolution_level") or 0) > 0 else "")
        lines.append(f"{RARITY_ICON.get(c.get('rarity') or '', '▫️')} {esc(c['name'])} — L{c['level']}{evo}")
    lines.append(f"\n🧬 {t(lang, 'evo_legend')}")
    rows = [[btn(("• " if flt == f else "") + t(lang, "flt_" + f), f"cd:{f}:{sort}:0") for f in ("all", "evo", "tower")],
            [btn(("• " if flt == f else "") + RARITY_ICON[f[2:]], f"cd:{f}:{sort}:0")
             for f in ("r_common", "r_rare", "r_epic", "r_legendary", "r_champion")],
            [btn(t(lang, "sort_level") if sort == "name" else t(lang, "sort_name"),
                 f"cd:{flt}:{'level' if sort == 'name' else 'name'}:0")]]
    nav = []
    if page > 0:
        nav.append(btn("◀️", f"cd:{flt}:{sort}:{page - 1}"))
    nav.append(btn(f"{page + 1}/{pages}", "x:noop"))
    if page + 1 < pages:
        nav.append(btn("▶️", f"cd:{flt}:{sort}:{page + 1}"))
    rows.append(nav)
    return "\n".join(lines), ikb(rows)
