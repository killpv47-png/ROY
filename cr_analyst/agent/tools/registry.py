"""Hermes-style tool registry: schemas, permissions, dispatch, structured results, error wrapping."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ...security.redact import redact

log = logging.getLogger(__name__)


@dataclass
class ToolResult:
    ok: bool
    data: Any = None
    error: str | None = None
    error_code: str | None = None
    source: str | None = None        # provenance label: official_api|official_blog|secondary|local_db|calculated|web
    duration_ms: int = 0

    def to_llm(self, max_chars: int = 3500) -> str:
        """Compact, redacted JSON for model context."""
        payload = {"ok": self.ok, "source": self.source}
        if self.ok:
            payload["data"] = self.data
        else:
            payload["error"] = self.error
            payload["error_code"] = self.error_code
        s = json.dumps(payload, ensure_ascii=False, default=str)
        s = redact(s)
        return s if len(s) <= max_chars else s[: max_chars - 20] + '…"truncated"}'


@dataclass
class ToolContext:
    user_id: int
    telegram_id: int
    role: str
    permissions: set[str]
    task_id: str | None = None
    chat_id: int | None = None
    language: str = "en"
    confirmed: bool = False          # set only after an explicit owner confirmation (destructive ops)
    extras: dict = field(default_factory=dict)


Handler = Callable[[ToolContext, dict], Awaitable[ToolResult]]


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    handler: Handler
    toolset: str
    permission: str = "agent.ask"
    timeout: float = 60.0
    llm_exposed: bool = True

    def schema(self) -> dict:
        return {"type": "function", "function": {"name": self.name, "description": self.description,
                                                 "parameters": self.parameters}}


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, Tool] = {}
        self.calls: list[dict] = []   # in-memory audit of the current process (bounded)

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool {tool.name}")
        self._tools[tool.name] = tool

    def tool(self, name: str, description: str, parameters: dict, *, toolset: str, permission: str = "agent.ask",
             timeout: float = 60.0, llm_exposed: bool = True):
        def deco(fn: Handler) -> Handler:
            self.register(Tool(name, description, parameters, fn, toolset, permission, timeout, llm_exposed))
            return fn
        return deco

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def toolsets(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for t in self._tools.values():
            out.setdefault(t.toolset, []).append(t.name)
        return out

    def schemas(self, ctx: ToolContext, toolsets: set[str] | None = None) -> list[dict]:
        """Only tools the caller is allowed to use (and in the requested toolsets) are exposed to the model."""
        return [t.schema() for t in self._tools.values()
                if t.llm_exposed and t.permission in ctx.permissions and (toolsets is None or t.toolset in toolsets)]

    async def call(self, name: str, ctx: ToolContext, args: dict | None = None) -> ToolResult:
        args = args or {}
        tool = self._tools.get(name)
        t0 = time.monotonic()
        if tool is None:
            return ToolResult(False, error=f"unknown tool {name}", error_code="unknown_tool")
        if tool.permission not in ctx.permissions:
            res = ToolResult(False, error="permission denied", error_code="permission_denied")
        else:
            try:
                res = await asyncio.wait_for(tool.handler(ctx, args), timeout=tool.timeout)
            except asyncio.TimeoutError:
                res = ToolResult(False, error=f"tool timed out after {tool.timeout}s", error_code="timeout")
            except asyncio.CancelledError:
                raise
            except Exception as e:  # error wrapping: tools never crash the agent loop
                code = getattr(e, "code", None) or type(e).__name__
                log.warning("tool %s failed: %s", name, redact(str(e)))
                res = ToolResult(False, error=redact(str(e))[:500] or type(e).__name__, error_code=str(code))
        res.duration_ms = int((time.monotonic() - t0) * 1000)
        if res.error:
            res.error = redact(res.error)
        self.calls.append({"tool": name, "ok": res.ok, "ms": res.duration_ms, "task": ctx.task_id,
                           "user": ctx.user_id, "code": res.error_code})
        del self.calls[:-500]
        return res
