"""Parser for official Supercell Clash Royale blog posts (balance changes).

The blog is a Next.js site; each article embeds `__NEXT_DATA__` with `publishDate`, `title` and a
`bodyCollection` of rich-text blocks. Balance posts contain headings like "Balance Update – October 19th",
section headings (NERFS / BUFFS / REWORKS / BUG FIXES) and one `FeatureBlock` per card whose text contains
lines of the form "Attribute: old → new (±x%)". Only values written in the official post are stored.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

OFFICIAL_BLOG = "https://supercell.com/en/games/clashroyale/blog/"
_MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august",
                                        "september", "october", "november", "december"], 1)}
_SECTION = [("nerf", "nerf"), ("buff", "buff"), ("rework", "rework"), ("bug", "bugfix"), ("fix", "bugfix")]
_CHANGE_RE = re.compile(r"^\s*(?P<attr>[^:\n]{2,80}?)\s*:\s*(?P<old>[^→\n]+?)\s*→\s*(?P<new>[^\n(]+?)\s*"
                        r"(?:\((?P<pct>[+\-−]?\d+(?:\.\d+)?)%\))?\s*$")
# Fallback when the post omits the colon, e.g. "Damage 53 → 71 (+33%)": old value must start with a digit.
_CHANGE_NOCOLON_RE = re.compile(r"^\s*(?P<attr>[A-Za-z][^:\n→\d]{1,80}?)\s+(?P<old>\d[^→\n]*?)\s*→\s*"
                                r"(?P<new>[^\n(]+?)\s*(?:\((?P<pct>[+\-−]?\d+(?:\.\d+)?)%\))?\s*$")
_NUM_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")


def _match_change(line: str):
    return _CHANGE_RE.match(line) or _CHANGE_NOCOLON_RE.match(line)


@dataclass
class ParsedChange:
    card_name: str
    form: str
    change_type: str
    attribute: str
    old_value: str | None
    new_value: str | None
    change_pct: float | None
    note: str | None
    published_at: datetime | None
    effective_at: datetime | None
    source_url: str
    verification: str = "official"

    @property
    def content_hash(self) -> str:
        key = f"{self.source_url}|{self.card_name}|{self.form}|{self.attribute}|{self.old_value}|{self.new_value}"
        return hashlib.sha256(key.encode()).hexdigest()

    def to_dict(self) -> dict:
        d = asdict(self)
        d["published_at"] = self.published_at.isoformat() if self.published_at else None
        d["effective_at"] = self.effective_at.isoformat() if self.effective_at else None
        d["content_hash"] = self.content_hash
        return d


def extract_next_data(html: str) -> dict | None:
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def _rich_text(node) -> str:
    """Flatten Contentful rich-text JSON into text with newlines between paragraphs."""
    if isinstance(node, dict):
        if node.get("nodeType") == "text":
            return node.get("value", "")
        parts = [_rich_text(c) for c in node.get("content", [])]
        sep = "\n" if node.get("nodeType") in ("document", "unordered-list", "ordered-list") else ""
        txt = sep.join(parts)
        if node.get("nodeType", "").startswith(("heading", "paragraph", "list-item")):
            txt += "\n"
        return txt
    if isinstance(node, list):
        return "\n".join(_rich_text(n) for n in node)
    return ""


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _effective_from_heading(heading: str, published: datetime | None) -> datetime | None:
    m = re.search(r"(january|february|march|april|may|june|july|august|september|october|november|december)\s+"
                  r"(\d{1,2})", heading.lower())
    if not m or not published:
        return None
    month, day = _MONTHS[m.group(1)], int(m.group(2))
    year = published.year + (1 if month < published.month - 6 else 0)
    try:
        return datetime(year, month, day, tzinfo=timezone.utc)
    except ValueError:
        return None


def _pct(old: str, new: str, given: str | None) -> float | None:
    if given:
        return float(given.replace("−", "-"))
    o, n = _NUM_RE.findall(old), _NUM_RE.findall(new)
    if len(o) == 1 and len(n) == 1 and float(o[0]) != 0:
        return round((float(n[0]) - float(o[0])) / abs(float(o[0])) * 100, 1)
    return None


def _form_and_name(title: str) -> tuple[str, str]:
    t = title.strip()
    if re.search(r"\bevolution\b|\bevo\b", t, re.I):
        return "evolution", re.sub(r"\s*\b(evolution|evo)\b\s*", " ", t, flags=re.I).strip()
    if re.match(r"^hero\s+", t, re.I):
        return "hero", re.sub(r"^hero\s+", "", t, flags=re.I).strip()
    return "base", t


def parse_balance_article(next_data: dict, source_url: str) -> list[ParsedChange]:
    pp = (next_data.get("props") or {}).get("pageProps") or {}
    published = _parse_iso(pp.get("publishDate"))
    changes: list[ParsedChange] = []
    section = "neutral"
    effective = published
    for block in pp.get("bodyCollection") or []:
        typ = block.get("__typename")
        if typ == "TextBlock":
            content = ((block.get("text") or {}).get("json") or {}).get("content") or []
            for node in content:
                nt = node.get("nodeType", "")
                txt = _rich_text(node).strip()
                if not txt:
                    continue
                if nt.startswith("heading"):
                    low = txt.lower()
                    if "balance update" in low or re.search(r"(january|february|march|april|may|june|july|august|"
                                                            r"september|october|november|december)\s+\d", low):
                        eff = _effective_from_heading(txt, published)
                        effective = eff or published
                        section = "neutral"
                        continue
                    for needle, val in _SECTION:
                        if needle in low:
                            section = val
                            break
        elif typ == "FeatureBlock":
            title = (block.get("title") or "").strip()
            if not title:
                continue
            form, name = _form_and_name(title)
            text = _rich_text((block.get("featureText") or {}).get("json") or {})
            lines = [ln.strip() for ln in text.replace("\r", "").split("\n") if ln.strip()]
            parsed_any = False
            notes = []
            n_parsed = 0
            for ln in lines:
                m = _match_change(ln)
                if m:
                    n_parsed += 1
                    parsed_any = True
                    changes.append(ParsedChange(
                        card_name=name, form=form, change_type=section, attribute=m.group("attr").strip(),
                        old_value=m.group("old").strip(), new_value=m.group("new").strip(),
                        change_pct=_pct(m.group("old"), m.group("new"), m.group("pct")),
                        note=None, published_at=published, effective_at=effective, source_url=source_url))
                else:
                    notes.append(ln)
            if not parsed_any and notes:
                # qualitative change without numbers (e.g. "Removed Attack Delay after Ability")
                changes.append(ParsedChange(
                    card_name=name, form=form, change_type=section, attribute=notes[0][:96], old_value=None,
                    new_value=None, change_pct=None, note=" ".join(notes[1:])[:500] or None,
                    published_at=published, effective_at=effective, source_url=source_url))
            elif parsed_any and notes:
                for c in changes[-n_parsed:]:
                    c.note = " ".join(notes)[:500]
    return changes


def parse_blog_index(next_data: dict, base: str = "https://supercell.com") -> list[dict]:
    arts = ((next_data.get("props") or {}).get("pageProps") or {}).get("articles") or []
    out = []
    for a in arts:
        link = a.get("linkUrl") or ""
        out.append({"title": (a.get("title") or "").strip(), "url": base + link if link.startswith("/") else link,
                    "published_at": _parse_iso(a.get("publishDate"))})
    return out


def is_balance_article(title: str) -> bool:
    t = title.lower()
    return ("balance" in t and "merge tactics" not in t) or "card changes" in t
