"""CASP protocol HTTP client with discovery caching.

Used by the CASPProvider host bridge functions to call CASP providers
(e.g. LoomHarbor). These calls are Python-level and bypass the Lua
sandbox's _BLOCKED_PATTERNS security policy by design — they are
structured internal calls to known, configured provider URLs.
"""

from __future__ import annotations

import logging
import time

import httpx

log = logging.getLogger(__name__)

_DISCOVERY_TTL = 300.0  # 5 minutes

# url → (types_response_dict, expires_at monotonic)
_discovery_cache: dict[str, tuple[dict, float]] = {}

_CLIENT = httpx.Client(
    timeout=8.0,
    limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
)


def _provider_url(resource_type: str) -> str | None:
    """Return the URL of the provider that handles this resource type."""
    from ritesmith.config import get_settings

    providers = get_settings().casp_providers
    if not providers:
        return None

    # Priority: lower priority int wins. Within same priority, first listed wins.
    candidates = sorted(providers, key=lambda p: p.priority)
    for p in candidates:
        if not p.resource_types or resource_type in p.resource_types:
            return p.url.rstrip("/")
    return None


def get_types(provider_url: str) -> dict:
    """Fetch /casp/v1/types with 5-minute in-process cache."""
    now = time.monotonic()
    if provider_url in _discovery_cache:
        data, expires = _discovery_cache[provider_url]
        if now < expires:
            return data
    try:
        resp = _CLIENT.get(f"{provider_url}/casp/v1/types", timeout=3.0)
        resp.raise_for_status()
        data = resp.json()
        _discovery_cache[provider_url] = (data, now + _DISCOVERY_TTL)
        return data
    except Exception as exc:
        log.warning("CASP discovery failed for %s: %s", provider_url, exc)
        return {"resourceTypes": []}


def invalidate_discovery(provider_url: str | None = None) -> None:
    if provider_url:
        _discovery_cache.pop(provider_url, None)
    else:
        _discovery_cache.clear()


def query(resource_type: str, filters: dict, requires_capabilities: list[str]) -> dict:
    url = _provider_url(resource_type)
    if not url:
        return {
            "error": "no_provider",
            "message": f"No CASP provider configured for type '{resource_type}'",
        }
    try:
        resp = _CLIENT.post(
            f"{url}/casp/v1/resources/query",
            json={
                "resourceType": resource_type,
                "filters": filters,
                "requiresCapabilities": requires_capabilities,
            },
        )
        return resp.json()
    except Exception as exc:
        log.warning("CASP query failed: %s", exc)
        return {"error": "request_failed", "message": str(exc)}


def resolve(resource_type: str, capability: str, hint: str) -> dict:
    url = _provider_url(resource_type)
    if not url:
        return {"status": "invalid", "error": f"No CASP provider for '{resource_type}'"}
    try:
        resp = _CLIENT.post(
            f"{url}/casp/v1/resources/resolve",
            json={
                "resourceType": resource_type,
                "capability": capability,
                "hint": hint,
            },
        )
        return resp.json()
    except Exception as exc:
        log.warning("CASP resolve failed: %s", exc)
        return {"status": "invalid", "error": str(exc)}


def execute(resource_id: str, resource_type: str, capability: str, input_data: dict) -> dict:
    url = _provider_url(resource_type)
    if not url:
        return {
            "status": "failed",
            "errorCode": "NO_PROVIDER",
            "error": f"No CASP provider for '{resource_type}'",
        }
    try:
        resp = _CLIENT.post(
            f"{url}/casp/v1/capabilities/execute",
            json={
                "resourceId": resource_id,
                "resourceType": resource_type,
                "capability": capability,
                "input": input_data,
            },
        )
        return resp.json()
    except Exception as exc:
        log.warning("CASP execute failed: %s", exc)
        return {"status": "failed", "errorCode": "REQUEST_FAILED", "error": str(exc)}


def all_types_from_providers() -> list[dict]:
    """Collect resource types from all configured providers (for planning context)."""
    from ritesmith.config import get_settings

    providers = get_settings().casp_providers
    all_types: list[dict] = []
    seen: set[str] = set()
    for p in providers:
        url = p.url.rstrip("/")
        discovery = get_types(url)
        for rt in discovery.get("resourceTypes", []):
            if rt["type"] not in seen:
                all_types.append(rt)
                seen.add(rt["type"])
    return all_types
