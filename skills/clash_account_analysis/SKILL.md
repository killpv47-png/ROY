---
name: clash_account_analysis
description: Full official-API account scan with availability report
version: 1
tags: [scan, account, profile, collection]
---
## When to Use
User asks for a full scan, profile overview or card collection analysis.

## Procedure
1. Resolve the player's primary tag from memory/DB; validate with `normalize_tag`.
2. Verify the network route (approved egress IP) before any authenticated call.
3. Call `clash_scan` (profile → catalog if missing → battlelog → upcomingchests → clan).
4. Persist snapshot + card snapshot + new battles; compare with previous snapshot.
5. Present: profile, collection by rarity/level, evolutions, tower troops, upgrade priorities.
6. Always print the availability report: retrieved / stored / calculated / unavailable.

## Pitfalls
- API levels are rarity-relative: display level = level + (globalMax - maxLevel).
- Evolution / tower-troop / hero fields are only reported when present in the response.
- 403 accessDenied.invalidIp means the egress IP is not on the key's allow-list — run api_troubleshooting.

## Verification
Card count equals len(player.cards); no metric is shown for an unavailable field.
