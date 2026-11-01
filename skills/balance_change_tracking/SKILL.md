---
name: balance_change_tracking
description: Ingest and version official Supercell balance changes
version: 1
tags: [balance, buff, nerf, patch, rework]
---
## When to Use
Scheduled meta update or a question about a specific card's changes.

## Procedure
1. Fetch the official blog index (`__NEXT_DATA__.props.pageProps.articles`).
2. Select balance articles (exclude Merge Tactics posts).
3. Parse each FeatureBlock: "Attribute: old → new (±x%)"; heading "Balance Update – <Month Day>" sets effective date.
4. Store with content hash (dedupe), publication date, effective date, source URL, verification=official.
5. Never overwrite stat versions; secondary baseline stats become flagged as pre-dating the change.

## Pitfalls
- Some lines omit the colon ("Damage 53 → 71"); the parser handles both.
- Qualitative changes (no numbers) are stored with old/new = null.
- "Hero X" and "X Evolution" are forms of X, not new cards.

## Verification
Re-running ingestion on the same article adds 0 rows.
