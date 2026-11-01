"""Real health information for /status (no fabricated "OK"s)."""
from __future__ import annotations

from typing import Any


async def collect_status(svc, deep: bool = False) -> dict[str, Any]:
    ok, detail = await svc.db.ping()
    out: dict[str, Any] = {"db": {"ok": ok, "detail": detail}}
    out["telegram"] = bool(getattr(svc, "bot_ok", False))
    last = await svc.network.last_health("clash_api")
    token, origin = await svc.credentials.clash_token_for(None)
    if last is None:
        api = {"ok": False, "detail": "no requests yet" if token else "no API key configured",
               "hint": None if token else "err_no_api_key"}
    else:
        api = {"ok": bool(last.ok), "detail": f"{'ok' if last.ok else 'error'} {last.status_code or ''} "
                                              f"{(last.detail or '')[:60]} @ {last.ts:%Y-%m-%d %H:%M}".strip(),
               "hint": None}
        if not last.ok and last.detail and "invalid_ip" in last.detail:
            api["hint"] = "err_invalid_ip"
    out["clash_api"] = api
    llm = svc.llm
    out["llm"] = {"enabled": llm is not None,
                  "detail": (getattr(llm, "model", "custom") if llm else "disabled (deterministic mode)")}
    out["search"] = svc.web.provider
    out["kb"] = await svc.kb.stats_summary()
    out["tasks"] = await svc.tasks.counts()
    out["runner"] = svc.runtime_status_sync() if hasattr(svc, "runtime_status_sync") else {}
    if deep:
        out["network"] = await svc.network.status()
        out["errors"] = await svc.errors.summary(6)
        out["health_24h"] = await svc.network.health_summary()
    return out
