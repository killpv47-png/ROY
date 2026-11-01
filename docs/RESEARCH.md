# Technical Research — Clash Royale AI Analyst

Research date: **2026-11-01**. Every claim below was checked against the cited source on that date.

## 1. Hermes Agent (Nous Research) — verified architectural findings

Sources: https://github.com/NousResearch/hermes-agent, https://hermes-agent.nousresearch.com/docs/ (Architecture, Memory, Skills pages).

| Hermes concept | What Hermes does (verified) | What we adopt |
|---|---|---|
| Agent loop | `AIAgent.run_conversation()` → prompt builder → provider call → `tool_calls` → `model_tools.handle_function_call()` → loop | Bounded orchestrator loop (`agent/orchestrator.py`) with deterministic plans for known intents and an LLM tool-calling loop for free questions |
| Tool registry | `tools/registry.py`, each tool self-registers; registry handles schemas, dispatch, availability, error wrapping; tools grouped in toolsets | `agent/tools/registry.py` with toolsets, JSON schemas, permission levels, structured `ToolResult` |
| Memory | Bounded curated memory (MEMORY.md 2,200 chars / USER.md 1,375 chars), frozen snapshot at session start, add/patch/remove, injection scanning; long history is searched on demand (FTS) rather than kept in prompt | Per-user DB memory with a **compact summary budget** (char-limited), facts keyed for dedupe, injection/secret scanning, detailed history retrieved from DB tables only when needed |
| Skills | Procedural memory; `SKILL.md` with YAML front-matter (name, description, version, tags); **progressive disclosure** (list → view → reference); agent creates/patches skills after non-trivial tasks; optional write-approval gate | `skills/<name>/SKILL.md` seeds + versioned `skills` DB table; only the index (name+description) is cheap; full content loaded only for the matching task; agent records new versions after successful tasks; owner can gate writes |
| Terminal | Terminal tool with backends and dangerous-command approval | Restricted terminal: allow-list, owner-only, scrubbed env, secret redaction, timeout, sandbox working dir, destructive commands need explicit confirmation |
| Cron / background | Built-in scheduler delivering to platforms | Persistent scheduler state in DB (meta update, battle tracking, retention, checkpoints) |
| Sub-agents | `delegate_tool` spawns isolated subagents | Optional research sub-agent (bounded, research toolset only) |
| Gateway | Telegram gateway, user authorization, session routing | aiogram 3 bot with role-based authorization in the backend |

Memory vs skills (Hermes definition, adopted verbatim in spirit): *memory stores small durable facts that should always be in context; skills store longer procedures that load only when relevant.* We do **not** clone Hermes (no TUI, no 20 platforms, no MCP, no trajectory export).

## 2. Official Clash Royale API

Source: https://developer.clashroyale.com/ (portal, requires login for docs), verified behaviour with a live unauthenticated request:
`GET https://api.clashroyale.com/v1/cards` → `{"reason":"accessDenied","message":"Missing authorization"}`.

* **Auth**: `Authorization: Bearer <JWT developer key>`. Keys are created by the developer in the portal and are **bound to an IP allow-list** (max several IPs per key). A developer key is *not* a player password and gives no access to the player's game account.
* **Player tag**: `#` + characters from `0289PYLQGRJCUV`; must be URL-encoded (`#` → `%23`).
* **Endpoints used**: `/players/{tag}`, `/players/{tag}/battlelog`, `/players/{tag}/upcomingchests`, `/cards`, `/clans/{tag}`, `/clans/{tag}/members`, `/clans/{tag}/currentriverrace`, `/tournaments?name=`, `/tournaments/{tag}`, `/globaltournaments`, `/locations`.
* **Errors**: 400 badRequest, 403 accessDenied / accessDenied.invalidIp, 404 notFound, 429 requestThrottled, 500 unknownException, 503 inMaintenance.
* **Limitations**: battle log returns only the **most recent ~25 battles**; no positional/placement data; no lifetime battle history; card level fields are rarity-relative (`level`, `maxLevel`) → we normalise to the displayed level with `display = level + (globalMax - maxLevel)` calibrated from the response itself; evolution fields (`evolutionLevel`, `maxEvolutionLevel`) and `supportCards` (Tower Troops) are parsed **only when present**; hero-form ownership is not documented, so it is reported as "not exposed" unless a hero field appears.
* `/cards` is a **catalog** (id, name, maxLevel, maxEvolutionLevel, elixirCost, rarity, iconUrls) — not stats, not patch history.

