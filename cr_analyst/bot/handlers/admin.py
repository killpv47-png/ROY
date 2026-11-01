"""Owner / admin control panel. Every action re-checks permissions in the backend."""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from ...agent.notifier import STATUS_ICON
from ...i18n import button_texts, fmt_date, t
from ...services.users import PermissionDenied
from ..common import answer, btn, esc, ikb, is_private, need_perm, pager

log = logging.getLogger(__name__)
router = Router(name="admin")


class Adm(StatesGroup):
    llm_model = State()
    global_key = State()


def admin_kb(lang: str, is_owner: bool):
    rows = [[btn(t(lang, "adm_users"), "adm:users:0"), btn(t(lang, "adm_tasks"), "adm:tasks")],
            [btn(t(lang, "adm_system"), "adm:system"), btn(t(lang, "adm_errors"), "adm:errors")],
            [btn(t(lang, "adm_meta"), "adm:meta")]]
    if is_owner:
        rows += [[btn(t(lang, "adm_runner"), "adm:runner"), btn(t(lang, "btn_network"), "net:menu")],
                 [btn(t(lang, "adm_llm"), "adm:llm"), btn(t(lang, "adm_api"), "adm:api")],
                 [btn(t(lang, "adm_skills"), "adm:skills"), btn(t(lang, "adm_recovery"), "adm:recovery")]]
    return ikb(rows)


@router.message(Command("admin"))
@router.message(F.text.in_(button_texts("btn_admin")))
async def cmd_admin(m: Message, svc, user, lang):
    if not await need_perm(svc, m, user, lang, "admin.status.view"):
        return
    await answer(m, lang, t(lang, "adm_title"), admin_kb(lang, user.role == "owner"))


