"""Official Clash Royale API client (https://developer.clashroyale.com).

* Bearer developer key (IP allow-listed) — NOT a player credential.
* Network route (direct / trusted proxy) is resolved and verified by NetworkManager before
  any authenticated request; the Authorization header is never sent over an unverified route.
* Bounded exponential backoff for 429 / 5xx / timeouts; permanent errors are not retried.
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import httpx

from .tags import encode_tag

log = logging.getLogger(__name__)


class ClashAPIError(Exception):
    code = "unknown"
    retryable = False

    def __init__(
        self,
        message: str = "",
        status: int | None = None,
        reason: str | None = None,
    ):
        super().__init__(message or self.code)
        self.status = status
        self.reason = reason


class NotConfigured(ClashAPIError):
    code = "not_configured"


class InvalidIP(ClashAPIError):
    code = "invalid_ip"


class AccessDenied(ClashAPIError):
    code = "access_denied"


class NotFound(ClashAPIError):
    code = "not_found"


class BadRequest(ClashAPIError):
    code = "bad_request"


class RateLimited(ClashAPIError):
    code = "rate_limited"
    retryable = True


class Maintenance(ClashAPIError):
    code = "maintenance"
    retryable = True


class ServerError(ClashAPIError):
    code = "server_error"
    retryable = True


class NetworkError(ClashAPIError):
    code = "network"
    retryable = True


class RouteRejected(ClashAPIError):
    """Egress verification failed — request not sent."""

    code = "route_rejected"


@dataclass
class Route:
    """Verified network route for authenticated requests."""

    proxy_url: str | None
    egress_ip: str | None
    label: str
    verified: bool
    warning: str | None = None


RouteResolver = Callable[[], Awaitable[Route]]
TokenResolver = Callable[[], Awaitable[str | None]]


def _clean_log_field(value: Any, limit: int = 300) -> str:
    """Convert an API error field into a bounded, single-line log value."""
    if value is None:
        return ""

    text = " ".join(str(value).split())
    return text[:limit]


def classify(status: int, body: dict | None) -> ClashAPIError:
    """Convert an official API response into a typed exception."""

    payload = body if isinstance(body, dict) else {}

    reason = str(payload.get("reason") or "")
    message = str(payload.get("message") or "")

    normalized_reason = reason.casefold()

    if status == 403:
        is_invalid_ip = (
            "invalidip" in normalized_reason
            or re.search(
                r"\bip(?:\s+address)?\b",
                message,
                flags=re.IGNORECASE,
            ) is not None
        )

        if is_invalid_ip:
            return InvalidIP(message, status, reason)

        return AccessDenied(message, status, reason)

    if status == 401:
        return AccessDenied(message, status, reason)

    if status == 404:
        return NotFound(message, status, reason)

    if status == 400:
        return BadRequest(message, status, reason)

    if status == 429:
        return RateLimited(message, status, reason)

    if status == 503:
        return Maintenance(message, status, reason)

    return ServerError(message, status, reason)


class ClashClient:

    def __init__(
        self,
        base_url: str,
        token_resolver: TokenResolver,
        route_resolver: RouteResolver | None = None,
        *,
        timeout: float = 15.0,
        max_retries: int = 3,
        transport: httpx.AsyncBaseTransport | None = None,
        health_hook: Callable[..., Awaitable[None]] | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self._token = token_resolver
        self._route = route_resolver

        self.timeout = timeout
        self.max_retries = max_retries

        self._transport = transport
        self._health = health_hook

        self._clients: dict[str | None, httpx.AsyncClient] = {}

    def _client(self, proxy: str | None) -> httpx.AsyncClient:
        if proxy not in self._clients:

            kw: dict[str, Any] = {
                "timeout": self.timeout,
                "headers": {
                    "Accept": "application/json",
                    "User-Agent": "cr-analyst/1.0",
                },
            }

            if self._transport is not None:
                kw["transport"] = self._transport

            elif proxy:
                kw["proxy"] = proxy

            self._clients[proxy] = httpx.AsyncClient(**kw)

        return self._clients[proxy]

    async def aclose(self) -> None:
        for client in self._clients.values():
            await client.aclose()

        self._clients.clear()

    async def request(
        self,
        path: str,
        params: dict | None = None,
    ) -> Any:

        # ---------------------------------------------------------
        # Resolve developer API key
        # ---------------------------------------------------------

        token = await self._token()

        if not token:
            raise NotConfigured(
                "No Clash Royale developer API key configured"
            )

        # ---------------------------------------------------------
        # Resolve and verify network route
        # ---------------------------------------------------------

        route = (
            await self._route()
            if self._route
            else Route(
                proxy_url=None,
                egress_ip=None,
                label="direct",
                verified=True,
            )
        )

        if not route.verified:
            raise RouteRejected(
                route.warning or "Egress route not verified"
            )

        url = f"{self.base_url}{path}"

        attempt = 0

        # ---------------------------------------------------------
        # API request with bounded retries
        # ---------------------------------------------------------

        while True:

            attempt += 1
            t0 = time.monotonic()

            try:

                resp = await self._client(
                    route.proxy_url
                ).get(
                    url,
                    params=params,
                    headers={
                        "Authorization": f"Bearer {token}"
                    },
                )

            except (
                httpx.TimeoutException,
                httpx.TransportError,
            ) as exc:

                err: ClashAPIError = NetworkError(
                    type(exc).__name__
                )

                status = None
                latency = int(
                    (time.monotonic() - t0) * 1000
                )

                await self._record(
                    False,
                    status,
                    latency,
                    route,
                    f"{path}: {err.code}",
                )

            else:

                status = resp.status_code

                latency = int(
                    (time.monotonic() - t0) * 1000
                )

                # -------------------------------------------------
                # Successful response
                # -------------------------------------------------

                if status == 200:

                    await self._record(
                        True,
                        status,
                        latency,
                        route,
                        path,
                    )

                    return resp.json()

                # -------------------------------------------------
                # Parse error response
                # -------------------------------------------------

                try:
                    parsed_body = resp.json()

                    if isinstance(parsed_body, dict):
                        body = parsed_body
                    else:
                        body = None

                except Exception:
                    body = None

                # -------------------------------------------------
                # Classify API error
                # -------------------------------------------------

                err = classify(status, body)

                # -------------------------------------------------
                # IMPORTANT: Diagnostic logging
                # -------------------------------------------------

                if status in (400, 401, 403):

                    if isinstance(body, dict):

                        reason = _clean_log_field(
                            body.get("reason"),
                            200,
                        )

                        message = _clean_log_field(
                            body.get("message"),
                            300,
                        )

                    else:

                        reason = ""

                        # Only a short preview of the response body
                        # is logged if the response was not JSON.
                        message = _clean_log_field(
                            resp.text,
                            300,
                        )

                    log.warning(
                        "Clash API diagnostic: "
                        "status=%s "
                        "reason=%s "
                        "message=%s "
                        "route=%s "
                        "egress_ip=%s",
                        status,
                        reason,
                        message,
                        route.label,
                        route.egress_ip or "unknown",
                    )

                # -------------------------------------------------
                # Record API health information
                # -------------------------------------------------

                await self._record(
                    False,
                    status,
                    latency,
                    route,
                    f"{path}: {err.code}",
                )

            # -----------------------------------------------------
            # Retry handling
            # -----------------------------------------------------

            if not err.retryable or attempt > self.max_retries:
                raise err

            delay = (
                min(
                    8.0,
                    0.6 * 2 ** (attempt - 1),
                )
                + random.uniform(0, 0.3)
            )

            if status == 429:
                delay = max(delay, 1.5)

            log.info(
                "Clash API %s; retry %d in %.1fs",
                err.code,
                attempt,
                delay,
            )

            await asyncio.sleep(delay)

    async def _record(
        self,
        ok,
        status,
        latency,
        route: Route,
        detail,
    ):

        if self._health:

            try:

                await self._health(
                    ok=ok,
                    status=status,
                    latency=latency,
                    egress=route.egress_ip,
                    detail=detail,
                )

            except Exception:
                pass

    # -------------------------------------------------------------
    # Official Clash Royale API endpoints
    # -------------------------------------------------------------

    async def get_player(self, tag: str) -> dict:
        return await self.request(
            f"/players/{encode_tag(tag)}"
        )

    async def get_battlelog(self, tag: str) -> list[dict]:
        return await self.request(
            f"/players/{encode_tag(tag)}/battlelog"
        )

    async def get_upcoming_chests(self, tag: str) -> dict:
        return await self.request(
            f"/players/{encode_tag(tag)}/upcomingchests"
        )

    async def get_cards(self) -> dict:
        return await self.request("/cards")

    async def get_clan(self, tag: str) -> dict:
        return await self.request(
            f"/clans/{encode_tag(tag)}"
        )

    async def get_clan_members(self, tag: str) -> dict:
        return await self.request(
            f"/clans/{encode_tag(tag)}/members"
        )

    async def get_river_race(self, tag: str) -> dict:
        return await self.request(
            f"/clans/{encode_tag(tag)}/currentriverrace"
        )

    async def search_tournaments(self, name: str) -> dict:
        return await self.request(
            "/tournaments",
            params={
                "name": name,
                "limit": 10,
            },
        )

    async def get_tournament(self, tag: str) -> dict:
        return await self.request(
            f"/tournaments/{encode_tag(tag)}"
        )

    async def get_global_tournaments(self) -> dict:
        return await self.request(
            "/globaltournaments"
        )
