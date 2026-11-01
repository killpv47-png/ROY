"""Deck image composer (Pillow). Uses official card icons (iconUrls from the API catalog) when reachable,
otherwise draws a labelled tile — never a fabricated card art."""
from __future__ import annotations

import asyncio
import io
import logging
from pathlib import Path

import httpx
from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger(__name__)

W_CARD, H_CARD, PAD, HEADER, FOOTER = 150, 180, 14, 70, 56
RARITY_COLORS = {"common": (120, 160, 200), "rare": (230, 150, 50), "epic": (170, 80, 210),
                 "legendary": (90, 210, 200), "champion": (240, 200, 60)}
BG = (24, 28, 44)


def _font(size: int):
    for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf", "DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            continue
    return ImageFont.load_default()


class IconCache:
    def __init__(self, cache_dir: Path):
        self.dir = Path(cache_dir) / "icons"
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, card_id: int, evo: bool) -> Path:
        return self.dir / f"{card_id}{'_evo' if evo else ''}.png"

    async def get(self, client: httpx.AsyncClient, card_id: int, url: str | None, evo: bool = False) -> Image.Image | None:
        p = self._path(card_id, evo)
        if p.is_file():
            try:
                return Image.open(p).convert("RGBA")
            except Exception:
                p.unlink(missing_ok=True)
        if not url or not url.startswith("https://"):
            return None
        try:
            r = await client.get(url, timeout=10)
            r.raise_for_status()
            img = Image.open(io.BytesIO(r.content)).convert("RGBA")
            img.save(p)
            return img
        except Exception as e:  # network or decode failure → labelled tile fallback
            log.debug("icon fetch failed for %s: %s", card_id, type(e).__name__)
            return None


def _tile(card: dict, icon: Image.Image | None, evo_slot: bool) -> Image.Image:
    tile = Image.new("RGBA", (W_CARD, H_CARD), (40, 46, 70, 255))
    d = ImageDraw.Draw(tile)
    color = RARITY_COLORS.get((card.get("rarity") or "").lower(), (140, 140, 140))
    d.rounded_rectangle([1, 1, W_CARD - 2, H_CARD - 2], radius=12, outline=color, width=4)
    if icon is not None:
        ic = icon.copy()
        ic.thumbnail((W_CARD - 24, H_CARD - 50))
        tile.alpha_composite(ic, ((W_CARD - ic.width) // 2, 8))
    else:
        name = card["name"]
        words, lines, cur = name.split(), [], ""
        for w in words:
            if len(cur + " " + w) > 11 and cur:
                lines.append(cur)
                cur = w
            else:
                cur = (cur + " " + w).strip()
        lines.append(cur)
        f = _font(17)
        y = 40
        for ln in lines[:3]:
            tw = d.textlength(ln, font=f)
            d.text(((W_CARD - tw) / 2, y), ln, fill=(235, 235, 245), font=f)
            y += 24
    # elixir drop
    if card.get("elixir") is not None:
        d.ellipse([6, 6, 40, 40], fill=(200, 60, 220))
        e = str(card["elixir"])
        f = _font(18)
        d.text((23 - d.textlength(e, font=f) / 2, 11), e, fill="white", font=f)
    # level banner
    f = _font(18)
    lvl = f"Lv {card['level']}"
    d.rectangle([0, H_CARD - 36, W_CARD, H_CARD], fill=(15, 18, 30))
    d.text(((W_CARD - d.textlength(lvl, font=f)) / 2, H_CARD - 31), lvl, fill=(255, 220, 90), font=f)
    if evo_slot:
        d.rounded_rectangle([W_CARD - 54, 6, W_CARD - 6, 30], radius=8, fill=(130, 60, 230))
        d.text((W_CARD - 49, 8), "EVO", fill="white", font=_font(15))
    return tile


def compose_deck_image(cards: list[dict], title: str, subtitle: str, icons: list[Image.Image | None],
                       evo_ids: set[int]) -> bytes:
    cols, rows = 4, 2
    width = cols * W_CARD + (cols + 1) * PAD
    height = HEADER + rows * H_CARD + (rows + 1) * PAD + FOOTER
    img = Image.new("RGBA", (width, height), BG + (255,))
    d = ImageDraw.Draw(img)
    d.text((PAD, 14), title[:48], fill=(255, 255, 255), font=_font(26))
    for i, (c, ic) in enumerate(zip(cards, icons)):
        x = PAD + (i % cols) * (W_CARD + PAD)
        y = HEADER + PAD + (i // cols) * (H_CARD + PAD)
        img.alpha_composite(_tile(c, ic, c["id"] in evo_ids), (x, y))
    d.text((PAD, height - FOOTER + 14), subtitle[:70], fill=(200, 205, 225), font=_font(19))
    out = io.BytesIO()
    img.convert("RGB").save(out, "PNG", optimize=True)
    return out.getvalue()


async def render_deck(deck: dict, title: str, subtitle: str, cache_dir: Path,
                      client: httpx.AsyncClient | None = None) -> bytes:
    """`deck` is DeckResult.to_dict()."""
    cache = IconCache(cache_dir)
    evo_ids = set(deck.get("evolution_ids") or [])
    own = client is None
    client = client or httpx.AsyncClient()
    try:
        icons = await asyncio.gather(*[
            cache.get(client, c["id"], (c.get("evo_icon_url") if c["id"] in evo_ids and c.get("evo_icon_url")
                                        else c.get("icon_url")), evo=c["id"] in evo_ids and bool(c.get("evo_icon_url")))
            for c in deck["cards"]])
    finally:
        if own:
            await client.aclose()
    return compose_deck_image(deck["cards"], title, subtitle, list(icons), evo_ids)
