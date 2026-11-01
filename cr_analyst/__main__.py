"""CLI entrypoint.

  python -m cr_analyst run                 # start the bot worker (GitHub Actions or VPS)
  python -m cr_analyst init-db             # create schema / seed reference data (Alembic is the prod path)
  python -m cr_analyst meta-update         # one-shot official balance-change research (meta-update.yml)
  python -m cr_analyst sync-stats          # import secondary baseline stats (flagged secondary-stale)
  python -m cr_analyst healthcheck         # exit 0 if DB reachable and config valid
  python -m cr_analyst owner-transfer ID   # out-of-band owner recovery (requires DB access)
  python -m cr_analyst build-zip OUT.zip   # clean source archive (no secrets / DBs)
  python -m cr_analyst gen-key             # generate an APP_ENCRYPTION_KEY
  python -m cr_analyst export-backup OUT   # encrypted JSON export of non-secret tables (backup.yml)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from .config import get_settings
from .security.redact import setup_logging

log = logging.getLogger("cr_analyst")


async def _services(**kw):
    from .app import Services
    svc = Services(get_settings(), **kw)
    await svc.init()
    return svc


async def cmd_run(args) -> int:
    from .app import Services
    from .bot.app import TelegramGateway, build_bot, build_dispatcher
    from .runtime.runner import Runner
    st = get_settings()
    if not st.bot_token:
        log.error("BOT_TOKEN is not configured")
        return 2
    if not st.app_encryption_key:
        log.warning("APP_ENCRYPTION_KEY not set: credential storage disabled")
    if st.is_sqlite and st.max_runtime_minutes > 0:
        log.warning("SQLite on an ephemeral runner is NOT persistent; use PostgreSQL (DATABASE_URL) in production")
    svc = Services(st)
    bot = build_bot(st.bot_token)
    gw = TelegramGateway(svc, bot, build_dispatcher(svc))
    try:
        if not await gw.check():
            return 3
        runner = Runner(svc, chain_index=args.chain_index)
        reason = await runner.run(gw.poller(runner))
        log.info("exit reason: %s", reason)
        return 0 if reason in ("planned_deadline", "owner_restart", "signal") else 1
    finally:
        await gw.close()
        await svc.close()


async def cmd_init_db(args) -> int:
    svc = await _services()
    print(json.dumps({"db": svc.db.dialect, "skills": len(await svc.skills.index()),
                      "kb": await svc.kb.stats_summary()}))
    await svc.close()
    return 0


async def cmd_meta_update(args) -> int:
    svc = await _services()
    try:
        rep = await svc.kb.research_official_balance(max_articles=args.max_articles)
        from datetime import datetime, timezone
        await svc.set_config("meta_last_refresh", datetime.now(timezone.utc).isoformat())
        if args.catalog_fallback and (await svc.kb.stats_summary())["cards"] == 0:
            rep["secondary_catalog"] = await svc.kb.bootstrap_secondary_catalog()
        rep["summary"] = await svc.kb.stats_summary()
        print(json.dumps(rep, default=str, indent=2))
        return 0 if rep["checked"] or not rep["errors"] else 1
    finally:
        await svc.close()


async def cmd_sync_stats(args) -> int:
    svc = await _services()
    try:
        if (await svc.kb.stats_summary())["cards"] == 0:
            print(json.dumps(await svc.kb.bootstrap_secondary_catalog()))
        print(json.dumps(await svc.kb.import_secondary_stats()))
        return 0
    finally:
        await svc.close()


async def cmd_healthcheck(args) -> int:
    from .app import Services
    svc = Services(get_settings())
    ok, detail = await svc.db.ping()
    print(json.dumps({"db_ok": ok, "db": detail, "bot_token": bool(svc.settings.bot_token),
                      "encryption": svc.box is not None, "llm": svc.llm is not None,
                      "search": svc.web.provider, "gh_dispatch": bool(svc.settings.gh_pat)}))
    await svc.close()
    return 0 if ok else 1


async def cmd_owner_transfer(args) -> int:
    svc = await _services()
    await svc.users.transfer_owner_cli(int(args.telegram_id))
    print(f"owner set to {args.telegram_id}")
    await svc.close()
    return 0


def cmd_build_zip(args) -> int:
    from .agent.terminal import build_project_zip
    res = build_project_zip(Path(__file__).resolve().parent.parent, Path(args.out))
    print(json.dumps(res))
    return 0


def cmd_gen_key(args) -> int:
    from .security.crypto import generate_key
    print(generate_key())
    return 0


async def cmd_export_backup(args) -> int:
    """Encrypted logical backup of application tables (credentials stay encrypted; nothing is decrypted)."""
    from sqlalchemy import select
    from .db.models import Base
    svc = await _services()
    try:
        if not svc.box:
            print("APP_ENCRYPTION_KEY required for encrypted backups", file=sys.stderr)
            return 2
        data = {}
        async with svc.db.session() as s:
            for table in Base.metadata.sorted_tables:
                if table.name in ("processed_updates", "research_cache"):
                    continue
                rows = (await s.execute(select(table))).mappings().all()
                data[table.name] = [dict(r) for r in rows]
        blob = json.dumps(data, default=str).encode()
        Path(args.out).write_bytes(svc.box.encrypt_bytes(blob))
        print(json.dumps({"tables": len(data), "rows": sum(len(v) for v in data.values()), "out": args.out}))
        return 0
    finally:
        await svc.close()


def main(argv: list[str] | None = None) -> int:
    st = get_settings()
    setup_logging(st.log_level)
    p = argparse.ArgumentParser(prog="cr_analyst")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--chain-index", type=int, default=0)
    sub.add_parser("init-db")
    m = sub.add_parser("meta-update")
    m.add_argument("--max-articles", type=int, default=6)
    m.add_argument("--catalog-fallback", action="store_true")
    sub.add_parser("sync-stats")
    sub.add_parser("healthcheck")
    o = sub.add_parser("owner-transfer")
    o.add_argument("telegram_id")
    z = sub.add_parser("build-zip")
    z.add_argument("out")
    sub.add_parser("gen-key")
    b = sub.add_parser("export-backup")
    b.add_argument("out")
    args = p.parse_args(argv)
    sync = {"build-zip": cmd_build_zip, "gen-key": cmd_gen_key}
    if args.cmd in sync:
        return sync[args.cmd](args)
    fn = {"run": cmd_run, "init-db": cmd_init_db, "meta-update": cmd_meta_update, "sync-stats": cmd_sync_stats,
          "healthcheck": cmd_healthcheck, "owner-transfer": cmd_owner_transfer,
          "export-backup": cmd_export_backup}[args.cmd]
    return asyncio.run(fn(args))


if __name__ == "__main__":
    sys.exit(main())
