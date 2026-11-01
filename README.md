# Clash Royale AI Analyst (Telegram bot)

A Telegram bot that analyses a player's **real** Clash Royale account through the official API and acts as an
AI analyst for it. It includes a Hermes-style agent (task planning, tools, skills, persistent memory, research
first), an official balance-change tracker, battle analysis, a deterministic deck builder and a resilient runner
for GitHub Actions.

* **Languages:** فارسی (primary/default), English, العربية, Türkçe, Deutsch, Français, Español, Português,
  Русский, 日本語. Every language has all 427 strings. Persian and Arabic are rendered RTL.
* **Stack:** Python 3.12+/3.13, aiogram 3, SQLAlchemy 2 (async), PostgreSQL (SQLite for development), Alembic,
  httpx, cryptography (Fernet), Pillow.

> **Data honesty.** The official API returns only the ~25 most recent battles and no card placements or timing.
> The bot stores battles from the moment a tag is linked and always labels every value as **retrieved**
> (official API now), **stored** (local history), **calculated** (deterministic metrics) or **unavailable**.
> It never invents statistics, win rates or deck links. AI answers are checked against tool data, and unsupported
> numbers are flagged.

---

## Features

| Area | What it does |
|---|---|
| Account scanner | Profile, full card collection (normalised levels, evolutions, Tower Troops), battle log, upcoming chests, clan. Unknown new API fields are kept, never silently dropped. |
| Battle analysis | Win/loss/draw, per game mode, problem opponent cards (loss-rate lift), deck performance, level differential, trend, net trophies, counter suggestions from owned cards only. |
| Deck builder | 8 categories: defensive, offensive, balanced, fast cycle, counter-attack, high-level cards, meta, custom (must-include / exclude cards). Transparent 12-factor scoring with strengths, weaknesses, substitutions, an upgrade plan, a deck image, and an official copy link that is produced only for 8 valid card ids. |
| Card knowledge base | Official `/cards` catalogue plus versioned balance changes parsed from the official Supercell blog (effective dates, old → new, %), with source tiers: official / secondary / unverified. |
| AI agent | Task lifecycle `QUEUED → RESEARCHING → PLANNING → EXECUTING → VERIFYING → COMPLETED` (+ `FAILED`, `PAUSED`, `CANCELLED`, `RECOVERY_REQUIRED`), 25 permissioned tools, 8 bundled skills (`skills/*/SKILL.md`), bounded per-user memory with injection/secret screening, an allow-listed restricted terminal (owner only), and an OpenAI-compatible LLM. |
| Interrupt & resume | If work is interrupted mid-step (network/LLM/API drop), the bot tells the user, **waits 10 seconds** (`RESUME_DELAY_SECONDS`) and **continues from the same step**. Finished steps are checkpointed and never re-run. After a worker restart, interrupted tasks go to `RECOVERY_REQUIRED` and are re-queued with the same delay and their partial results. |
| Web research | **Tavily** (`TAVILY_KEY`) is the preferred search provider; it falls back to DuckDuckGo HTML (Brave optional). Results are tier-ranked with official sources first, cached, and protected against SSRF. |
| Notifications | One live progress message per task, edited in place and batched, with Pause / Resume / Cancel / Details buttons. |
| Roles | owner / admin / user / banned. The owner is set by `ADMIN_ID`, or else the first private `/start` claims it atomically. There is a one-time recovery code and a CLI transfer. |
| Network | Egress-IP detection, trusted proxy profiles (http/https/socks5, encrypted credentials), approved-IP enforcement, health records. |
| Security | Fernet/MultiFernet encryption with key rotation (`APP_ENCRYPTION_KEY`), secret redaction in logs, tool output and errors, API keys deleted from chat immediately, and permission checks in the backend. |
| Runner | DB lease with a fencing token (single active worker), persisted Telegram offset, idempotent update processing, periodic checkpoints, graceful stop at about 330 min, and automatic chaining of the next GitHub Actions run. |

