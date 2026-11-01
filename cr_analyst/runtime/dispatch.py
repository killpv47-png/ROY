"""GitHub workflow chaining via the REST API (workflow_dispatch) using GH_PAT from Actions secrets.

POST /repos/{owner}/{repo}/actions/workflows/{workflow_file}/dispatches  {"ref": ..., "inputs": {...}}
Requires a fine-grained PAT with "Actions: Read and write" on this repository only. GITHUB_TOKEN cannot be
used because it does not trigger new workflow runs except for dispatch events created by a PAT/App token.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from ..security.redact import redact

log = logging.getLogger(__name__)
API = "https://api.github.com"


@dataclass
class DispatchResult:
    ok: bool
    status: int | None
    detail: str


class GitHubDispatcher:
    def __init__(self, token: str, repository: str, workflow_file: str, ref: str = "main",
                 transport: httpx.AsyncBaseTransport | None = None):
        self.token = token
        self.repository = repository
        self.workflow_file = workflow_file
        self.ref = ref
        self._transport = transport

    @property
    def configured(self) -> bool:
        return bool(self.token and self.repository and "/" in self.repository)

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "cr-analyst-runner"}

    async def dispatch(self, inputs: dict | None = None, attempts: int = 3) -> DispatchResult:
        if not self.configured:
            return DispatchResult(False, None, "not_configured (GH_PAT or GITHUB_REPOSITORY missing)")
        url = f"{API}/repos/{self.repository}/actions/workflows/{self.workflow_file}/dispatches"
        body = {"ref": self.ref, "inputs": {k: str(v) for k, v in (inputs or {}).items()}}
        last = DispatchResult(False, None, "no attempt")
        async with httpx.AsyncClient(timeout=20, transport=self._transport) as c:
            for i in range(attempts):
                try:
                    r = await c.post(url, json=body, headers=self._headers())
                except httpx.HTTPError as e:
                    last = DispatchResult(False, None, type(e).__name__)
                else:
                    if r.status_code in (204, 200):
                        return DispatchResult(True, r.status_code, "dispatched")
                    last = DispatchResult(False, r.status_code, redact(r.text[:200]))
                    if r.status_code in (401, 403, 404, 422):
                        break  # permanent: bad token / permissions / workflow not on default branch
                import asyncio
                await asyncio.sleep(2 * (i + 1))
        return last

    async def active_runs(self) -> int | None:
        """Count queued/in-progress runs of the runner workflow (duplicate-run protection)."""
        if not self.configured:
            return None
        url = f"{API}/repos/{self.repository}/actions/workflows/{self.workflow_file}/runs"
        n = 0
        async with httpx.AsyncClient(timeout=20, transport=self._transport) as c:
            for status in ("in_progress", "queued"):
                try:
                    r = await c.get(url, params={"status": status, "per_page": 5}, headers=self._headers())
                    if r.status_code == 200:
                        n += int(r.json().get("total_count", 0))
                except httpx.HTTPError:
                    return None
        return n
