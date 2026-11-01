"""Secret redaction used by logging, terminal output, notifications and tool results."""
from __future__ import annotations

import logging
import re

_PATTERNS = [
    re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b"),                 # Telegram bot token
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),                   # GitHub tokens
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),                        # OpenAI style keys
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),  # JWT (Supercell keys)
    re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s'\"]+"),
    re.compile(r"(?i)((?:password|passwd|secret|token|api[_-]?key)\s*[=:]\s*)[^\s'\",;]+"),
    re.compile(r"(?i)(postgres(?:ql)?(?:\+asyncpg)?://[^:/\s]+:)[^@\s]+(@)"),
]

_extra_secrets: set[str] = set()


def register_secrets(values: list[str]) -> None:
    for v in values:
        if v and len(v) >= 6:
            _extra_secrets.add(v)


def redact(text: str | None) -> str:
    if not text:
        return text or ""
    out = str(text)
    for s in sorted(_extra_secrets, key=len, reverse=True):
        out = out.replace(s, "[REDACTED]")
    for p in _PATTERNS:
        if p.groups >= 2:
            out = p.sub(lambda m: m.group(1) + "[REDACTED]" + (m.group(2) if m.lastindex and m.lastindex >= 2 else ""), out)
        elif p.groups == 1:
            out = p.sub(lambda m: m.group(1) + "[REDACTED]", out)
        else:
            out = p.sub("[REDACTED]", out)
    return out


def mask(value: str | None, keep: int = 4) -> str:
    if not value:
        return "—"
    if len(value) <= keep * 2:
        return "•" * len(value)
    return f"{value[:keep]}…{value[-keep:]}"


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:  # pragma: no cover - trivial
        try:
            msg = record.getMessage()
        except Exception:
            return True
        record.msg = redact(msg)
        record.args = ()
        return True


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(RedactingFilter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    for noisy in ("httpx", "httpcore", "aiogram.event", "sqlalchemy.engine"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
