---
name: card_meta_research
description: Research current meta from official then secondary sources
version: 1
tags: [meta, research, trend, season]
---
## When to Use
Questions about the current meta, strongest cards, or "what changed recently".

## Procedure
1. Check `research_cache`; reuse results newer than the TTL.
2. Run `meta_refresh` (official Supercell blog index → balance posts).
3. Use `web_search` only for context; tag results by tier (official / secondary / unverified).
4. Answer with publication + effective dates and source URLs.

## Pitfalls
- Social media and wikis are unverified: never store them as facts.
- RoyaleAPI website blocks bots (403); use search snippets, not scraping.

## Verification
Every factual claim maps to a stored source URL.
