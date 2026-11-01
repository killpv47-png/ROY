"""Centralised localisation. All user-facing strings live in `locales/<lang>.json`.

* 10 languages; Persian is the default, English the fallback for any missing key.
* RTL languages (fa, ar) get an RLM mark so mixed Latin/number content renders correctly in Telegram.
* Numbers are formatted with locale digits/separators; dates with localised month names.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from functools import lru_cache
from pathlib import Path

log = logging.getLogger(__name__)

LANGUAGES: dict[str, dict] = {
    "fa": {"name": "فارسی", "flag": "🇮🇷", "rtl": True, "digits": "۰۱۲۳۴۵۶۷۸۹", "dec": "٫", "group": "٬"},
    "en": {"name": "English", "flag": "🇬🇧", "rtl": False, "digits": None, "dec": ".", "group": ","},
    "ar": {"name": "العربية", "flag": "🇸🇦", "rtl": True, "digits": "٠١٢٣٤٥٦٧٨٩", "dec": "٫", "group": "٬"},
    "tr": {"name": "Türkçe", "flag": "🇹🇷", "rtl": False, "digits": None, "dec": ",", "group": "."},
    "de": {"name": "Deutsch", "flag": "🇩🇪", "rtl": False, "digits": None, "dec": ",", "group": "."},
    "fr": {"name": "Français", "flag": "🇫🇷", "rtl": False, "digits": None, "dec": ",", "group": "\u202f"},
    "es": {"name": "Español", "flag": "🇪🇸", "rtl": False, "digits": None, "dec": ",", "group": "."},
    "pt": {"name": "Português", "flag": "🇧🇷", "rtl": False, "digits": None, "dec": ",", "group": "."},
    "ru": {"name": "Русский", "flag": "🇷🇺", "rtl": False, "digits": None, "dec": ",", "group": "\u00a0"},
    "ja": {"name": "日本語", "flag": "🇯🇵", "rtl": False, "digits": None, "dec": ".", "group": ","},
}
DEFAULT_LANG = "fa"
FALLBACK_LANG = "en"
SUPPORTED = tuple(LANGUAGES)
RLM = "\u200f"
LOCALE_DIR = Path(__file__).parent / "locales"


@lru_cache
def _load(lang: str) -> dict[str, str]:
    p = LOCALE_DIR / f"{lang}.json"
    if not p.is_file():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def reload() -> None:
    _load.cache_clear()


def normalize_lang(lang: str | None) -> str:
    lang = (lang or "").split("-")[0].lower()
    return lang if lang in LANGUAGES else DEFAULT_LANG


def is_rtl(lang: str) -> bool:
    return LANGUAGES.get(lang, {}).get("rtl", False)


class _SafeDict(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def t(lang: str, key: str, **kwargs) -> str:
    lang = normalize_lang(lang)
    text = _load(lang).get(key)
    if text is None:
        text = _load(FALLBACK_LANG).get(key)
        if text is None:
            log.warning("missing i18n key %s", key)
            return key
    if kwargs:
        text = text.format_map(_SafeDict({k: fmt_value(lang, v) for k, v in kwargs.items()}))
    return text


def fmt_value(lang: str, v):
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, (int, float)):
        return fmt_number(lang, v)
    return v


def localize_digits(lang: str, s: str) -> str:
    digits = LANGUAGES.get(lang, {}).get("digits")
    if not digits:
        return s
    return s.translate(str.maketrans("0123456789", digits))


def fmt_number(lang: str, n: float | int, decimals: int | None = None) -> str:
    meta = LANGUAGES.get(normalize_lang(lang), LANGUAGES[FALLBACK_LANG])
    if decimals is None:
        decimals = 0 if isinstance(n, int) or float(n).is_integer() else (1 if abs(n) >= 10 else 2)
    s = f"{n:,.{decimals}f}"
    s = s.replace(",", "\x00").replace(".", meta["dec"]).replace("\x00", meta["group"])
    return localize_digits(lang, s)


def fmt_pct(lang: str, n: float) -> str:
    return fmt_number(lang, n, 1) + "%"


def fmt_date(lang: str, dt: datetime | None, with_time: bool = False) -> str:
    if dt is None:
        return "—"
    months = _load(lang).get("_months") or _load(FALLBACK_LANG).get("_months")
    names = months.split(",") if months else None
    if names and len(names) == 12:
        mon = names[dt.month - 1]
        if lang == "ja":
            s = f"{dt.year}年{dt.month}月{dt.day}日"
        elif lang in ("en",):
            s = f"{mon} {dt.day}, {dt.year}"
        else:
            s = f"{dt.day} {mon} {dt.year}"
    else:
        s = dt.strftime("%Y-%m-%d")
    if with_time:
        s += dt.strftime(" %H:%M") + " UTC"
    return localize_digits(lang, s)


def rtl_wrap(lang: str, text: str) -> str:
    """Prefix each line with RLM for RTL languages so Telegram aligns mixed content correctly."""
    if not is_rtl(lang):
        return text
    return "\n".join(RLM + ln if ln and not ln.startswith(RLM) else ln for ln in text.split("\n"))


def all_keys() -> set[str]:
    return set(_load(FALLBACK_LANG))


def button_texts(key: str) -> set[str]:
    """All translations of a button key (used to route reply-keyboard presses in any language)."""
    return {t(lang, key) for lang in LANGUAGES}
