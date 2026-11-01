---
name: game_mode_analysis
description: Per game-mode performance breakdown
version: 1
tags: [mode, ladder, 2v2, challenge, tournament]
---
## Procedure
1. Group stored battles by gameMode.name and battle type.
2. Report games, win rate and most common opponent cards per mode.
3. Exclude 2v2 from 1v1 deck conclusions.

## Pitfalls
- Mode names come from the API verbatim and may change between seasons.