## 3. Game data sources and reliability

| Source | Tier | Verified content | Use |
|---|---|---|---|
| Supercell blog (`supercell.com/en/games/clashroyale/blog/`) | **Official (primary)** | Next.js `__NEXT_DATA__` JSON with `publishDate`, `title`, `bodyCollection` (headings "Balance Update – October 19th", sections NERFS/BUFFS/REWORKS/BUG FIXES, `FeatureBlock` per card with `Attribute: old → new (±x%)`) | Balance-change history (publication date + effective date + source URL) |
| Official API `/cards` | Official | Current card list, max levels, evolutions, elixir | Catalog of record |
| RoyaleAPI `cr-api-data` (`royaleapi.github.io/cr-api-data/json/cards_stats.json`) | Secondary, **stale** (last commit 2023-10-18) | Per-level hitpoints/damage, speed, range, hit speed, targets | Baseline stats flagged `secondary-stale`; superseded by later official changes |
| royaleapi.com website | Secondary | Returns HTTP 403 to non-browser clients | Referenced via web search results only; never scraped aggressively |
| Social media / wikis | Unverified | — | Never written to the knowledge DB |

Latest verified official balance updates (Season 88): published **2026-10-06**, effective **2026-10-06** (Hero Ice Wizard, Cannon Evo, Barbarian Barrel, Elixir Collector, Minion Giant, Royal Ghost, Skeletons nerfs; Goblin Barrel, Goblin Drill, Lava Hound, Ram Rider, Wall Breakers, X-Bow, Monk, Three Musketeers buffs; Golden Knight rework) and **2026-10-19** (Cannon Cart, Dart Goblin, Minion Giant, Rune Giant nerfs; Hero Electro Wizard, Electro Dragon, Flying Machine, Void buffs; Skeleton Barrel/Evo & Skeletons reworks). Supercell notes cards are scaled at **level 11**. The parser is tested against a fixture of this exact page.

Deck sharing: the in-game copy link `https://link.clashroyale.com/deck/en?deck=<id>;<id>;…` (8 card IDs) is the official format; it does not carry evolution or hero form. We generate it only from verified catalog IDs.

## 4. GitHub Actions constraints (docs.github.com)

* Job max 6 h on hosted runners → `timeout-minutes: 350`, worker target 330 min, graceful stop at deadline.
* Events triggered with `GITHUB_TOKEN` don't start workflows, **except `workflow_dispatch` / `repository_dispatch`**; we use a PAT (`GH_PAT`, Actions: write on this repo only) via `POST /repos/{owner}/{repo}/actions/workflows/bot-runner.yml/dispatches`.
* `workflow_dispatch` only works for workflows on the default branch.
* Hosted runner egress IPs are dynamic → official API IP allow-listing is unreliable from Actions; a user-controlled static egress (trusted proxy/VPS gateway) is required for live API calls.
* `concurrency` group + DB lease guarantee one active worker.

## 5. Proposed architecture

```
Telegram (aiogram 3) ─┐                 ┌─ Clash API client (httpx, NetworkManager)
                      ├─ Orchestrator ──┼─ Knowledge DB (catalog, stats versions, balance history)
Scheduler ────────────┘   │  │  │       ├─ Scanner / Battle analyzer / Deck engine (deterministic)
                          │  │  └ Tools ├─ Web search/extract (cached, trust-tiered)
                    Tasks │  └ Skills   ├─ Terminal (restricted) / Files / Image composer
                   (DB)   └ Memory      └─ LLM (OpenAI-compatible, optional, mockable)
Runner lifecycle: lease → recover checkpoint → poll (persisted offset) → checkpoint → graceful stop → dispatch next run
```

Database: PostgreSQL (asyncpg) in production, SQLite (aiosqlite) for dev/tests; Alembic migrations; 24 tables (see `cr_analyst/db/models.py`).

## 6. Risks

1. **Dynamic runner IP** — live API needs a static trusted egress; the bot reports this instead of silently failing.
2. Supercell blog markup change — parser is defensive, records `verification_status`, and a fixture test catches regressions.
3. Hero/evolution ownership fields may change — parsed opportunistically, unknowns reported.
4. Actions minutes / quota — restart cap, owner pause, failure notifications.
5. LLM hallucination — numbers in LLM output are cross-checked against the facts supplied; unsupported numbers trigger a fallback to the deterministic explanation.
6. Stale secondary stats — marked `secondary-stale` and never presented as current.
