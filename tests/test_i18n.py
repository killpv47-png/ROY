import json
import re
from pathlib import Path

import pytest

from cr_analyst import i18n
from cr_analyst.i18n import LANGUAGES, fmt_date, fmt_number, is_rtl, rtl_wrap, t

LOC = Path(i18n.__file__).parent / "locales"
EN = json.loads((LOC / "en.json").read_text(encoding="utf-8"))
PH = re.compile(r"\{([a-z_]+)\}")


def test_ten_languages_farsi_default():
    assert set(LANGUAGES) == {"fa", "en", "ar", "tr", "de", "fr", "es", "pt", "ru", "ja"}
    assert i18n.DEFAULT_LANG == "fa"


@pytest.mark.parametrize("lang", sorted({"fa", "en", "ar", "tr", "de", "fr", "es", "pt", "ru", "ja"}))
def test_locale_complete_and_placeholders_match(lang):
    d = json.loads((LOC / f"{lang}.json").read_text(encoding="utf-8"))
    assert set(d) == set(EN)
    for k, v in EN.items():
        assert set(PH.findall(v)) == set(PH.findall(d[k])), (lang, k)
        assert d[k].strip(), (lang, k)
    assert len(d["_months"].split(",")) == 12
    # every locale must render every key without KeyError
    for k in EN:
        t(lang, k, n=1, name="x", tag="#2PP", ip="1.2.3.4", step="s", seconds=10, err="e", code="c")


def test_rtl_and_digits():
    assert is_rtl("fa") and is_rtl("ar") and not is_rtl("en")
    assert rtl_wrap("fa", "a\nb").startswith("\u200f")
    assert rtl_wrap("en", "a") == "a"
    assert fmt_number("fa", 1234) != "1234"  # localized digits
    assert any(ch in fmt_number("fa", 5) for ch in "۵")
    assert "1" in fmt_number("en", 1)


def test_fallback_and_missing():
    assert t("xx", "yes") == t("fa", "yes")      # unknown language -> default
    assert t("en", "no_such_key_123") == "no_such_key_123"


def test_fmt_date_month_names():
    from datetime import datetime, timezone
    d = datetime(2026, 10, 5, tzinfo=timezone.utc)
    assert "October" in fmt_date("en", d)
    assert "Oktober" in fmt_date("de", d)
