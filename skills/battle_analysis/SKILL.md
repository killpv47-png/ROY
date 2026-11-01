---
name: battle_analysis
description: Deterministic analysis of recent and stored battles
version: 1
tags: [battle, battles, losses, matchup]
---
## When to Use
User asks why they lose, which cards counter them, or how their deck performs.

## Procedure
1. Poll the battle log, store new battles (dedupe by battle key).
2. Analyse API sample and local history separately; label each.
3. Report W/L/D, per-mode win rate, problem cards (loss-rate lift, min 2 losses), deck performance, level differential.
4. Map loss-role pressure to owned answer cards.

## Pitfalls
- No placement/timing data exists — never claim to know in-game mistakes.
- Small samples: always print the sample size.
- History only starts when tracking started.

## Verification
wins + losses + draws == sample_size.
