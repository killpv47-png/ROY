"""Web research tools: search + content extraction, with trust tiers, dedupe and a DB-backed TTL cache."""
from __future__ import annotations

import hashlib
import html as htmllib
import ipaddress
import logging
import re
import socket
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, unquote, urlparse

import httpx
from sqlalchemy import delete, select

from ..db.models import ResearchCache

log = logging.getLogger(__name__)
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"

OFFICIAL_DOMAINS = ("supercell.com", "clashroyale.com", "developer.clashroyale.com", "support.clashroyale.com")
SECONDARY_DOMAINS = ("royaleapi.com", "royaleapi.github.io", "deckshop.pro", "statsroyale.com", "github.com")


def source_tier(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    if any(host == d or host.endswith("." + d) for d in OFFICIAL_DOMAINS):
        return "official"
    if any(host == d or host.endswith("." + d) for d in SECONDARY_DOMAINS):
        return "secondary"
    return "unverified"


def is_public_url(url: str) -> bool:
    """SSRF guard: only http(s) to public addresses."""
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        return False
    try:
        infos = socket.getaddrinfo(p.hostname, None)
    except socket.gaierror:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return False
    return True


def html_to_text(html: str, max_chars: int = 6000) -> str:
    html = re.sub(r"(?is)<(script|style|noscript|svg|nav|footer|header)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?i)<br\s*/?>|</p>|</h\d>|</li>|</tr>", "\n", html)
    text = re.sub(r"<[^>]+>", " ", html)
    text = htmllib.unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text).strip()
    return text[:max_chars]


class ResearchCacheStore:
    def __init__(self, db):
        self.db = db

    @staticmethod
    def key(kind: str, q: str) -> str:
        return kind + ":" + hashlib.sha256(q.strip().lower().encode()).hexdigest()[:40]

    async def get(self, kind: str, q: str):
        async with self.db.session() as s:
            row = await s.get(ResearchCache, self.key(kind, q))
            if not row:
                return None
            exp = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=timezone.utc)
            if exp < datetime.now(timezone.utc):
                await s.delete(row)
                return None
            return row.payload.get("v")

    async def put(self, kind: str, q: str, value, ttl_hours: float) -> None:
        k = self.key(kind, q)
        async with self.db.session() as s:
            row = await s.get(ResearchCache, k)
            exp = datetime.now(timezone.utc) + timedelta(hours=ttl_hours)
            if row:
                row.payload, row.expires_at = {"v": value}, exp
            else:
                s.add(ResearchCache(key=k, kind=kind, payload={"v": value}, expires_at=exp))

    async def purge_expired(self) -> int:
        async with self.db.session() as s:
            res = await s.execute(delete(ResearchCache).where(ResearchCache.expires_at < datetime.now(timezone.utc)))
            return res.rowcount or 0


