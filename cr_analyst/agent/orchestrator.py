"""Central agent orchestrator (Hermes-inspired).

Every meaningful request becomes a persisted task:
    QUEUED → (RESEARCHING) → PLANNING → EXECUTING → VERIFYING → COMPLETED
* Known intents (scan / deck / battles / meta / api_check) use deterministic plans; exact numbers come from Python.
  The LLM (optional) only phrases strategic interpretation and is fact-checked.
* Free questions ("ask") use a bounded LLM tool-calling loop over the permission-filtered registry, research-first
  for questions about the current game state (Tavily web search when TAVILY_KEY is configured).

Resume-from-same-step (interruption handling)
* Every plan step runs through `_step()`. Its result is checkpointed into `task.result._partial[step]`.
* If work is interrupted mid-step by a transient failure (network, rate limit, maintenance, LLM outage, DB blip),
  the agent notifies the user, waits RESUME_DELAY_SECONDS (default 10 s) and continues from the SAME step.
  Completed steps are never re-run (their checkpointed results are reused).
* If the worker itself stops (runner restart), unfinished tasks become RECOVERY_REQUIRED, are re-queued with the
  same 10 s delay, and resume from the first unfinished step using the checkpointed results.
* The LLM tool loop checkpoints its conversation after each round, so an "ask" task also resumes mid-reasoning.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Awaitable, Callable

import httpx
from sqlalchemy.exc import OperationalError

from ..clash.client import ClashAPIError
from ..i18n import t
from ..security.redact import redact
from .llm import LLMError, unsupported_numbers
from .tasks import (
    CANCELLED, COMPLETED, EXECUTING, FAILED, PAUSED, PLANNING, QUEUED, RESEARCHING, VERIFYING, PlanStep, TaskCancelled,
    TaskPaused, TaskView,
)
from .tools.registry import ToolContext, ToolResult

if TYPE_CHECKING:  # pragma: no cover
    from ..app import Services

log = logging.getLogger(__name__)

PLANS: dict[str, list[tuple[str, str | None]]] = {
    "scan": [("plan", None), ("api_scan", "clash_scan"), ("interpret", None), ("verify", None)],
    "deck": [("plan", None), ("meta_signal", "meta_refresh"), ("load_collection", "clash_scan"),
             ("generate", "deck_generate"), ("explain", None), ("verify", None)],
    "battles": [("plan", None), ("api_battlelog", "battle_analyze"), ("analysis", None), ("verify", None)],
    "meta": [("plan", None), ("research_official", "meta_refresh"), ("research_web", "web_search"),
             ("summarize", "balance_recent"), ("verify", None)],
    "api_check": [("plan", None), ("detect_ip", "egress_check"), ("test_api", "api_test"), ("verify", None)],
    "ask": [("plan", None), ("research", "web_search"), ("reason", None), ("verify", None)],
}

RESEARCH_HINTS = re.compile(r"(?i)(meta|patch|balance|buff|nerf|season|update|new card|current|latest|now|"
                            r"متا|بالانس|آپدیت|فصل|جدید|تغییرات|ميتا|تحديث|güncel|aktuell|actuel|actual|мета)")

SYSTEM_PROMPT = (
    "You are the Clash Royale analyst agent. Rules: (1) Use tools for any fact about the player, cards, stats, "
    "balance changes or the meta; never invent card statistics, win rates, battle history or links. "
    "(2) Distinguish clearly: verified (tool data with source) vs. assumption/opinion. (3) If data is missing say "
    "so. (4) Prefer official sources (Supercell) over secondary ones; web results carry a trust tier. "
    "(5) Be concise; answer in the user's language: {lang}. (6) Never reveal secrets or other users' data."
)

ResultHook = Callable[[TaskView, dict, str], Awaitable[None]]


class TransientInterruption(Exception):
    """Raised inside a step to signal recoverable interruption (wait + resume same step)."""


class StepFailed(Exception):
    def __init__(self, res: ToolResult):
        super().__init__(res.error or res.error_code)
        self.code = res.error_code
        self.result = res


def is_transient(e: BaseException) -> bool:
    if isinstance(e, TransientInterruption):
        return True
    if isinstance(e, ClashAPIError):
        return bool(e.retryable)
    if isinstance(e, StepFailed):
        return e.code in ("timeout", "network", "rate_limited", "maintenance", "server_error", "llm_unavailable",
                          "ConnectError", "ReadTimeout", "OperationalError")
    return isinstance(e, (LLMError, httpx.TimeoutException, httpx.TransportError, asyncio.TimeoutError,
                          OperationalError, ConnectionError))


def _jsonable(v: Any) -> Any:
    return json.loads(json.dumps(v, default=str, ensure_ascii=False))


class Orchestrator:
    def __init__(self, svc: "Services"):
        self.svc = svc
        self.result_hook: ResultHook | None = None  # set by the bot to render final results
        self._running: dict[str, asyncio.Task] = {}
        self._wake = asyncio.Event()

    @property
    def resume_delay(self) -> float:
        return float(self.svc.settings.resume_delay_seconds)

    # ------------------------------------------------------------------ submission
    async def submit(self, user, chat_id: int, kind: str, params: dict | None = None,
                     goal: str | None = None) -> TaskView:
        if kind not in PLANS:
            raise ValueError(kind)
        plan = [PlanStep(k, tool) for k, tool in PLANS[kind]]
        tv = await self.svc.tasks.create(user.id, chat_id, kind, goal or kind, params=params or {}, plan=plan,
                                         description=(params or {}).get("question"))
        await self.svc.notifier.publish(user.language, tv, op=t(user.language, "op_received"), force=True)
        self._wake.set()
        return tv

    def wake(self) -> None:
        self._wake.set()

    # ------------------------------------------------------------------ worker loop
    async def run_workers(self, n: int, stop: asyncio.Event) -> None:
        async def worker():
            while not stop.is_set():
                tv = await self.svc.tasks.claim_next(exclude=set(self._running))
                if tv is None:
                    self._wake.clear()
                    try:
                        await asyncio.wait_for(self._wake.wait(), timeout=2.0)
                    except asyncio.TimeoutError:
                        pass
                    continue
                self._running[tv.id] = asyncio.current_task()
                try:
                    await self.execute(tv)
                finally:
                    self._running.pop(tv.id, None)
                    self.svc.tasks.release(tv.id)
        await asyncio.gather(*(worker() for _ in range(max(1, n))))

    def running_ids(self) -> list[str]:
        return list(self._running)

    # ------------------------------------------------------------------ execution
    async def _ctx(self, tv: TaskView) -> tuple[ToolContext, str]:
        user = await self.svc.users.get_by_id(tv.user_id)
        lang = user.language if user else "en"
        perms = await self.svc.permissions_for(user)
        ctx = ToolContext(user_id=tv.user_id, telegram_id=user.telegram_id if user else 0,
                          role=user.role if user else "user", permissions=perms, task_id=tv.id, chat_id=tv.chat_id,
                          language=lang)
        return ctx, lang

    async def execute(self, tv: TaskView) -> TaskView:
        ctx, lang = await self._ctx(tv)

        async def progress(step_key: str, pct: int):
            await self.svc.tasks.checkpoint_guard(tv.id)
            cur = await self.svc.tasks.step(tv.id, step_key, "running", progress=pct)
            await self.svc.notifier.publish(lang, cur, op=t(lang, f"op_{step_key}"))

        ctx.extras["progress"] = progress
        resumed_from = await self.svc.tasks.resume_step(tv.id) if (await self.svc.tasks.partial(tv.id)) else None
        try:
            if tv.status == QUEUED:
                tv = await self.svc.tasks.transition(tv.id, PLANNING, message="claimed")
            if resumed_from:
                await self.svc.notifier.publish(lang, tv, op=t(lang, "op_resumed", step=t(lang, f"op_{resumed_from}")),
                                                force=True)
            await self.svc.tasks.step(tv.id, "plan", "done", progress=5)
            skills = await self.svc.skills.match(tv.kind, tv.params.get("question", ""))
            if skills:
                await self.svc.tasks.event(tv.id, "skills", ", ".join(skills))
            tv = await self.svc.tasks.get(tv.id)
            await self.svc.notifier.publish(lang, tv, op=t(lang, "op_plan"))
            handler = getattr(self, f"_run_{tv.kind}")
            result = await handler(tv, ctx, lang, progress, skills)
            await self.svc.tasks.checkpoint_guard(tv.id)
            tv = await self.svc.tasks.transition(tv.id, VERIFYING, message="verifying")
            await self.svc.notifier.publish(lang, tv, op=t(lang, "op_verify"))
            issues = self.verify(tv.kind, result)
            if issues:
                result["verification_issues"] = issues
            await self.svc.tasks.step(tv.id, "verify", "done", progress=98)
            await self._learn(tv, result)
            tv = await self.svc.tasks.transition(tv.id, COMPLETED, message="completed", result=_compact(result))
            await self.svc.notifier.publish(lang, tv, op=t(lang, "op_done"), force=True)
            if self.result_hook:
                await self.result_hook(tv, result, lang)
            return tv
        except TaskCancelled:
            tv = await self.svc.tasks.transition(tv.id, CANCELLED, message="cancelled")
            await self.svc.notifier.publish(lang, tv, force=True)
            return tv
        except TaskPaused:
            tv = await self.svc.tasks.transition(tv.id, PAUSED, message="paused")
            await self.svc.notifier.publish(lang, tv, force=True)
            return tv
        except asyncio.CancelledError:
            raise  # worker shutdown: task stays active → RECOVERY_REQUIRED at next startup → resumes same step
        except Exception as e:
            code = getattr(e, "code", None) or type(e).__name__
            msg = self.user_error(lang, e)
            await self.svc.errors.record("agent", str(code), redact(str(e)), tv.user_id)
            cur = await self.svc.tasks.get(tv.id)
            if cur and cur.status not in (COMPLETED, CANCELLED, FAILED):
                tv = await self.svc.tasks.transition(tv.id, FAILED, error=msg, message=f"failed: {code}")
            await self.svc.notifier.publish(lang, tv, note=t(lang, "op_failed_investigating"), force=True)
            if code in ("invalid_ip", "access_denied", "route_rejected"):
                await self.svc.skills.append_lesson("api_troubleshooting",
                                                    f"{code}: compare approved egress IP with the key allow-list first.")
            return tv

    # ------------------------------------------------------------------ resumable step runner
    async def _step(self, tv: TaskView, lang: str, key: str, pct: int, fn: Callable[[], Awaitable[Any]],
                    *, required: bool = True) -> Any:
        """Run one plan step with checkpoint + wait-and-resume-same-step semantics."""
        partial = await self.svc.tasks.partial(tv.id)
        if key in partial:
            await self.svc.tasks.step(tv.id, key, "done", progress=pct, detail="restored from checkpoint")
            return partial[key]
        attempts = 0
        while True:
            await self.svc.tasks.checkpoint_guard(tv.id)
            cur = await self.svc.tasks.step(tv.id, key, "running", progress=max(1, pct - 5))
            await self.svc.notifier.publish(lang, cur, op=t(lang, f"op_{key}"))
            try:
                value = await fn()
            except (TaskCancelled, TaskPaused, asyncio.CancelledError):
                raise
            except Exception as e:
                if not is_transient(e) or attempts >= self.svc.settings.max_resume_attempts:
                    if not required:
                        await self.svc.tasks.step(tv.id, key, "skipped", progress=pct,
                                                  detail=f"{type(e).__name__}: {redact(str(e))[:120]}")
                        await self.svc.tasks.checkpoint_result(tv.id, {key: None})
                        return None
                    await self.svc.tasks.step(tv.id, key, "failed", detail=redact(str(e))[:200])
                    raise
                attempts += 1
                await self._wait_and_resume(tv, lang, key, attempts, e)
                continue
            value = _jsonable(value)
            await self.svc.tasks.checkpoint_result(tv.id, {key: value})
            await self.svc.tasks.step(tv.id, key, "done", progress=pct)
            return value

    async def _wait_and_resume(self, tv: TaskView, lang: str, key: str, attempt: int, err: BaseException) -> None:
        delay = self.resume_delay
        await self.svc.tasks.event(tv.id, "interrupted",
                                   f"step {key} interrupted ({type(err).__name__}); resume in {int(delay)}s "
                                   f"(attempt {attempt})", level="warn")
        cur = await self.svc.tasks.step(tv.id, key, "running", detail=f"waiting {int(delay)}s")
        await self.svc.notifier.publish(lang, cur, op=t(lang, "op_waiting_resume", seconds=int(delay),
                                                       step=t(lang, f"op_{key}")), force=True)
        # cancellable wait: poll the control guard every 0.5 s
        loop = asyncio.get_running_loop()
        end = loop.time() + delay
        while loop.time() < end:
            await self.svc.tasks.checkpoint_guard(tv.id)
            await asyncio.sleep(min(0.5, max(0.0, end - loop.time())))

    async def _tool(self, tv: TaskView, ctx: ToolContext, tool: str, args: dict | None = None) -> Any:
        """Call a tool; transient failures raise so `_step` can wait and resume; permanent ones raise StepFailed."""
        res = await self.svc.registry.call(tool, ctx, args or {})
        await self.svc.tasks.event(tv.id, f"tool:{tool}", "ok" if res.ok else f"error:{res.error_code}",
                                   level="info" if res.ok else "warn", data={"ms": res.duration_ms})
        if not res.ok:
            raise StepFailed(res)
        return res.data

    # ------------------------------------------------------------------ intent handlers
    async def _run_scan(self, tv, ctx, lang, progress, skills):
        await self.svc.tasks.transition(tv.id, EXECUTING, message="executing scan")
        data = await self._step(tv, lang, "api_scan", 80,
                                lambda: self._tool(tv, ctx, "clash_scan", {"tag": tv.params.get("tag")}))
        data["ai_summary"] = await self._step(tv, lang, "interpret", 92, lambda: self._interpret(lang, "scan", {
            "profile": {k: data["profile"].get(k) for k in ("trophies", "best_trophies", "arena", "exp_level")},
            "collection": {k: data["collection"].get(k) for k in ("total", "avg_level", "maxed", "by_rarity",
                                                                  "evolutions_unlocked")},
            "upgrade_priorities": data["upgrade_priorities"][:5],
            "battles": {k: (data.get("battles_recent") or {}).get(k) for k in ("sample_size", "win_rate",
                                                                               "problem_cards")}}), required=False)
        tag = data["profile"].get("tag")
        if tag:
            await self.svc.memory.remember(ctx.user_id, "player_tag", f"{tag} ({data['profile'].get('name')})",
                                           importance=9)
        await self.svc.memory.remember(ctx.user_id, "last_scan",
                                       f"{datetime.now(timezone.utc):%Y-%m-%d}: trophies "
                                       f"{data['profile'].get('trophies')}, avg card level "
                                       f"{data['collection'].get('avg_level')}", importance=6)
        return data

    async def _run_deck(self, tv, ctx, lang, progress, skills):
        await self.svc.tasks.transition(tv.id, RESEARCHING, message="checking meta freshness")
        await self._step(tv, lang, "meta_signal", 25, lambda: self._tool(tv, ctx, "meta_refresh", {}), required=False)
        await self.svc.tasks.transition(tv.id, EXECUTING, message="generating decks")
        p = await self.svc.users.primary_player(ctx.user_id)
        if not p:
            raise LookupError("no_player_linked")

        async def load_collection():
            coll, snap = await self.svc.scanner_for(ctx.user_id).latest_collection(p.id)
            stale = snap is None or (datetime.now(timezone.utc) - (snap.taken_at if snap.taken_at.tzinfo else
                                     snap.taken_at.replace(tzinfo=timezone.utc))).total_seconds() > 86400
            if stale:
                try:
                    await self._tool(tv, ctx, "clash_scan", {})
                    return {"scanned": True}
                except StepFailed:
                    if snap is None:
                        raise
                    return {"scanned": False, "used_snapshot": snap.taken_at.isoformat()}
            return {"scanned": False, "used_snapshot": snap.taken_at.isoformat()}

        await self._step(tv, lang, "load_collection", 45, load_collection)
        args = {"category": tv.params.get("category", "balanced"), "top_n": 3}
        for k in ("weights", "must_include", "exclude"):
            if tv.params.get(k):
                args[k] = tv.params[k]
        data = await self._step(tv, lang, "generate", 75, lambda: self._tool(tv, ctx, "deck_generate", args))
        best = data["decks"][0]
        facts = {"category": data["category"], "cards": [(c["name"], c["level"]) for c in best["cards"]],
                 "avg_elixir": best["avg_elixir"], "score": best["score"], "breakdown": best["breakdown"],
                 "strengths": best["strengths"], "weaknesses": best["weaknesses"], "archetype": best["archetype"],
                 "uncertainty": best["uncertainty"][:4], "battle_pressure": data.get("battle_pressure")}
        data["ai_explanation"] = await self._step(tv, lang, "explain", 90,
                                                  lambda: self._interpret(lang, "deck", facts), required=False)
        if "deck_ids" not in data:
            data["deck_ids"] = await self.svc.decks.save(ctx.user_id, p.id, tv.id, data)
            await self.svc.tasks.checkpoint_result(tv.id, {"generate": _jsonable(data)})
        await self.svc.memory.remember(ctx.user_id, "deck_pref", f"last requested category: {data['category']}",
                                       importance=4)
        return data

    async def _run_battles(self, tv, ctx, lang, progress, skills):
        await self.svc.tasks.transition(tv.id, EXECUTING, message="analysing battles")
        data = await self._step(tv, lang, "api_battlelog", 60,
                                lambda: self._tool(tv, ctx, "battle_analyze", {"days": tv.params.get("days")}))
        data["ai_summary"] = await self._step(tv, lang, "analysis", 90, lambda: self._interpret(lang, "battles", {
            k: data.get(k) for k in ("sample_size", "win_rate", "by_mode", "problem_cards", "loss_role_pressure",
                                     "level_differential", "counter_suggestions")}), required=False)
        return data

    async def _run_meta(self, tv, ctx, lang, progress, skills):
        await self.svc.tasks.transition(tv.id, RESEARCHING, message="researching official sources")
        ref = await self._step(tv, lang, "research_official", 40,
                               lambda: self._tool(tv, ctx, "meta_refresh", {"force": bool(tv.params.get("force"))}),
                               required=False)
        web = await self._step(tv, lang, "research_web", 60, lambda: self._tool(
            tv, ctx, "web_search", {"query": f"Clash Royale balance changes meta {datetime.now(timezone.utc):%B %Y}",
                                    "limit": 6}), required=False)
        await self.svc.tasks.transition(tv.id, EXECUTING, message="summarising")
        recent = await self._step(tv, lang, "summarize", 80,
                                  lambda: self._tool(tv, ctx, "balance_recent", {"limit": 40}))
        data = {"refresh": ref, "changes": recent or [], "web_context": web or [],
                "summary": await self.svc.kb.stats_summary(),
                "search_provider": getattr(self.svc.web, "last_provider", None)}
        data["ai_summary"] = await self._interpret(lang, "meta", {
            "official_changes": (recent or [])[:25],
            "web_context": [{"title": w["title"], "tier": w["tier"], "url": w["url"]} for w in (web or [])[:6]]})
        return data

    async def _run_api_check(self, tv, ctx, lang, progress, skills):
        await self.svc.tasks.transition(tv.id, EXECUTING, message="network diagnostics")
        eg = await self._step(tv, lang, "detect_ip", 50, lambda: self._tool(tv, ctx, "egress_check", {}),
                              required=False)
        res = await self.svc.registry.call("api_test", ctx, {})
        await self.svc.tasks.step(tv.id, "test_api", "done" if res.ok else "failed", progress=85,
                                  detail=None if res.ok else res.error_code)
        return {"egress": eg or {"error": "egress_unreachable"}, "api_ok": res.ok, "api": res.data,
                "api_error": res.error_code}

    async def _run_ask(self, tv, ctx, lang, progress, skills):
        q = tv.params.get("question", "")
        if not self.svc.llm:
            await self.svc.tasks.transition(tv.id, EXECUTING, message="no LLM configured")
            return {"answer": None, "no_llm": True}
        needs_research = bool(RESEARCH_HINTS.search(q))
        research = None
        if needs_research:
            await self.svc.tasks.transition(tv.id, RESEARCHING, message="research-first")

            async def do_research():
                await self.svc.registry.call("meta_refresh", ctx, {})
                return await self._tool(tv, ctx, "web_search", {"query": f"Clash Royale {q[:180]}", "limit": 5})
            research = await self._step(tv, lang, "research", 25, do_research, required=False)
        else:
            await self.svc.tasks.step(tv.id, "research", "skipped", progress=20)
        await self.svc.tasks.transition(tv.id, EXECUTING, message="reasoning with tools")
        return await self._step(tv, lang, "reason", 90, lambda: self._ask_loop(tv, ctx, lang, q, skills, research))

    async def _ask_loop(self, tv, ctx, lang, q, skills, research) -> dict:
        """Bounded tool-calling loop. The conversation is checkpointed after every round, so an interruption
        resumes from the same round instead of restarting the reasoning."""
        state = (await self.svc.tasks.partial(tv.id)).get("_ask_state")
        if state:
            messages, facts, used, rnd0 = state["messages"], state["facts"], state["used"], state["round"]
        else:
            skill_text = ""
            for name in skills[:2]:
                sk = await self.svc.skills.load(name)
                if sk:
                    skill_text += f"\n\nSKILL {sk['name']} v{sk['version']}:\n{sk['content'][:1800]}"
            memory = await self.svc.memory.summary(ctx.user_id)
            user_msg = q[:2000]
            if research:
                user_msg += "\n\nWEB RESEARCH (tiered):\n" + json.dumps(
                    [{"title": r.get("title"), "url": r.get("url"), "tier": r.get("tier"),
                      "snippet": (r.get("snippet") or "")[:300]} for r in research[:5]], ensure_ascii=False)
            messages = [{"role": "system", "content": SYSTEM_PROMPT.format(lang=lang) + "\n\n" + memory + skill_text},
                        {"role": "user", "content": user_msg}]
            facts, used, rnd0 = ([{"research": research}] if research else []), [], 0
        tools = self.svc.registry.schemas(ctx, {"clash", "knowledge", "analysis", "decks", "research", "memory",
                                                 "skills", "tasks", "network"})
        answer = None
        for rnd in range(rnd0, self.svc.settings.llm_max_tool_rounds):
            await self.svc.tasks.checkpoint_guard(tv.id)
            resp = await self.svc.llm.chat(messages, tools=tools)  # LLMError → transient → wait 10 s, same round
            if not resp.tool_calls:
                answer = resp.content
                break
            messages.append({"role": "assistant", "content": resp.content or "",
                             "tool_calls": [{"id": c["id"], "type": "function",
                                             "function": {"name": c["name"], "arguments": json.dumps(c["arguments"])}}
                                            for c in resp.tool_calls]})
            for c in resp.tool_calls:
                used.append(c["name"])
                r = await self.svc.registry.call(c["name"], ctx, c["arguments"])
                await self.svc.tasks.event(tv.id, f"tool:{c['name']}", "ok" if r.ok else f"error:{r.error_code}")
                facts.append({"tool": c["name"], "result": r.data if r.ok else r.error})
                messages.append({"role": "tool", "tool_call_id": c["id"], "content": r.to_llm()})
            await self.svc.tasks.checkpoint_result(tv.id, {"_ask_state": _jsonable(
                {"messages": messages[-24:], "facts": facts[-20:], "used": used, "round": rnd + 1})})
            await self.svc.tasks.step(tv.id, "reason", "running", progress=min(88, 45 + rnd * 8))
        if answer is None:
            answer = t(lang, "agent_round_limit")
        return {"answer": redact(answer), "tools_used": used, "researched": bool(research),
                "unverified_numbers": unsupported_numbers(answer, facts + [q])[:10],
                "search_provider": getattr(self.svc.web, "last_provider", None)}

    # ------------------------------------------------------------------ LLM interpretation (optional)
    async def _interpret(self, lang: str, kind: str, facts: dict) -> str | None:
        if not self.svc.llm:
            return None
        prompts = {
            "scan": "Give 3-4 short strategic insights about this account. Only use the numbers provided.",
            "deck": "Explain in 4-6 short bullet points why this deck suits the player and how to play it "
                    "(win condition, defense, spell use). Mention weaknesses. Only use the facts provided.",
            "battles": "Summarise the player's battle performance and 2-3 actionable improvements. Do not claim "
                       "knowledge of placements or timing. Only use the numbers provided.",
            "meta": "Summarise the most impactful recent official balance changes and what they imply for the meta. "
                    "Cite effective dates. Web context is lower-trust; label it. Only use the facts provided.",
        }
        messages = [{"role": "system", "content": SYSTEM_PROMPT.format(lang=lang)},
                    {"role": "user", "content": prompts[kind] + "\nFACTS:\n" +
                     json.dumps(facts, ensure_ascii=False, default=str)[:5000]}]
        resp = await self.svc.llm.chat(messages, max_tokens=500)  # LLMError is transient → resume same step
        text = (resp.content or "").strip()
        if unsupported_numbers(text, facts):
            log.info("LLM output rejected by numeric fact guard")
            return None
        return redact(text)[:2500] or None

    # ------------------------------------------------------------------ verification & learning
    @staticmethod
    def verify(kind: str, result: dict) -> list[str]:
        issues = []
        if kind == "deck":
            for d in result.get("decks", []):
                ids = [c["id"] for c in d["cards"]]
                if len(ids) != 8 or len(set(ids)) != 8:
                    issues.append("deck_not_8_unique")
                if d.get("share_link") and d["share_link"].count(";") != 7:
                    issues.append("bad_share_link")
                elixirs = [c["elixir"] for c in d["cards"] if c.get("elixir") is not None]
                if elixirs and abs(round(sum(elixirs) / len(elixirs), 2) - d["avg_elixir"]) > 0.01:
                    issues.append("avg_elixir_mismatch")
        if kind == "battles" and result.get("sample_size"):
            if result["wins"] + result["losses"] + result["draws"] != result["sample_size"]:
                issues.append("wld_sum_mismatch")
        if kind == "scan" and result.get("collection", {}).get("total", 0) == 0:
            issues.append("empty_collection")
        return issues

    async def _learn(self, tv: TaskView, result: dict) -> None:
        try:
            if tv.kind == "deck" and result.get("verification_issues"):
                await self.svc.skills.append_lesson("deck_optimization",
                                                    "Re-verify 8 unique cards and avg elixir after substitutions.")
            if tv.kind == "battles" and 0 < (result.get("sample_size") or 0) < 10:
                await self.svc.skills.append_lesson("battle_analysis",
                                                    "Samples under 10 battles: present trends as low-confidence.")
        except Exception:
            log.debug("learning step failed", exc_info=True)

    @staticmethod
    def user_error(lang: str, e: Exception) -> str:
        code = getattr(e, "code", None)
        if isinstance(e, LookupError) and "no_player_linked" in str(e):
            return t(lang, "err_no_player")
        mapping = {"not_configured": "err_no_api_key", "invalid_ip": "err_invalid_ip", "access_denied": "err_access",
                   "not_found": "err_player_not_found", "rate_limited": "err_rate_limited",
                   "maintenance": "err_maintenance", "network": "err_network", "route_rejected": "err_route",
                   "no_snapshot": "err_no_snapshot", "insufficient_cards": "err_insufficient_cards",
                   "no_player_linked": "err_no_player", "LookupError": "err_no_player"}
        if isinstance(e, StepFailed) and e.result.error and "no_player_linked" in e.result.error:
            return t(lang, "err_no_player")
        if code in mapping:
            return t(lang, mapping[code])
        return t(lang, "err_generic", code=str(code or type(e).__name__)[:40])


def _compact(result: dict, limit: int = 60000) -> dict:
    s = json.dumps(result, default=str, ensure_ascii=False)
    if len(s) <= limit:
        return json.loads(s)
    small = {k: v for k, v in result.items() if k in ("category", "profile", "collection", "wins", "losses",
                                                       "win_rate", "answer", "summary", "deck_ids", "api_ok")}
    small["_truncated"] = True
    return json.loads(json.dumps(small, default=str, ensure_ascii=False))
