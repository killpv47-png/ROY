"""Egress / trusted-proxy manager.

Rules (see docs/RESEARCH.md §4):
* Supercell developer keys are IP allow-listed. The authenticated client asks `resolve_route()` before every
  request; the route is only `verified` when the observed egress IP matches the owner-approved IP.
* Only owner-configured, `trusted` proxy profiles are ever used. No public proxy lists.
* Each profile is tested (egress IP + latency) before activation; credentials are never sent to an
  unverified endpoint. If the observed IP drifts from the approved IP, requests are blocked and reported —
  we never silently switch to another egress.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import httpx
from sqlalchemy import select

from ..clash.client import Route
from ..db.models import ApiHealthRecord, AppConfig, EncryptedCredential, ProxyProfile
from ..security.crypto import SecretBox, fingerprint

log = logging.getLogger(__name__)

IPV4_SERVICES = ["https://api.ipify.org", "https://ipv4.icanhazip.com", "https://checkip.amazonaws.com"]
IPV6_SERVICES = ["https://api64.ipify.org", "https://ipv6.icanhazip.com"]
ROUTE_CACHE_SECONDS = 300


@dataclass
class EgressInfo:
    ipv4: str | None
    ipv6: str | None
    latency_ms: int | None
    via: str
    error: str | None = None


def _valid_ip(s: str, version: int | None = None) -> str | None:
    try:
        ip = ipaddress.ip_address(s.strip())
    except ValueError:
        return None
    if version and ip.version != version:
        return None
    return str(ip)


def proxy_url(p: ProxyProfile, password: str | None) -> str:
    auth = ""
    if p.username:
        auth = quote(p.username, safe="") + (":" + quote(password, safe="") if password else "") + "@"
    return f"{p.scheme}://{auth}{p.host}:{p.port}"


class NetworkManager:
    def __init__(self, db, box: SecretBox | None, *, expected_ip: str = "", is_github_runner: bool = False,
                 transport_factory=None):
        self.db = db
        self.box = box
        self.expected_ip_env = expected_ip.strip()
        self.is_github_runner = is_github_runner
        self._transport_factory = transport_factory
        self._route_cache: tuple[float, Route] | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ IP detection
    async def _probe(self, services: list[str], proxy: str | None, version: int) -> tuple[str | None, int | None, str | None]:
        kw: dict = {"timeout": 8}
        if self._transport_factory:
            kw["transport"] = self._transport_factory(proxy)
        elif proxy:
            kw["proxy"] = proxy
        last_err = None
        async with httpx.AsyncClient(**kw) as c:
            for url in services:
                t0 = time.monotonic()
                try:
                    r = await c.get(url)
                    ip = _valid_ip(r.text, version)
                    if r.status_code == 200 and ip:
                        return ip, int((time.monotonic() - t0) * 1000), None
                    last_err = f"bad response from {url}"
                except httpx.HTTPError as e:
                    last_err = type(e).__name__
        return None, None, last_err

    async def detect_egress(self, proxy: str | None = None, label: str = "direct") -> EgressInfo:
        (v4, lat, err4), (v6, _, _) = await asyncio.gather(self._probe(IPV4_SERVICES, proxy, 4),
                                                          self._probe(IPV6_SERVICES, proxy, 6))
        info = EgressInfo(ipv4=v4, ipv6=v6, latency_ms=lat, via=label, error=None if v4 or v6 else err4)
        await self.record_health("egress", bool(v4 or v6), None, lat, v4 or v6, f"via={label} err={info.error}")
        return info

    # ------------------------------------------------------------------ config
    async def get_config(self, key: str, default=None):
        async with self.db.session() as s:
            row = await s.get(AppConfig, key)
            return row.value.get("v", default) if row else default

    async def set_config(self, key: str, value) -> None:
        async with self.db.session() as s:
            row = await s.get(AppConfig, key)
            if row:
                row.value = {"v": value}
            else:
                s.add(AppConfig(key=key, value={"v": value}))

    async def approved_ip(self) -> str | None:
        return (await self.get_config("approved_egress_ip")) or self.expected_ip_env or None

    async def approve_ip(self, ip: str) -> str:
        ip = _valid_ip(ip)
        if not ip:
            raise ValueError("invalid IP")
        await self.set_config("approved_egress_ip", ip)
        self.invalidate()
        return ip

    def invalidate(self) -> None:
        self._route_cache = None

    # ------------------------------------------------------------------ proxy profiles
    async def list_profiles(self) -> list[ProxyProfile]:
        async with self.db.session() as s:
            return list((await s.execute(select(ProxyProfile).order_by(ProxyProfile.id))).scalars())

    async def add_profile(self, name: str, region: str, scheme: str, host: str, port: int,
                          username: str | None = None, password: str | None = None) -> ProxyProfile:
        scheme = scheme.lower()
        if scheme not in ("http", "https", "socks5"):
            raise ValueError("scheme must be http, https or socks5")
        if not (1 <= int(port) <= 65535) or not host or any(ch in host for ch in "/@ "):
            raise ValueError("invalid host/port")
        async with self.db.session() as s:
            cred_id = None
            if password:
                if not self.box:
                    raise ValueError("APP_ENCRYPTION_KEY required to store proxy credentials")
                cred = EncryptedCredential(user_id=None, kind="proxy_auth", label=f"proxy:{name}"[:64],
                                           ciphertext=self.box.encrypt(password), fingerprint=fingerprint(password))
                s.add(cred)
                await s.flush()
                cred_id = cred.id
            p = ProxyProfile(name=name[:64], region=region[:64], scheme=scheme, host=host, port=int(port),
                             username=username or None, credential_id=cred_id, trusted=True)
            s.add(p)
            await s.flush()
            return p

    async def update_profile(self, profile_id: int, **fields) -> ProxyProfile | None:
        allowed = {"name", "region", "host", "port", "scheme", "username"}
        async with self.db.session() as s:
            p = await s.get(ProxyProfile, profile_id)
            if not p:
                return None
            for k, v in fields.items():
                if k in allowed and v is not None:
                    setattr(p, k, int(v) if k == "port" else v)
            p.observed_ipv4 = p.observed_ipv6 = None  # must be re-tested
            p.last_success_at = None
            if p.is_active:
                p.is_active = False
            self.invalidate()
            return p

    async def remove_profile(self, profile_id: int) -> bool:
        async with self.db.session() as s:
            p = await s.get(ProxyProfile, profile_id)
            if not p:
                return False
            if p.credential_id:
                cred = await s.get(EncryptedCredential, p.credential_id)
                if cred:
                    await s.delete(cred)
            await s.delete(p)
        self.invalidate()
        return True

    async def _profile_url(self, p: ProxyProfile) -> str:
        password = None
        if p.credential_id and self.box:
            async with self.db.session() as s:
                cred = await s.get(EncryptedCredential, p.credential_id)
                password = self.box.decrypt(cred.ciphertext) if cred else None
        return proxy_url(p, password)

    async def test_profile(self, profile_id: int) -> dict:
        async with self.db.session() as s:
            p = await s.get(ProxyProfile, profile_id)
        if not p:
            return {"ok": False, "error": "not_found"}
        if not p.trusted:
            return {"ok": False, "error": "untrusted"}
        url = await self._profile_url(p)
        info = await self.detect_egress(url, label=f"proxy:{p.name}")
        async with self.db.session() as s:
            p = await s.get(ProxyProfile, profile_id)
            p.tests_total += 1
            if info.ipv4 or info.ipv6:
                p.tests_ok += 1
                p.observed_ipv4, p.observed_ipv6 = info.ipv4, info.ipv6
                p.latency_ms = info.latency_ms
                p.last_success_at = datetime.now(timezone.utc)
            reliability = round(p.tests_ok / p.tests_total * 100, 1)
        self.invalidate()
        return {"ok": bool(info.ipv4 or info.ipv6), "ipv4": info.ipv4, "ipv6": info.ipv6,
                "latency_ms": info.latency_ms, "reliability": reliability, "error": info.error}

    async def activate_profile(self, profile_id: int | None) -> dict:
        """Activate a profile (tested first) or None for direct egress."""
        if profile_id is None:
            async with self.db.session() as s:
                for p in (await s.execute(select(ProxyProfile))).scalars():
                    p.is_active = False
            self.invalidate()
            return {"ok": True, "mode": "direct"}
        res = await self.test_profile(profile_id)
        if not res["ok"]:
            return {"ok": False, "error": res.get("error") or "test_failed"}
        async with self.db.session() as s:
            for p in (await s.execute(select(ProxyProfile))).scalars():
                p.is_active = p.id == profile_id
        self.invalidate()
        return {"ok": True, "mode": "proxy", **res}

    async def active_profile(self) -> ProxyProfile | None:
        async with self.db.session() as s:
            return (await s.execute(select(ProxyProfile).where(ProxyProfile.is_active.is_(True)))).scalar_one_or_none()

    # ------------------------------------------------------------------ route resolution
    async def resolve_route(self) -> Route:
        async with self._lock:
            now = time.monotonic()
            if self._route_cache and now - self._route_cache[0] < ROUTE_CACHE_SECONDS:
                return self._route_cache[1]
            route = await self._resolve()
            if route.verified:
                self._route_cache = (now, route)
            return route

    async def _resolve(self) -> Route:
        approved = await self.approved_ip()
        profile = await self.active_profile()
        purl, label = (await self._profile_url(profile), f"proxy:{profile.name}") if profile else (None, "direct")
        info = await self.detect_egress(purl, label)
        observed = {info.ipv4, info.ipv6} - {None}
        if not observed:
            return Route(purl, None, label, False, f"egress_unreachable ({info.error})")
        if not approved:
            # No approved IP recorded yet: allow, but warn so the owner can approve the observed IP.
            warning = "no_approved_ip"
            if self.is_github_runner and not profile:
                warning = "github_runner_dynamic_ip"
            return Route(purl, info.ipv4 or info.ipv6, label, True, warning)
        if approved in observed:
            return Route(purl, approved, label, True, None)
        return Route(purl, info.ipv4 or info.ipv6, label, False,
                     f"egress_ip_mismatch observed={info.ipv4 or info.ipv6} approved={approved}")

    async def status(self) -> dict:
        approved = await self.approved_ip()
        prof = await self.active_profile()
        last = await self.last_health("egress")
        return {"approved_ip": approved, "active_profile": prof.name if prof else None,
                "active_region": prof.region if prof else None,
                "profile_ip": (prof.observed_ipv4 or prof.observed_ipv6) if prof else None,
                "last_checked_ip": last.egress_ip if last else None,
                "last_checked_at": last.ts.isoformat() if last else None,
                "github_runner": self.is_github_runner,
                "stable_ip_possible": bool(prof) or not self.is_github_runner}

    # ------------------------------------------------------------------ health records
    async def record_health(self, component: str, ok: bool, status: int | None, latency: int | None,
                            egress: str | None, detail: str | None) -> None:
        try:
            async with self.db.session() as s:
                s.add(ApiHealthRecord(component=component, ok=ok, status_code=status, latency_ms=latency,
                                      egress_ip=egress, detail=(detail or "")[:500]))
        except Exception:  # health logging must never break the caller
            log.debug("health record failed", exc_info=True)

    async def last_health(self, component: str) -> ApiHealthRecord | None:
        async with self.db.session() as s:
            return (await s.execute(select(ApiHealthRecord).where(ApiHealthRecord.component == component)
                                    .order_by(ApiHealthRecord.id.desc()).limit(1))).scalar_one_or_none()

    async def health_summary(self, hours: int = 24) -> dict[str, dict]:
        since = datetime.now(timezone.utc) - timedelta(hours=hours)
        async with self.db.session() as s:
            rows = (await s.execute(select(ApiHealthRecord).where(ApiHealthRecord.ts >= since))).scalars().all()
        out: dict[str, dict] = {}
        for r in rows:
            d = out.setdefault(r.component, {"ok": 0, "fail": 0, "last_ok": None, "last_detail": None})
            d["ok" if r.ok else "fail"] += 1
            d["last_ok"] = r.ok
            d["last_detail"] = r.detail
        return out
