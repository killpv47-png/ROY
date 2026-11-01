"""OpenAI-compatible chat client (works with OpenAI, OpenRouter, Nous Portal, local servers).

* Optional: when LLM_API_KEY is unset the agent runs fully deterministic (no LLM calls).
* Bounded retries; token usage tracked; output passed through a numeric fact-check guard.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from ..security.redact import redact

log = logging.getLogger(__name__)


@dataclass
class LLMResponse:
    content: str | None
    tool_calls: list[dict] = field(default_factory=list)  # [{"id", "name", "arguments"(dict)}]
    usage: dict = field(default_factory=dict)
    finish_reason: str | None = None


class LLMBackend(Protocol):
    async def chat(self, messages: list[dict], tools: list[dict] | None = None, **kw) -> LLMResponse: ...


class LLMError(Exception):
    pass


class OpenAICompatibleLLM:
    def __init__(self, api_key: str, model: str, base_url: str = "https://api.openai.com/v1", *,
                 temperature: float = 0.3, max_tokens: int = 900, timeout: float = 60.0,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.temperature = temperature
        self.max_tokens = max_tokens
        self._client = httpx.AsyncClient(timeout=timeout, transport=transport)
        self.usage_total = {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0}

    async def aclose(self) -> None:
        await self._client.aclose()

    async def chat(self, messages: list[dict], tools: list[dict] | None = None, **kw) -> LLMResponse:
        body: dict[str, Any] = {"model": kw.get("model") or self.model, "messages": messages,
                                "temperature": kw.get("temperature", self.temperature),
                                "max_tokens": kw.get("max_tokens", self.max_tokens)}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        last: Exception | None = None
        for attempt in range(3):
            try:
                r = await self._client.post(f"{self.base_url}/chat/completions", json=body,
                                            headers={"Authorization": f"Bearer {self.api_key}"})
                if r.status_code in (429, 500, 502, 503, 504):
                    raise LLMError(f"HTTP {r.status_code}")
                if r.status_code >= 400:
                    raise LLMError(f"HTTP {r.status_code}: {redact(r.text[:200])}")
                data = r.json()
                break
            except (httpx.HTTPError, LLMError) as e:
                last = e
                if isinstance(e, LLMError) and "HTTP 4" in str(e) and "429" not in str(e):
                    raise
                await asyncio.sleep(1.5 * (2 ** attempt))
        else:
            raise LLMError(f"LLM unavailable: {type(last).__name__}")
        choice = data["choices"][0]
        msg = choice.get("message") or {}
        calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            calls.append({"id": tc.get("id"), "name": fn.get("name"), "arguments": args})
        usage = data.get("usage") or {}
        self.usage_total["prompt_tokens"] += usage.get("prompt_tokens", 0)
        self.usage_total["completion_tokens"] += usage.get("completion_tokens", 0)
        self.usage_total["calls"] += 1
        return LLMResponse(content=msg.get("content"), tool_calls=calls, usage=usage,
                           finish_reason=choice.get("finish_reason"))


_NUM = re.compile(r"(?<![\w#])\d+(?:[.,]\d+)?%?")


def unsupported_numbers(text: str, facts: Any, allow: set[str] | None = None) -> list[str]:
    """Return numbers in `text` that do not appear anywhere in the supplied facts (anti-hallucination guard).
    Small integers (<= 10) are allowed since they are commonly ordinal/structural (e.g. "8 cards", "2 spells")."""
    blob = json.dumps(facts, ensure_ascii=False, default=str)
    fact_nums = {n.rstrip("%").replace(",", ".") for n in _NUM.findall(blob)}
    bad = []
    for n in _NUM.findall(text or ""):
        v = n.rstrip("%").replace(",", ".")
        try:
            if float(v) <= 10:
                continue
        except ValueError:
            continue
        if allow and v in allow:
            continue
        if v not in fact_nums and v.rstrip("0").rstrip(".") not in {f.rstrip("0").rstrip(".") for f in fact_nums}:
            bad.append(n)
    return bad
