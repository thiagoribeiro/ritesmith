"""Host functions expostas a scripts Lua.

Cada função é um callable Python registrado por nome (ex: "http.get_json").
O sandbox injeta apenas as funções permitidas pelo runtime profile.

Profiles:
  transform_only   — json, text, time. Sem rede.
  readonly_network — tudo acima + http.request (GET/POST para domínios permitidos).
"""
import json as _json
import re
import time as _time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Callable

import httpx

from ritesmith.runtime._conversion import lua_to_python


# ------------------------------------------------------------------
# Configuração de segurança de rede
# ------------------------------------------------------------------

_BLOCKED_PATTERNS = [
    re.compile(r"^https?://localhost", re.IGNORECASE),
    re.compile(r"^https?://127\."),
    re.compile(r"^https?://10\."),
    re.compile(r"^https?://172\.(1[6-9]|2[0-9]|3[01])\."),
    re.compile(r"^https?://192\.168\."),
    re.compile(r"^https?://0\.0\.0\.0"),
    re.compile(r"^https?://\[::1\]"),
]

_HTTP_TIMEOUT = 5.0       # segundos
_MAX_RESPONSE_BYTES = 512 * 1024   # 512KB

_HTTP_CLIENT = httpx.Client(
    timeout=_HTTP_TIMEOUT,
    limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
)


def _is_url_allowed(url: str) -> bool:
    for pattern in _BLOCKED_PATTERNS:
        if pattern.match(url):
            return False
    return True


def _http_request(method: str, url: str, headers: dict | None = None, body: dict | None = None) -> dict:
    """Executa requisição HTTP síncrona (chamado de dentro do executor Lua)."""
    if not _is_url_allowed(url):
        return {"error": "blocked_url", "message": f"URL not allowed: {url}"}

    method = method.upper()
    if method not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
        return {"error": "invalid_method", "message": f"Method not allowed: {method}"}

    try:
        resp = _HTTP_CLIENT.request(
            method,
            url,
            json=body if body else None,
            headers=headers or {},
        )
        content = resp.content[:_MAX_RESPONSE_BYTES]
        try:
            data = _json.loads(content)
        except Exception:
            data = content.decode("utf-8", errors="replace")
        return {"status": resp.status_code, "body": data, "ok": resp.is_success, "error": None}
    except httpx.TimeoutException:
        return {"error": "timeout", "message": "Request timed out"}
    except Exception as e:
        return {"error": "request_failed", "message": str(e)}


# ------------------------------------------------------------------
# Registro de host functions
# ------------------------------------------------------------------

@dataclass
class HostFunctionDef:
    name: str
    profile: str          # "transform_only" | "readonly_network"
    callable: Callable


_REGISTRY: dict[str, HostFunctionDef] = {}


def _register(name: str, profile: str, fn: Callable) -> None:
    _REGISTRY[name] = HostFunctionDef(name=name, profile=profile, callable=fn)


def _json_encode(val) -> str:
    return _json.dumps(lua_to_python(val))


# json.*
_register("json.encode", "transform_only", _json_encode)
_register("json.decode", "transform_only", _json.loads)

# text.*
def _text_slugify(s: str) -> str:
    s = s.lower().strip()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s_-]+", "-", s)
    return re.sub(r"^-+|-+$", "", s)

_register("text.slugify", "transform_only", _text_slugify)
_register("text.upper", "transform_only", str.upper)
_register("text.lower", "transform_only", str.lower)
_register("text.strip", "transform_only", str.strip)

# time.*
_register("time.now_utc", "transform_only", lambda: datetime.now(UTC).isoformat())
_register("time.timestamp", "transform_only", _time.time)

# http.* (readonly_network profile)
_register("http.request", "readonly_network", _http_request)
_register(
    "http.get_json",
    "readonly_network",
    lambda url: _http_request("GET", url),
)
_register(
    "http.post_json",
    "readonly_network",
    lambda url, body: _http_request("POST", url, body=body),
)


# ------------------------------------------------------------------
# API pública
# ------------------------------------------------------------------

PROFILES: dict[str, set[str]] = {
    "transform_only":    {"transform_only"},
    "readonly_network":  {"transform_only", "readonly_network"},
    "notification":      {"transform_only", "readonly_network", "notification"},
    "sensitive_personal":{"transform_only", "readonly_network", "notification", "sensitive_personal"},
    "analytics_local":   {"transform_only", "analytics_local"},
    "filesystem_write":  {"transform_only", "filesystem_write"},
    "reporting":         {"transform_only", "readonly_network", "analytics_local",
                          "filesystem_write", "notification"},
    "trusted_internal":  {"transform_only", "readonly_network", "notification",
                          "sensitive_personal", "analytics_local", "filesystem_write",
                          "trusted_internal"},
}


def _load_providers() -> None:
    """Load host functions from all available tool providers into _REGISTRY."""
    import logging
    log = logging.getLogger(__name__)
    try:
        from ritesmith.runtime.providers import PROVIDERS
        for provider in PROVIDERS:
            if not provider.is_available():
                continue
            for name, fn_def in provider.lua_functions().items():
                _REGISTRY[name] = fn_def
            log.debug("Loaded provider: %s (%d functions)", provider.namespace, len(provider.lua_functions()))
    except Exception as e:
        log.warning("Provider loading failed: %s", e)


_load_providers()


def get_capability(name: str) -> HostFunctionDef | None:
    return _REGISTRY.get(name)


def get_available_provider_capabilities() -> list[dict]:
    """Return structured metadata for all available provider capabilities (namespace.* names)."""
    try:
        from ritesmith.runtime.providers import PROVIDERS
        available_ns = {p.namespace for p in PROVIDERS if p.is_available()}
    except Exception:
        available_ns = set()
    result = []
    for name, fn in _REGISTRY.items():
        if "." not in name:
            continue
        if name.split(".")[0] not in available_ns:
            continue
        result.append({
            "capability_name": name,
            "description": fn.description or f"{name} capability",
            "input_schema": fn.input_schema or {},
            "output_schema": fn.output_schema or {},
        })
    return sorted(result, key=lambda c: c["capability_name"])


def get_functions_for_profile(profile: str) -> dict[str, HostFunctionDef]:
    allowed = PROFILES.get(profile, {"transform_only"})
    return {name: fn for name, fn in _REGISTRY.items() if fn.profile in allowed}


def list_names_for_profile(profile: str) -> list[str]:
    return sorted(get_functions_for_profile(profile).keys())