@router.callback_query(F.data.startswith("adm:"))
async def cb_admin(c: CallbackQuery, svc, user, lang, state: FSMContext):
    if not await need_perm(svc, c, user, lang, "admin.status.view"):
        return
    parts = c.data.split(":")
    a = parts[1]
    owner_only = {"runner", "pause", "resume", "restart", "llm", "llmset", "api", "apiset", "apidel", "skills",
                  "skok", "skno", "recovery", "role"}
    if a in owner_only and user.role != "owner":
        await c.answer(t(lang, "err_permission"), show_alert=True)
        return
    await c.answer()

    if a == "home":
        await answer(c, lang, t(lang, "adm_title"), admin_kb(lang, user.role == "owner"), edit=True)

    elif a == "users":
        page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        users, total = await svc.users.list_users(page * 10, 10)
        cnt = await svc.users.counts()
        lines = [f"👥 <b>{t(lang, 'adm_users')}</b>",
                 t(lang, "user_counts", total=cnt["total"], active=cnt["active"], inactive=cnt["inactive"],
                   admins=cnt["admins"], banned=cnt["banned"], players=cnt["linked_players"]), ""]
        rows = []
        for u in users:
            lines.append(f"• <code>{u.telegram_id}</code> {esc(u.first_name or '')} @{esc(u.username or '—')} · "
                         f"{t(lang, 'role_name_' + u.role)} · {fmt_date(lang, u.last_seen_at)}")
            if user.role == "owner" and u.role != "owner":
                rows.append([btn(f"⭐ {u.telegram_id}", f"adm:role:{u.telegram_id}:admin"),
                             btn("👤", f"adm:role:{u.telegram_id}:user"),
                             btn("⛔", f"adm:role:{u.telegram_id}:banned")])
        rows.append(pager("adm:users:", page, max(1, (total + 9) // 10), lang))
        rows.append([btn("⬅️", "adm:home")])
        await answer(c, lang, "\n".join(lines), ikb(rows), edit=True)

    elif a == "role" and len(parts) == 4:
        try:
            await svc.users.set_role(user.telegram_id, int(parts[2]), parts[3])
            await c.message.answer(t(lang, "role_updated", id=parts[2], role=t(lang, "role_name_" + parts[3])))
        except (PermissionDenied, LookupError, ValueError):
            await c.message.answer(t(lang, "err_permission"))

    elif a == "tasks":
        tasks = await svc.tasks.list_recent(15)
        counts = await svc.tasks.counts()
        lines = [f"🤖 <b>{t(lang, 'adm_tasks')}</b>", esc(", ".join(f"{k}={v}" for k, v in counts.items())), ""]
        rows = []
        for tv in tasks:
            lines.append(f"{STATUS_ICON.get(tv.status, '•')} <code>{tv.id[:8]}</code> u{tv.user_id} "
                         f"{t(lang, 'task_' + tv.kind)} · {tv.progress}% · {fmt_date(lang, tv.created_at, True)}")
            if tv.status not in ("COMPLETED", "FAILED", "CANCELLED"):
                rows.append([btn(f"🧾 {tv.id[:6]}", f"tk:view:{tv.id}"), btn("🚫", f"tk:cancel:{tv.id}")])
        rows.append([btn("⬅️", "adm:home")])
        await answer(c, lang, "\n".join(lines), ikb(rows[:10]), edit=True)

    elif a == "system":
        from ...runtime.health import collect_status
        from .settings import render_status
        st = await collect_status(svc, deep=True)
        await answer(c, lang, render_status(lang, st, admin=True), ikb([[btn("⬅️", "adm:home")]]), edit=True)

    elif a == "errors":
        errs = await svc.errors.summary(12)
        lines = [f"⚠️ <b>{t(lang, 'adm_errors')}</b>"]
        lines += [f"• {esc(e['component'])}/{esc(e['type'])} ×{e['count']} · {esc((e['last'] or '')[:16])}" for e in errs]
        if not errs:
            lines.append(t(lang, "no_errors"))
        await answer(c, lang, "\n".join(lines), ikb([[btn("⬅️", "adm:home")]]), edit=True)

    elif a == "meta":
        auto = await svc.get_config("meta_auto_update", True)
        last = await svc.get_config("meta_last_refresh")
        s = await svc.kb.stats_summary()
        lines = [f"📈 <b>{t(lang, 'adm_meta')}</b>",
                 t(lang, "kb_summary", cards=s["cards"], changes=s["balance_changes"]),
                 f"• {t(lang, 'meta_auto')}: {t(lang, 'on' if auto else 'off')}",
                 f"• {t(lang, 'meta_last')}: {esc(last or '—')}"]
        await answer(c, lang, "\n".join(lines),
                     ikb([[btn(t(lang, "meta_run_now"), "adm:metarun"), btn(t(lang, "meta_toggle"), "adm:metatoggle")],
                          [btn(t(lang, "meta_sync_catalog"), "adm:catalog")], [btn("⬅️", "adm:home")]]), edit=True)

    elif a == "metarun":
        if not await svc.users.has(user, "admin.meta.manage"):
            return
        from .core import submit
        await submit(c, svc, user, lang, "meta", {"force": True}, perm="admin.meta.manage")

    elif a == "metatoggle":
        if not await svc.users.has(user, "admin.meta.manage"):
            return
        cur = await svc.get_config("meta_auto_update", True)
        await svc.set_config("meta_auto_update", not cur)
        await c.message.answer(t(lang, "saved"))

    elif a == "catalog":
        if not await svc.users.has(user, "admin.meta.manage"):
            return
        res = await svc.registry.call("clash_cards_catalog", await _ctx(svc, user, c), {})
        if res.ok:
            await c.message.answer(t(lang, "catalog_synced", n=res.data.get("total", 0)))
        else:
            sec = await svc.kb.bootstrap_secondary_catalog()
            await c.message.answer(t(lang, "catalog_fallback", err=res.error_code, n=sec.get("added", 0)))

    elif a == "runner":
        rs = await svc.runtime_status() if hasattr(svc, "runtime_status") else {}
        paused = await svc.get_config("runner_paused", False)
        lines = [f"🏃 <b>{t(lang, 'adm_runner')}</b>",
                 f"• {t(lang, 'mode')}: {esc(rs.get('mode', '—'))}",
                 f"• {t(lang, 'uptime')}: {rs.get('uptime_min', 0)} min · {t(lang, 'remaining')}: {rs.get('remaining_min', '—')} min",
                 f"• {t(lang, 'auto_restart')}: {t(lang, 'off' if paused else 'on')} · "
                 f"{t(lang, 'chain')}: {rs.get('chain_index', 0)}/{svc.settings.max_auto_restarts}",
                 f"• {t(lang, 'last_dispatch')}: {esc(rs.get('last_dispatch') or '—')}",
                 "", t(lang, "gh_best_effort")]
        await answer(c, lang, "\n".join(lines),
                     ikb([[btn(t(lang, "runner_pause"), "adm:pause"), btn(t(lang, "runner_resume"), "adm:resume")],
                          [btn(t(lang, "runner_restart"), "adm:restart")], [btn("⬅️", "adm:home")]]), edit=True)

    elif a in ("pause", "resume"):
        await svc.set_config("runner_paused", a == "pause")
        await c.message.answer(t(lang, "runner_paused" if a == "pause" else "runner_resumed"))

    elif a == "restart":
        await answer(c, lang, t(lang, "restart_confirm"),
                     ikb([[btn(t(lang, "yes"), "adm:restart!"), btn(t(lang, "no"), "adm:runner")]]))

    elif a == "restart!":
        if user.role != "owner":
            return
        await svc.set_config("restart_requested", True)
        await c.message.answer(t(lang, "restart_scheduled"))

    elif a == "llm":
        cfg = await svc.get_config("llm_settings", {}) or {}
        model = cfg.get("model") or svc.settings.llm_model
        lines = [f"🧠 <b>{t(lang, 'adm_llm')}</b>",
                 f"• LLM: {t(lang, 'on' if svc.llm else 'off')} · {esc(model)}",
                 f"• temperature: {cfg.get('temperature', svc.settings.llm_temperature)} · max_tokens: "
                 f"{cfg.get('max_tokens', svc.settings.llm_max_tokens)}",
                 f"• {t(lang, 'c_search')}: {esc(svc.web.provider)}",
                 f"• {t(lang, 'resume_delay')}: {svc.settings.resume_delay_seconds}s"]
        if svc.llm and hasattr(svc.llm, "usage_total"):
            u = svc.llm.usage_total
            lines.append(f"• {t(lang, 'usage')}: {u['calls']} calls · {u['prompt_tokens']}+{u['completion_tokens']} tokens")
        await answer(c, lang, "\n".join(lines),
                     ikb([[btn(t(lang, "llm_set_model"), "adm:llmset")],
                          [btn("🌡 0.2", "adm:temp:0.2"), btn("🌡 0.4", "adm:temp:0.4"), btn("🌡 0.7", "adm:temp:0.7")],
                          [btn("⬅️", "adm:home")]]), edit=True)

    elif a == "temp" and len(parts) == 3 and user.role == "owner":
        cfg = dict(await svc.get_config("llm_settings", {}) or {})
        cfg["temperature"] = float(parts[2])
        await svc.set_config("llm_settings", cfg)
        await svc.load_runtime_overrides()
        await c.message.answer(t(lang, "saved"))

    elif a == "llmset":
        await state.set_state(Adm.llm_model)
        await c.message.answer(t(lang, "llm_model_prompt"))

    elif a == "api":
        info = await svc.credentials.info(None, "clash_api")
        lines = [f"🔑 <b>{t(lang, 'adm_api')}</b>",
                 f"• {t(lang, 'global_key')}: " + (f"<code>#{info['fingerprint']}</code>" if info else "—"),
                 f"• CLASH_API_TOKEN (env): {t(lang, 'on' if svc.settings.clash_api_token else 'off')}"]
        await answer(c, lang, "\n".join(lines),
                     ikb([[btn(t(lang, "api_set_key"), "adm:apiset"), btn(t(lang, "api_delete_key"), "adm:apidel")],
                          [btn("⬅️", "adm:home")]]), edit=True)

    elif a == "apiset":
        if not is_private(c) or not svc.box:
            await c.message.answer(t(lang, "private_only" if not is_private(c) else "err_no_encryption"))
            return
        await state.set_state(Adm.global_key)
        await c.message.answer(t(lang, "api_send_key"))

    elif a == "apidel":
        ok = await svc.credentials.delete(None, "clash_api")
        await c.message.answer(t(lang, "deleted" if ok else "nothing_to_delete"))

    elif a == "skills":
        idx = await svc.skills.index()
        pending = await svc.skills.pending()
        gate = await svc.skills.write_approval_required()
        lines = [f"📚 <b>{t(lang, 'adm_skills')}</b>"]
        lines += [f"• {esc(s['name'])} v{s['version']} ({esc(s['origin'])}) · {s['usage']}×" for s in idx]
        lines.append(f"\n🔒 {t(lang, 'skill_gate')}: {t(lang, 'on' if gate else 'off')}")
        rows = [[btn(f"✅ {p.name} v{p.version}", f"adm:skok:{p.id}"), btn("❌", f"adm:skno:{p.id}")] for p in pending[:5]]
        rows.append([btn(t(lang, "skill_gate_toggle"), "adm:skgate")])
        rows.append([btn("⬅️", "adm:home")])
        await answer(c, lang, "\n".join(lines), ikb(rows), edit=True)

    elif a in ("skok", "skno") and len(parts) == 3:
        ok = await svc.skills.approve(int(parts[2]), a == "skok")
        await c.message.answer(t(lang, "saved" if ok else "task_action_unavailable"))

    elif a == "skgate" and user.role == "owner":
        await svc.set_config("skills_write_approval", not await svc.skills.write_approval_required())
        await c.message.answer(t(lang, "saved"))

    elif a == "recovery":
        if not is_private(c):
            await c.message.answer(t(lang, "private_only"))
            return
        code = await svc.users.create_recovery_code()
        await c.message.answer(t(lang, "recovery_code", code=code))

    else:
        await c.message.answer(t(lang, "err_invalid_action"))


async def _ctx(svc, user, c):
    from ...agent.tools.registry import ToolContext
    return ToolContext(user_id=user.id, telegram_id=user.telegram_id, role=user.role,
                       permissions=await svc.permissions_for(user), chat_id=c.message.chat.id, language=user.language)


@router.message(Adm.llm_model, F.text & ~F.text.startswith("/"))
async def on_llm_model(m: Message, svc, user, lang, state: FSMContext):
    await state.clear()
    if user.role != "owner":
        return
    model = (m.text or "").strip()[:80]
    cfg = dict(await svc.get_config("llm_settings", {}) or {})
    cfg["model"] = model
    await svc.set_config("llm_settings", cfg)
    await svc.load_runtime_overrides()
    await m.answer(t(lang, "saved"))


@router.message(Adm.global_key, F.text & ~F.text.startswith("/"))
async def on_global_key(m: Message, svc, user, lang, state: FSMContext):
    await state.clear()
    value = (m.text or "").strip()
    try:
        await m.delete()
    except Exception:
        pass
    if user.role != "owner" or m.chat.type != "private":
        return
    from ...services.credentials import looks_like_clash_key
    if not looks_like_clash_key(value):
        await m.answer(t(lang, "api_key_invalid_format"))
        return
    fp = await svc.credentials.set(None, "clash_api", value)
    await m.answer(t(lang, "api_key_saved", fp=fp))


@router.message(Command("owner"))
async def cmd_owner_recover(m: Message, svc, user, lang, command: CommandObject):
    """/owner <recovery-code> — claim ownership with a one-time code generated by the current owner."""
    if user is None or m.chat.type != "private" or not command.args:
        await m.answer(t(lang, "owner_recover_usage"))
        return
    ok = await svc.users.recover_owner(m.from_user.id, command.args.strip())
    try:
        await m.delete()
    except Exception:
        pass
    await m.answer(t(lang, "owner_recovered" if ok else "owner_recover_failed"))
