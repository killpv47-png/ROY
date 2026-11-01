"""Verify every locale has every key of en.json and identical {placeholders}. Exit 1 on problems."""
import json, re, sys
from pathlib import Path
D = Path(__file__).resolve().parent.parent / "cr_analyst" / "i18n" / "locales"
LANGS = ["fa", "en", "ar", "tr", "de", "fr", "es", "pt", "ru", "ja"]
ph = lambda s: sorted(set(re.findall(r"\{([a-z_]+)\}", s)))
en = json.loads((D / "en.json").read_text(encoding="utf-8"))
bad = 0
for lang in LANGS:
    p = D / f"{lang}.json"
    if not p.is_file():
        print(f"{lang}: MISSING FILE"); bad += 1; continue
    d = json.loads(p.read_text(encoding="utf-8"))
    missing = [k for k in en if k not in d]
    extra = [k for k in d if k not in en]
    phbad = [k for k in en if k in d and ph(en[k]) != ph(d[k])]
    months = len(d.get("_months", "").split(","))
    print(f"{lang}: {len(d)} keys, missing={len(missing)} extra={len(extra)} placeholder_mismatch={len(phbad)} months={months}")
    if missing: print("   missing:", missing[:20])
    if phbad: print("   placeholders:", phbad[:20])
    bad += bool(missing or phbad or months != 12)
sys.exit(1 if bad else 0)