class WebResearcher:
    def __init__(self, db, provider: str = "duckduckgo", api_key: str = "",
                 transport: httpx.AsyncBaseTransport | None = None, *, tavily_key: str = "",
                 tavily_depth: str = "basic"):
        self.cache = ResearchCacheStore(db)
        self.provider = provider
        self.api_key = api_key
        self.tavily_key = tavily_key
        self.tavily_depth = tavily_depth if tavily_depth in ("basic", "advanced", "fast", "ultra-fast") else "basic"
        self._transport = transport
        self.last_provider: str | None = None

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=15, follow_redirects=True, headers={"User-Agent": UA},
                                 transport=self._transport)

    async def search(self, query: str, limit: int = 6) -> list[dict]:
        cached = await self.cache.get("search", query)
        if cached is not None:
            return cached
        results: list[dict] = []
        order = []
        if self.provider == "tavily" and self.tavily_key:
            order.append("tavily")
        if self.provider == "brave" and self.api_key:
            order.append("brave")
        order.append("duckduckgo")  # always-available fallback
        last_err: Exception | None = None
        for prov in order:
            try:
                results = await {"tavily": self._tavily, "brave": self._brave, "duckduckgo": self._ddg}[prov](query, limit)
                self.last_provider = prov
                if results:
                    break
            except (httpx.HTTPError, ValueError, KeyError) as e:
                last_err = e
                log.warning("search provider %s failed: %s", prov, type(e).__name__)
        if not results and last_err is not None:
            raise RuntimeError(f"all search providers failed ({type(last_err).__name__})")
        seen, out = set(), []
        for r in results:
            u = r["url"].split("#")[0]
            if u in seen:
                continue
            seen.add(u)
            r["tier"] = source_tier(u)
            out.append(r)
        out.sort(key=lambda r: {"official": 0, "secondary": 1}.get(r["tier"], 2))
        await self.cache.put("search", query, out[:limit], ttl_hours=6)
        return out[:limit]

    async def _ddg(self, query: str, limit: int) -> list[dict]:
        async with self._client() as c:
            r = await c.post("https://html.duckduckgo.com/html/", data={"q": query})
            r.raise_for_status()
        out = []
        for m in re.finditer(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>.*?'
                             r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>', r.text, re.S):
            href = htmllib.unescape(m.group(1))
            if "uddg=" in href:
                href = unquote(parse_qs(urlparse(href).query).get("uddg", [href])[0])
            out.append({"title": html_to_text(m.group(2), 200), "url": href,
                        "snippet": html_to_text(m.group(3), 300)})
            if len(out) >= limit * 2:
                break
        return out

    async def _tavily(self, query: str, limit: int) -> list[dict]:
        """Tavily Search API (https://docs.tavily.com) — POST /search with Bearer key."""
        body = {"query": query, "search_depth": self.tavily_depth, "max_results": max(1, min(limit, 20)),
                "include_answer": False, "include_raw_content": False, "include_published_date": True}
        async with self._client() as c:
            r = await c.post("https://api.tavily.com/search", json=body,
                             headers={"Authorization": f"Bearer {self.tavily_key}",
                                      "Content-Type": "application/json"})
            if r.status_code in (401, 403):
                raise ValueError("tavily key rejected")
            r.raise_for_status()
        out = []
        for x in r.json().get("results") or []:
            out.append({"title": (x.get("title") or "")[:200], "url": x.get("url", ""),
                        "snippet": html_to_text(x.get("content") or "", 400),
                        "published": x.get("published_date"), "score": x.get("score"), "provider": "tavily"})
        return out

    async def _brave(self, query: str, limit: int) -> list[dict]:
        async with self._client() as c:
            r = await c.get("https://api.search.brave.com/res/v1/web/search", params={"q": query, "count": limit},
                            headers={"X-Subscription-Token": self.api_key, "Accept": "application/json"})
            r.raise_for_status()
        return [{"title": x.get("title", ""), "url": x.get("url", ""), "snippet": html_to_text(x.get("description", ""), 300)}
                for x in (r.json().get("web") or {}).get("results", [])]

    async def extract(self, url: str, max_chars: int = 6000) -> dict:
        if not is_public_url(url):
            raise ValueError("URL not allowed (non-public or unsupported scheme)")
        cached = await self.cache.get("extract", url)
        if cached is not None:
            return cached
        async with self._client() as c:
            r = await c.get(url)
        ctype = r.headers.get("content-type", "")
        if r.status_code >= 400:
            result = {"url": url, "status": r.status_code, "tier": source_tier(url), "text": "",
                      "error": f"HTTP {r.status_code}"}
        elif "html" not in ctype and "text" not in ctype and "json" not in ctype:
            result = {"url": url, "status": r.status_code, "tier": source_tier(url), "text": "",
                      "error": f"unsupported content type {ctype}"}
        else:
            title = re.search(r"(?is)<title[^>]*>(.*?)</title>", r.text)
            result = {"url": url, "status": r.status_code, "tier": source_tier(url),
                      "title": html_to_text(title.group(1), 200) if title else None,
                      "text": html_to_text(r.text, max_chars) if "html" in ctype else r.text[:max_chars]}
        await self.cache.put("extract", url, result, ttl_hours=12)
        return result