---

## Quick start (local)

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env                 # fill BOT_TOKEN, APP_ENCRYPTION_KEY (python -m cr_analyst gen-key)
python scripts/db_migrate.py         # Alembic upgrade to head (SQLite by default)
python -m cr_analyst healthcheck
python -m cr_analyst run             # long-polling worker
```

The first person to send `/start` in a private chat becomes the **owner**, unless `ADMIN_ID` is set.

### CLI

| Command | Purpose |
|---|---|
| `python -m cr_analyst run [--chain-index N]` | Start the bot worker |
| `python -m cr_analyst init-db` | Create the schema directly (development; Alembic is the production path) |
| `python -m cr_analyst meta-update [--max-articles 6] [--catalog-fallback]` | One-shot official balance research |
| `python -m cr_analyst sync-stats` | Import secondary baseline stats (marked secondary) |
| `python -m cr_analyst healthcheck` | Exit 0 if the DB is reachable and the config is valid |
| `python -m cr_analyst owner-transfer TELEGRAM_ID` | Out-of-band owner recovery (needs DB access) |
| `python -m cr_analyst build-zip OUT.zip` | Clean source archive (no secrets / DBs / caches) |
| `python -m cr_analyst gen-key` | Generate an `APP_ENCRYPTION_KEY` |
| `python -m cr_analyst export-backup OUT` | Encrypted logical backup |

---

## Configuration

All settings come from environment variables (see `.env.example`).

| Variable | Required | Notes |
|---|---|---|
| `BOT_TOKEN` | yes | From @BotFather |
| `DATABASE_URL` | yes | `postgresql://…` in production (normalised to asyncpg). SQLite only for development. |
| `APP_ENCRYPTION_KEY` | yes | Fernet key or a passphrase of 16+ characters. Comma-separate keys to rotate (newest first). |
| `ADMIN_ID` | no | Numeric Telegram id of the owner |
| `GH_PAT` | for chaining | Fine-grained PAT, **Actions: Read and write** on this repo only |
| `LLM_API_KEY`, `LLM_MODEL`, `LLM_BASE_URL` | no | Any OpenAI-compatible endpoint. Without it, free-form AI answers are off; all deterministic analysis still works. |
| `CLASH_API_TOKEN` | no | Shared developer key. Users can also add a personal key in the bot (stored encrypted). |
| `TAVILY_KEY` | no | Tavily web search (also accepts `TAVILY_API_KEY` / `tavily_key`) |
| `RESUME_DELAY_SECONDS` | no | Default `10`: wait before continuing an interrupted step |
| `MAX_RESUME_ATTEMPTS` | no | Default `5` |
| `EXPECTED_EGRESS_IP` | no | Refuse authenticated CR calls unless the egress IP matches |
| `MAX_RUNTIME_MINUTES`, `MAX_AUTO_RESTARTS`, `LEASE_TTL_SECONDS`, `TASK_WORKERS` | no | Runner tuning |

### Clash Royale developer key & IP allow-listing

Keys from <https://developer.clashroyale.com> are bound to **allowed IP addresses**. GitHub-hosted runners get a
**different IP on every run**, so for the official API you need one of these:

1. A **trusted proxy / VPS with a static IP**: add it in the bot under *Network & IP → Trusted proxy profiles*,
   test it, activate it, then add that IP to your key.
2. Running the worker on your own server with a static IP.

The bot shows the current egress IP, step-by-step allow-list instructions, and clear errors (`invalid_ip`,
`access_denied`, `route_rejected`). It never asks for game passwords.

---

## GitHub Actions

