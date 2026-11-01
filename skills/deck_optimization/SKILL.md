---
name: deck_optimization
description: Personalised deck generation from the real collection
version: 1
tags: [deck, decks, build, optimize]
---
## When to Use
User asks for a deck (defensive, offensive, balanced, cycle, counter-attack, high-level, meta, custom).

## Procedure
1. Load the latest card snapshot (scan first if none or older than 24h).
2. Load meta trend (official balance direction, last 60 days) and battle loss-role pressure.
3. Run the deterministic `deck_generate` tool for the category (top 3, diverse win conditions).
4. Verify each deck: 8 unique owned cards, 1–2 win conditions, >=2 air defenders, >=1 spell.
5. Explain with the score breakdown; the LLM may phrase it but must not add numbers.
6. Offer substitutions, upgrade plan, image, and official copy link (base cards only).

## Pitfalls
- Never recommend a card the player does not own.
- Copy links do not carry evolutions/hero forms — tell the user to set them in-game.
- Roles inferred for uncatalogued cards must be disclosed as uncertainty.

## Verification
share_link contains exactly 8 ids with 26/27/28 prefixes; avg elixir recomputed from catalog costs.
