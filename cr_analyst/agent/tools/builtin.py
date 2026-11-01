"""Built-in agent tools. Every tool returns a structured ToolResult with a provenance `source`."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from ...clash.battles import analyze_battles, counter_suggestions
from ...clash.client import ClashAPIError
from ...clash.tags import normalize_tag
from ...decks.engine import CATEGORIES, DeckEngine, missing_roles, to_deck_cards, upgrade_plan
from .registry import Tool, ToolContext, ToolRegistry, ToolResult

if TYPE_CHECKING:  # pragma: no cover
    from ...app import Services

S = {"type": "string"}
I = {"type": "integer"}


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or [], "additionalProperties": False}


def build_registry(svc: "Services") -> ToolRegistry:
    reg = ToolRegistry()

    async def _primary(ctx: ToolContext):
        p = await svc.users.primary_player(ctx.user_id)
        if not p:
            raise LookupError("no_player_linked")
        return p

    # ------------------------------------------------------------------ Clash Royale API
    async def clash_scan(ctx: ToolContext, a: dict) -> ToolResult:
        tag = a.get("tag") or (await _primary(ctx)).player_tag
        progress = ctx.extras.get("progress")
        res = await svc.scanner_for(ctx.user_id).full_scan(ctx.user_id, tag, progress or svc.noop_progress)
        return ToolResult(True, res, source="official_api+calculated")

    reg.register(Tool("clash_scan", "Full official-API scan of the user's linked player (profile, cards, battles, "
                      "chests, clan) with availability report.", _obj({"tag": S}), clash_scan, "clash",
                      "scan.run", timeout=120))

    async def clash_profile(ctx: ToolContext, a: dict) -> ToolResult:
        tag = normalize_tag(a.get("tag") or (await _primary(ctx)).player_tag)
        p = await svc.clash_for(ctx.user_id).get_player(tag)
        keep = ("tag", "name", "expLevel", "trophies", "bestTrophies", "wins", "losses", "arena", "clan", "role")
        return ToolResult(True, {k: p.get(k) for k in keep if k in p}, source="official_api")

    reg.register(Tool("clash_profile", "Fetch a player's profile summary from the official API.",
                      _obj({"tag": S}), clash_profile, "clash", "account.use"))

    async def clash_cards_catalog(ctx: ToolContext, a: dict) -> ToolResult:
        payload = await svc.clash_for(ctx.user_id).get_cards()
        res = await svc.kb.sync_official_catalog(payload)
        return ToolResult(True, res, source="official_api")

    reg.register(Tool("clash_cards_catalog", "Sync the official /cards catalog into the knowledge base.",
                      _obj({}), clash_cards_catalog, "clash", "account.use"))

    # ------------------------------------------------------------------ knowledge base
    async def card_search(ctx, a):
        return ToolResult(True, await svc.kb.search_cards(a["query"], int(a.get("limit", 8))), source="local_db")

    reg.register(Tool("card_search", "Search the card database by (partial) name.",
                      _obj({"query": S, "limit": I}, ["query"]), card_search, "knowledge", "meta.view"))

    async def card_details(ctx, a):
        d = await svc.kb.card_details(a["name"])
        if d is None:
            return ToolResult(False, error="card not found in database", error_code="not_found", source="local_db")
        return ToolResult(True, d, source="local_db(official+secondary)")

    reg.register(Tool("card_details", "Card info: catalog data, versioned stats with source/verification, and "
                      "official balance history. Never invent values missing here.",
                      _obj({"name": S}, ["name"]), card_details, "knowledge", "meta.view"))

    async def balance_recent(ctx, a):
        return ToolResult(True, await svc.kb.recent_changes(int(a.get("limit", 30))), source="official_blog")

    reg.register(Tool("balance_recent", "Most recent official balance changes (with effective dates and URLs).",
                      _obj({"limit": I}), balance_recent, "knowledge", "meta.view"))

    async def meta_refresh(ctx, a):
        last = await svc.network.get_config("meta_last_refresh")
        force = bool(a.get("force")) and "admin.meta.manage" in ctx.permissions
        if last and not force:
            age_h = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() / 3600
            if age_h < 6:
                return ToolResult(True, {"skipped": "fresh", "age_hours": round(age_h, 1),
                                         "summary": await svc.kb.stats_summary()}, source="local_db")
        rep = await svc.kb.research_official_balance()
        await svc.network.set_config("meta_last_refresh", datetime.now(timezone.utc).isoformat())
        rep["summary"] = await svc.kb.stats_summary()
        return ToolResult(not rep["errors"] or rep["checked"] > 0, rep, source="official_blog",
                          error="; ".join(rep["errors"]) or None)

    reg.register(Tool("meta_refresh", "Check the official Supercell blog for new balance changes and ingest them "
                      "(cached 6h).", _obj({"force": {"type": "boolean"}}), meta_refresh, "research", "meta.view",
                      timeout=120))

    # ------------------------------------------------------------------ analysis
    async def battle_analyze(ctx, a):
        p = await _primary(ctx)
        scanner = svc.scanner_for(ctx.user_id)
        api_err = None
        if a.get("refresh", True):
            try:
                await scanner.poll_battles(p.id, p.player_tag)
            except ClashAPIError as e:
                api_err = e.code
        battles = await scanner.load_battles(p.id, days=a.get("days"))
        analysis = analyze_battles(battles, source="local_history")
        coll, _ = await scanner.latest_collection(p.id)
        analysis["counter_suggestions"] = counter_suggestions(analysis, {c["name"] for c in coll})
        analysis["api_refresh_error"] = api_err
        return ToolResult(True, analysis, source="official_api+local_db+calculated")

    reg.register(Tool("battle_analyze", "Analyse stored + latest battles: win rates, modes, problem cards, deck "
                      "performance, counter suggestions.", _obj({"days": I, "refresh": {"type": "boolean"}}),
                      battle_analyze, "analysis", "battles.view", timeout=90))

    async def deck_generate(ctx, a):
        category = a.get("category", "balanced")
        if category not in CATEGORIES:
            return ToolResult(False, error=f"unknown category; use one of {sorted(CATEGORIES)}",
                              error_code="bad_category")
        p = await _primary(ctx)
        scanner = svc.scanner_for(ctx.user_id)
        coll, snap = await scanner.latest_collection(p.id)
        if not coll:
            return ToolResult(False, error="no card snapshot; run a scan first", error_code="no_snapshot")
        catalog = await svc.kb.catalog()
        cards = to_deck_cards(coll, catalog)
        gmax = (snap.summary or {}).get("collection", {}).get("global_max_level") or max(c.level for c in cards)
        trend = await svc.kb.card_trend()
        battles = await scanner.load_battles(p.id, days=30)
        pressure = analyze_battles(battles).get("loss_role_pressure") if battles else None
        engine = DeckEngine(weights=await svc.deck_weights(), evolution_slots=svc.settings.evolution_slots)
        decks = engine.generate(cards, category, gmax, meta_trend=trend, pressure=pressure,
                                custom=a.get("weights"), top_n=int(a.get("top_n", 3)),
                                must_include=a.get("must_include"), exclude=a.get("exclude"))
        if not decks:
            return ToolResult(False, error="not enough cards to build a deck", error_code="insufficient_cards")
        snap_age_h = (datetime.now(timezone.utc) - (snap.taken_at if snap.taken_at.tzinfo else
                                                    snap.taken_at.replace(tzinfo=timezone.utc))).total_seconds() / 3600
        return ToolResult(True, {
            "category": category, "decks": [d.to_dict() for d in decks], "comparison": engine.compare(decks),
            "upgrade_plan": upgrade_plan(decks, gmax), "missing_roles": missing_roles(cards),
            "global_max_level": gmax, "snapshot_age_hours": round(snap_age_h, 1),
            "meta_signal_cards": {k: v for k, v in trend.items() if v}, "battle_pressure": pressure,
            "weights": engine.category_weights(category, a.get("weights")),
        }, source="calculated(local_db)")

    reg.register(Tool("deck_generate", "Generate and rank personalised decks from the user's real collection for a "
                      "category (defensive|offensive|balanced|cycle|counter_attack|high_level|meta|custom).",
                      _obj({"category": S, "top_n": I, "must_include": {"type": "array", "items": I},
                            "exclude": {"type": "array", "items": I},
                            "weights": {"type": "object", "additionalProperties": {"type": "number"}}}),
                      deck_generate, "decks", "decks.generate", timeout=90))

    async def account_history(ctx, a):
        p = await _primary(ctx)
        from sqlalchemy import select
        from ...db.models import AccountSnapshot
        async with svc.db.session() as s:
            rows = (await s.execute(select(AccountSnapshot).where(AccountSnapshot.player_id == p.id)
                                    .order_by(AccountSnapshot.taken_at.desc()).limit(int(a.get("limit", 5))))).scalars()
            data = [{"taken_at": r.taken_at.isoformat(), "trophies": r.trophies, "best": r.best_trophies,
                     "exp": r.exp_level, "avg_level": (r.summary or {}).get("collection", {}).get("avg_level")}
                    for r in rows]
        return ToolResult(True, data, source="local_db")

    reg.register(Tool("account_history", "The user's own stored account snapshots (progress over time).",
                      _obj({"limit": I}), account_history, "analysis", "account.use"))

    # ------------------------------------------------------------------ research
    async def web_search(ctx, a):
        return ToolResult(True, await svc.web.search(a["query"], int(a.get("limit", 6))), source="web")

    reg.register(Tool("web_search", "Web search (results tagged official/secondary/unverified, cached 6h).",
                      _obj({"query": S, "limit": I}, ["query"]), web_search, "research", "agent.ask", timeout=30))

    async def web_extract(ctx, a):
        return ToolResult(True, await svc.web.extract(a["url"]), source="web")

    reg.register(Tool("web_extract", "Fetch and extract readable text from a public URL (cached 12h).",
                      _obj({"url": S}, ["url"]), web_extract, "research", "agent.ask", timeout=30))

    # ------------------------------------------------------------------ memory & skills & tasks
    async def memory_save(ctx, a):
        changed = await svc.memory.remember(ctx.user_id, a["key"], a["content"], importance=int(a.get("importance", 5)))
        return ToolResult(True, {"saved": changed, "usage": await svc.memory.usage(ctx.user_id)}, source="local_db")

    reg.register(Tool("memory_save", "Save a durable fact about this user (preferences, play style). Small facts only.",
                      _obj({"key": S, "content": S, "importance": I}, ["key", "content"]), memory_save, "memory",
                      "agent.ask"))

    async def memory_forget(ctx, a):
        return ToolResult(True, {"removed": await svc.memory.forget(ctx.user_id, a["key"])}, source="local_db")

    reg.register(Tool("memory_forget", "Remove a user memory entry by key.", _obj({"key": S}, ["key"]),
                      memory_forget, "memory", "agent.ask"))

    async def skill_list(ctx, a):
        return ToolResult(True, await svc.skills.index(), source="local_db")

    reg.register(Tool("skill_list", "List available skills (name + description only).", _obj({}), skill_list,
                      "skills", "agent.ask"))

    async def skill_view(ctx, a):
        sk = await svc.skills.load(a["name"])
        return ToolResult(bool(sk), sk, error=None if sk else "unknown skill", source="local_db")

    reg.register(Tool("skill_view", "Load a skill's full procedure.", _obj({"name": S}, ["name"]), skill_view,
                      "skills", "agent.ask"))

    async def skill_save(ctx, a):
        res = await svc.skills.record_improvement(a["name"], a["content"], description=a.get("description"),
                                                  origin="owner" if ctx.role == "owner" else "agent")
        return ToolResult(True, res, source="local_db")

    reg.register(Tool("skill_save", "Create or improve a reusable skill procedure (versioned; may need owner "
                      "approval).", _obj({"name": S, "content": S, "description": S}, ["name", "content"]),
                      skill_save, "skills", "admin.skills.approve"))

    async def task_status(ctx, a):
        tasks = await svc.tasks.list_for_user(ctx.user_id, limit=5)
        return ToolResult(True, [{"id": t.id[:8], "kind": t.kind, "status": t.status, "progress": t.progress}
                                 for t in tasks], source="local_db")

    reg.register(Tool("task_status", "List the user's recent agent tasks.", _obj({}), task_status, "tasks",
                      "agent.ask"))

    # ------------------------------------------------------------------ network / api
    async def egress_check(ctx, a):
        info = await svc.network.detect_egress()
        st = await svc.network.status()
        return ToolResult(True, {"ipv4": info.ipv4, "ipv6": info.ipv6, "latency_ms": info.latency_ms, **st},
                          source="network")

    reg.register(Tool("egress_check", "Detect the current public egress IPv4/IPv6 and the approved IP status.",
                      _obj({}), egress_check, "network", "credentials.own", timeout=30))

    async def api_test(ctx, a):
        token, origin = await svc.credentials.clash_token_for(ctx.user_id)
        if not token:
            return ToolResult(False, error="no API key configured", error_code="not_configured", source="network")
        try:
            data = await svc.clash_for(ctx.user_id).get_cards()
            await svc.credentials.mark_status(ctx.user_id if origin == "user" else None, "clash_api", "ok")
            return ToolResult(True, {"key_origin": origin, "cards": len(data.get("items", []))}, source="official_api")
        except ClashAPIError as e:
            if origin in ("user", "global"):
                await svc.credentials.mark_status(ctx.user_id if origin == "user" else None, "clash_api", e.code)
            return ToolResult(False, {"key_origin": origin}, error=str(e), error_code=e.code, source="official_api")

    reg.register(Tool("api_test", "Test the Clash Royale API key over the verified route (/cards).", _obj({}),
                      api_test, "network", "credentials.own", timeout=45))

    # ------------------------------------------------------------------ terminal & files (owner)
    async def terminal_run(ctx, a):
        r = await svc.terminal.run(a["command"], confirmed=ctx.confirmed, cwd=a.get("cwd"))
        return ToolResult(r.exit_code == 0, r.__dict__, error=None if r.exit_code == 0 else
                          (r.stderr[-300:] or f"exit {r.exit_code}"), error_code=None if r.exit_code == 0 else "exit",
                          source="terminal")

    reg.register(Tool("terminal_run", "Run an allow-listed command in the restricted workspace (no shell operators; "
                      "cwd='project' for read/test-only access to the source).",
                      _obj({"command": S, "cwd": S}, ["command"]), terminal_run, "terminal", "agent.terminal",
                      timeout=90))

    async def file_read(ctx, a):
        return ToolResult(True, {"content": svc.terminal.read_file(a["path"])}, source="files")

    reg.register(Tool("file_read", "Read a workspace file (prefix 'project:' for read-only source files).",
                      _obj({"path": S}, ["path"]), file_read, "files", "agent.terminal"))

    async def file_write(ctx, a):
        return ToolResult(True, svc.terminal.write_file(a["path"], a["content"]), source="files")

    reg.register(Tool("file_write", "Write a file in the workspace (reports, scripts).",
                      _obj({"path": S, "content": S}, ["path", "content"]), file_write, "files", "agent.files.write"))

    async def build_zip(ctx, a):
        from ..terminal import build_project_zip
        out = svc.settings.workspace_dir / "artifacts" / f"cr-analyst-{datetime.now(timezone.utc):%Y%m%d-%H%M}.zip"
        return ToolResult(True, build_project_zip(svc.project_root, out), source="files")

    reg.register(Tool("build_zip", "Build a clean source ZIP (no secrets / databases).", _obj({}), build_zip,
                      "files", "owner.all", timeout=120))

    async def telegram_send(ctx, a):
        if ctx.chat_id is None:
            return ToolResult(False, error="no chat", error_code="no_chat")
        mid = await svc.notifier.direct(ctx.chat_id, ctx.language, a["text"][:3500])
        return ToolResult(mid is not None, {"message_id": mid}, source="telegram")

    reg.register(Tool("telegram_send", "Send a message to the current user's chat only.", _obj({"text": S}, ["text"]),
                      telegram_send, "telegram", "agent.ask", llm_exposed=False))

    return reg
