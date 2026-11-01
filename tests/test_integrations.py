"""External integrations with mocked transports: Tavily/DDG search, LLM, GitHub dispatch, Clash API."""
import json

import httpx
import pytest

from cr_analyst.agent.llm import OpenAICompatibleLLM, unsupported_numbers
from cr_analyst.agent.web import WebResearcher, is_public_url, source_tier
from cr_analyst.runtime.dispatch import GitHubDispatcher


async def test_tavily_used_with_bearer_and_official_first(db):
    seen = {}

    def handler(req: httpx.Request):
        if req.url.host == "api.tavily.com":
            seen["auth"] = req.headers["authorization"]
            seen["body"] = json.loads(req.content)
            return httpx.Response(200, json={"results": [
                {"title": "Fan site", "url": "https://example.com/meta", "content": "x"},
                {"title": "Balance", "url": "https://supercell.com/en/games/clashroyale/blog/x", "content": "y",
                 "published_date": "2026-10-01"}]})
        raise AssertionError(f"unexpected request {req.url}")
    w = WebResearcher(db, "tavily", transport=httpx.MockTransport(handler), tavily_key="tvly-test-key")
    res = await w.search("clash royale balance", limit=5)
    assert seen["auth"] == "Bearer tvly-test-key"
    assert seen["body"]["query"] == "clash royale balance" and seen["body"]["max_results"] == 5
    assert w.last_provider == "tavily"
    assert res[0]["tier"] == "official"
    # cached: a second call does not hit the network
    w2 = WebResearcher(db, "tavily", transport=httpx.MockTransport(lambda r: httpx.Response(500)),
                       tavily_key="tvly-test-key")
    assert await w2.search("clash royale balance", limit=5) == res


async def test_tavily_failure_falls_back_to_duckduckgo(db):
    html = ('<a class="result__a" href="https://royaleapi.com/x">RoyaleAPI</a> ... '
            '<a class="result__snippet">meta decks</a>')

    def handler(req):
        if req.url.host == "api.tavily.com":
            return httpx.Response(401, json={"detail": "bad key"})
        if "duckduckgo" in req.url.host:
            return httpx.Response(200, text=html)
        raise AssertionError(req.url)
    w = WebResearcher(db, "tavily", transport=httpx.MockTransport(handler), tavily_key="bad")
    res = await w.search("meta decks")
    assert w.last_provider == "duckduckgo" and res[0]["url"] == "https://royaleapi.com/x"


def test_ssrf_guard_and_tiers():
    assert is_public_url("https://supercell.com/x")
    for u in ("http://127.0.0.1/", "http://localhost/", "http://10.0.0.1/", "file:///etc/passwd",
              "http://169.254.169.254/latest"):
        assert not is_public_url(u)
    assert source_tier("https://supercell.com/blog") == "official"


async def test_llm_tool_calls_parsed_and_auth():
    def handler(req):
        assert req.headers["authorization"] == "Bearer sk-test"
        body = json.loads(req.content)
        assert body["tools"][0]["function"]["name"] == "card_search"
        return httpx.Response(200, json={"choices": [{"finish_reason": "tool_calls", "message": {
            "content": None, "tool_calls": [{"id": "c1", "function": {"name": "card_search",
                                                                        "arguments": "{\"query\": \"hog\"}"}}]}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2}})
    llm = OpenAICompatibleLLM("sk-test", "m", transport=httpx.MockTransport(handler))
    tools = [{"type": "function", "function": {"name": "card_search", "parameters": {}}}]
    r = await llm.chat([{"role": "user", "content": "hi"}], tools=tools)
    assert r.tool_calls == [{"id": "c1", "name": "card_search", "arguments": {"query": "hog"}}]
    assert llm.usage_total["calls"] == 1
    await llm.aclose()


async def test_llm_4xx_not_retried():
    n = {"c": 0}

    def handler(req):
        n["c"] += 1
        return httpx.Response(401, text="invalid api key sk-abcdefghijklmnopqrstuvwxyz")
    llm = OpenAICompatibleLLM("sk-x", "m", transport=httpx.MockTransport(handler))
    from cr_analyst.agent.llm import LLMError
    with pytest.raises(LLMError) as ei:
        await llm.chat([{"role": "user", "content": "x"}])
    assert n["c"] == 1 and "abcdefghijklmnop" not in str(ei.value)
    await llm.aclose()


def test_numeric_hallucination_guard():
    facts = {"win_rate": 54.2, "trophies": 7012, "cards": [{"level": 14}]}
    assert unsupported_numbers("Your win rate is 54.2% at 7012 trophies with 8 cards", facts) == []
    assert unsupported_numbers("Hog Rider has a 61% win rate", facts) == ["61%"]


async def test_github_dispatch_success_and_auth_failure():
    seen = {}

    def ok(req):
        seen["url"] = str(req.url)
        seen["body"] = json.loads(req.content)
        seen["auth"] = req.headers["authorization"]
        return httpx.Response(204)
    d = GitHubDispatcher("ghp_test", "me/repo", "bot-runner.yml", "main", transport=httpx.MockTransport(ok))
    r = await d.dispatch({"chain_index": 3})
    assert r.ok and seen["url"].endswith("/repos/me/repo/actions/workflows/bot-runner.yml/dispatches")
    assert seen["body"] == {"ref": "main", "inputs": {"chain_index": "3"}} and seen["auth"] == "Bearer ghp_test"
    calls = {"n": 0}

    def denied(req):
        calls["n"] += 1
        return httpx.Response(403, json={"message": "Resource not accessible"})
    d2 = GitHubDispatcher("ghp_test", "me/repo", "bot-runner.yml", transport=httpx.MockTransport(denied))
    r2 = await d2.dispatch({})
    assert not r2.ok and r2.status == 403 and calls["n"] == 1  # permanent error: no retry storm
    assert not (await GitHubDispatcher("", "me/repo", "x.yml").dispatch()).ok