| Workflow | Trigger | Purpose |
|---|---|---|
| `bot-runner.yml` | manual, chained dispatch, 6-hourly watchdog | Runs the worker. Job timeout is **350 min**; the worker stops gracefully at about **330 min**, saves a checkpoint, releases the DB lease and dispatches the next run with `GH_PAT` (`chain_index`+1). `concurrency: bot-runner` plus the DB lease keep a single active worker. |
| `tests.yml` | push / PR | Compile, i18n completeness, Alembic upgrade/downgrade/drift check, pytest on Python 3.12 and 3.13, and migrations plus DB tests on a PostgreSQL 16 service |
| `meta-update.yml` | every 6 h | Official balance-change research into the DB |
| `backup.yml` | daily | Encrypted logical backup uploaded as an artifact |
| `artifact-build.yml` | tag `v*` / manual | Tests, builds and verifies the clean ZIP, uploads it, attaches it to a Release |

**Repository secrets:** `BOT_TOKEN`, `DATABASE_URL`, `APP_ENCRYPTION_KEY`, `GH_PAT`, and optionally `ADMIN_ID`,
`LLM_API_KEY`, `LLM_MODEL`, `LLM_BASE_URL`, `CLASH_API_TOKEN`, `TAVILY_KEY`.
**Optional variables:** `EXPECTED_EGRESS_IP`, `MAX_RUNTIME_MINUTES`, `MAX_AUTO_RESTARTS`, `RESUME_DELAY_SECONDS`.

Runner controls are in the owner's admin panel: pause/resume auto-restart, controlled restart, run history,
and max-restart caps.

> **Important:** GitHub Actions hosting is **best-effort**. Jobs are capped at 6 h, runs can be delayed or
> cancelled, and scheduled workflows are disabled after 60 days without repository activity. The chain and
> watchdog recover from most interruptions, but this is not guaranteed 24/7 hosting. Use a persistent
> PostgreSQL database; SQLite on a runner is wiped after every run.

---

## Project layout

```
cr_analyst/
  agent/        tasks (state machine, resume), orchestrator, tools/, skills, memory, llm, web (Tavily), terminal, notifier
  bot/          aiogram dispatcher, middlewares (idempotency, users), handlers (core, results, settings, admin), views
  clash/        API client, tags, cards, battles, scanner, knowledge base, balance parser
  decks/        scoring engine, image renderer
  db/           SQLAlchemy models (29 tables), session
  i18n/         10 locale files
  network/      egress IP and proxy manager
  runtime/      lease, checkpoints, dispatch, scheduler, runner, health
  security/     encryption, redaction
  services/     users/roles/owner, encrypted credentials
migrations/     Alembic (0001 initial schema)
skills/         8 bundled SKILL.md files
scripts/        check_i18n.py, db_migrate.py, verify_zip.py
tests/          pytest suite (110 tests) + real fixtures from the Supercell blog
docs/RESEARCH.md
```

## Tests

```bash
pip install -r requirements-dev.txt
python scripts/check_i18n.py
python -m pytest -q
```

The suite needs no network or real secrets. External services (Clash API, Telegram, Tavily, DuckDuckGo, LLM,
GitHub) are mocked with `httpx.MockTransport` or a fake aiogram session. Bot handlers run through the real
aiogram `Dispatcher`. Covered areas: tags, card levels, balance parser (real October 2026 article fixture), deck
generation for all 8 categories, battle statistics, the 10-language i18n check, encryption and redaction,
atomic owner claim, permissions, the task state machine, **wait-and-resume-same-step** (in-process and after a
restart), lease fencing, checkpoints, memory budget and safety, Tavily with fallback, LLM tool-call parsing,
the numeric hallucination guard, GitHub dispatch, workflow YAML contracts, ZIP cleanliness, the tool registry
and the CLI.

## Known limitations

* The official API only exposes recent battles; deep history builds up over time from automatic tracking.
* Placement, timing and elixir-usage data do not exist in the API, so the bot cannot detect tactical mistakes.
* Evolutions cannot be encoded in copy links; the bot says so and lists them separately.
* Card tactical roles come from a curated table. New cards fall back to inferred roles, which are marked uncertain.
* Free-form AI answers need `LLM_API_KEY`. Without it, everything except open-ended questions still works.
