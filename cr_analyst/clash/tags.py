"""Player / clan tag normalisation and URL encoding."""
from __future__ import annotations

import re
from urllib.parse import quote

VALID_TAG_CHARS = "0289PYLQGRJCUV"
_TAG_RE = re.compile(rf"^#[{VALID_TAG_CHARS}]{{3,14}}$")


class InvalidTag(ValueError):
    pass


def normalize_tag(raw: str) -> str:
    """Return canonical '#XXXX' tag. Accepts lowercase, missing '#', spaces, the common O→0 typo,
    and full RoyaleAPI / in-game profile links."""
    if raw is None:
        raise InvalidTag("empty tag")
    s = raw.strip()
    m = re.search(r"(?:tag=|/player/|/profile/|%23|#)([0-9A-Za-z]+)", s)
    if m and len(s) > 15:
        s = m.group(1)
    s = s.upper().replace(" ", "").replace("%23", "#").lstrip("#").replace("O", "0")
    tag = "#" + s
    if not _TAG_RE.match(tag):
        raise InvalidTag(f"invalid tag: {raw!r}")
    return tag


def encode_tag(tag: str) -> str:
    """URL path-segment encoding: '#ABC' -> '%23ABC'."""
    return quote(normalize_tag(tag), safe="")


def is_valid_tag(raw: str) -> bool:
    try:
        normalize_tag(raw)
        return True
    except InvalidTag:
        return False
