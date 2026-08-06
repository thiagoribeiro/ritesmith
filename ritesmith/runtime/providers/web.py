"""web.* host functions — web search (Brave/Exa) and page fetch."""

from __future__ import annotations

import logging

import httpx

from ritesmith.config import get_settings
from ritesmith.runtime.providers.base import HostFunctionDef, MCPToolDef, ToolProvider

logger = logging.getLogger(__name__)

_TIMEOUT = 10.0
_MAX_BYTES = 32 * 1024  # 32 KB
_BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"
_EXA_SEARCH_URL = "https://api.exa.ai/search"

_BLOCKED_PREFIXES = (
    "http://localhost",
    "https://localhost",
    "http://127.",
    "https://127.",
    "http://10.",
    "https://10.",
    "http://192.168.",
    "https://192.168.",
    "http://0.0.0.0",
    "https://0.0.0.0",
)

_HTTP_CLIENT = httpx.Client(
    timeout=_TIMEOUT,
    limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
    follow_redirects=True,
    headers={"User-Agent": "RiteSmith/0.1"},
)


def _is_allowed(url: str) -> bool:
    return not any(url.startswith(p) for p in _BLOCKED_PREFIXES)


def _web_search(query: str, limit: int = 5) -> list[dict]:
    api_key = get_settings().web_search_api_key
    if not api_key:
        return [{"error": "WEB_SEARCH_API_KEY not configured"}]
    try:
        r = _HTTP_CLIENT.get(
            _BRAVE_SEARCH_URL,
            params={"q": query, "count": min(limit, 20)},
            headers={"Accept": "application/json", "X-Subscription-Token": api_key},
        )
        data = r.json()
        results = data.get("web", {}).get("results", [])
        return [
            {"title": x.get("title"), "url": x.get("url"), "snippet": x.get("description")}
            for x in results
        ]
    except Exception as e:
        logger.warning("web.search failed: %s", e)
        return [{"error": str(e)}]


def _web_search_semantic(query: str, limit: int = 5) -> list[dict]:
    api_key = get_settings().exa_api_key
    if not api_key:
        return [{"error": "EXA_API_KEY not configured"}]
    try:
        r = _HTTP_CLIENT.post(
            _EXA_SEARCH_URL,
            json={
                "query": query,
                "numResults": min(limit, 10),
                "contents": {"text": {"maxCharacters": 500}},
            },
            headers={"x-api-key": api_key, "Content-Type": "application/json"},
        )
        data = r.json()
        return [
            {"title": x.get("title"), "url": x.get("url"), "text": x.get("text", "")}
            for x in data.get("results", [])
        ]
    except Exception as e:
        logger.warning("web.search_semantic failed: %s", e)
        return [{"error": str(e)}]


def _web_fetch(url: str) -> str:
    if not _is_allowed(url):
        return f"[blocked] URL not allowed: {url}"
    try:
        import html2text as _h2t

        r = _HTTP_CLIENT.get(url)
        raw = r.content[:_MAX_BYTES].decode("utf-8", errors="replace")
        h = _h2t.HTML2Text()
        h.ignore_links = False
        h.ignore_images = True
        return h.handle(raw).strip()
    except Exception as e:
        logger.warning("web.fetch failed: %s", e)
        return f"[error] {e}"


def _web_fetch_json(url: str) -> dict | list:
    if not _is_allowed(url):
        return {"error": f"URL not allowed: {url}"}
    try:
        r = _HTTP_CLIENT.get(url)
        return r.json()
    except Exception as e:
        logger.warning("web.fetch_json failed: %s", e)
        return {"error": str(e)}


class WebProvider(ToolProvider):
    namespace = "web"
    profile = "readonly_network"
    risk_level = "low"
    side_effects = "external_read"

    def is_available(self) -> bool:
        # fetch and fetch_json always work; search requires at least one key
        return True

    def lua_functions(self) -> dict[str, HostFunctionDef]:
        return {
            "web.search": HostFunctionDef(
                "web.search",
                "readonly_network",
                _web_search,
                description="Search the web via Brave Search. Returns a list of {title, url, snippet} results.",
                input_schema={
                    "type": "object",
                    "required": ["query"],
                    "properties": {
                        "query": {"type": "string"},
                        "limit": {"type": "integer", "default": 5},
                    },
                },
                output_schema={
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "url": {"type": "string"},
                            "snippet": {"type": "string"},
                        },
                    },
                },
            ),
            "web.search_semantic": HostFunctionDef(
                "web.search_semantic",
                "readonly_network",
                _web_search_semantic,
                description="Semantic web search via Exa. Returns results ranked by meaning with text extracts.",
                input_schema={
                    "type": "object",
                    "required": ["query"],
                    "properties": {
                        "query": {"type": "string"},
                        "limit": {"type": "integer", "default": 5},
                    },
                },
                output_schema={
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "url": {"type": "string"},
                            "text": {"type": "string"},
                        },
                    },
                },
            ),
            "web.fetch": HostFunctionDef(
                "web.fetch",
                "readonly_network",
                _web_fetch,
                description="Fetch a URL and return its content as plain text (HTML stripped).",
                input_schema={
                    "type": "object",
                    "required": ["url"],
                    "properties": {
                        "url": {"type": "string"},
                    },
                },
                output_schema={"type": "string"},
            ),
            "web.fetch_json": HostFunctionDef(
                "web.fetch_json",
                "readonly_network",
                _web_fetch_json,
                description="Fetch a JSON endpoint. Returns the parsed response. If the JSON root is an array it is wrapped as {result: [...]}; if it is an object its fields are directly accessible.",
                input_schema={
                    "type": "object",
                    "required": ["url"],
                    "properties": {
                        "url": {"type": "string"},
                    },
                },
                output_schema={
                    "type": "object",
                    "properties": {
                        "result": {"description": "Set when the JSON response is an array"},
                        "<field>": {
                            "description": "Direct field access when the JSON response is an object"
                        },
                    },
                },
            ),
        }

    def mcp_tools(self) -> list[MCPToolDef]:
        import asyncio

        async def _search(query: str, limit: int = 5, **_) -> list[dict]:
            return await asyncio.to_thread(_web_search, query, limit)

        async def _search_semantic(query: str, limit: int = 5, **_) -> list[dict]:
            return await asyncio.to_thread(_web_search_semantic, query, limit)

        async def _fetch(url: str, **_) -> str:
            return await asyncio.to_thread(_web_fetch, url)

        return [
            MCPToolDef(
                name="web_search",
                description="Search the web using Brave Search. Returns title, URL and snippet for each result.",
                input_schema={
                    "type": "object",
                    "required": ["query"],
                    "properties": {
                        "query": {"type": "string"},
                        "limit": {"type": "integer", "default": 5, "maximum": 20},
                    },
                },
                fn=_search,
            ),
            MCPToolDef(
                name="web_search_semantic",
                description="Semantic web search via Exa. Returns results ranked by meaning, with text extracts.",
                input_schema={
                    "type": "object",
                    "required": ["query"],
                    "properties": {
                        "query": {"type": "string"},
                        "limit": {"type": "integer", "default": 5, "maximum": 10},
                    },
                },
                fn=_search_semantic,
            ),
            MCPToolDef(
                name="web_fetch",
                description="Fetch a web page and return its content as clean plain text (HTML stripped).",
                input_schema={
                    "type": "object",
                    "required": ["url"],
                    "properties": {"url": {"type": "string", "format": "uri"}},
                },
                fn=_fetch,
            ),
        ]
