"""Tool provider registry.

PROVIDERS is the single source of truth for all available tool providers.
Import from here in host_functions.py (Lua sandbox) and in the MCP server.

Plugins: installed packages can add providers through the ``ritesmith.providers``
entry-point group (a ToolProvider subclass or instance). A plugin whose namespace
matches a built-in replaces it. A plugin that fails to load is logged and skipped.
``RITESMITH_DISABLED_PROVIDERS`` (comma-separated namespaces) drops providers
without a rebuild.
"""

import logging
import os
from importlib.metadata import entry_points

from ritesmith.runtime.providers.base import HostFunctionDef, MCPToolDef, ToolProvider
from ritesmith.runtime.providers.calendar import CalendarProvider
from ritesmith.runtime.providers.casp import CASPProvider
from ritesmith.runtime.providers.context import ContextProvider
from ritesmith.runtime.providers.duckdb_ import DuckdbProvider
from ritesmith.runtime.providers.email import EmailProvider
from ritesmith.runtime.providers.grafana import GrafanaProvider
from ritesmith.runtime.providers.home_example import ExampleHomeProvider
from ritesmith.runtime.providers.llm import LlmProvider
from ritesmith.runtime.providers.market import MarketProvider
from ritesmith.runtime.providers.obsidian import ObsidianProvider
from ritesmith.runtime.providers.reports import ReportsProvider
from ritesmith.runtime.providers.stat import StatProvider
from ritesmith.runtime.providers.telegram import TelegramProvider
from ritesmith.runtime.providers.web import WebProvider

_BUILTIN: list[ToolProvider] = [
    StatProvider(),
    ContextProvider(),
    WebProvider(),
    MarketProvider(),
    TelegramProvider(),
    DuckdbProvider(),
    ObsidianProvider(),
    CalendarProvider(),
    EmailProvider(),
    ReportsProvider(),
    CASPProvider(),
    ExampleHomeProvider(),  # replaced by a deployment's own `home` plugin
    GrafanaProvider(),
    LlmProvider(),
]

ENTRY_POINT_GROUP = "ritesmith.providers"
log = logging.getLogger(__name__)
#: namespace → "module:attr" of the plugin that supplied it (for /providers and logs)
PLUGIN_SOURCES: dict[str, str] = {}


def _load_plugins() -> list[ToolProvider]:
    found = []
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        try:
            obj = ep.load()
            provider = obj() if isinstance(obj, type) else obj
            if not isinstance(provider, ToolProvider):
                raise TypeError(f"{ep.value} is not a ToolProvider")
        except Exception:
            log.exception("provider plugin %s (%s) failed to load; skipped", ep.name, ep.value)
            continue
        PLUGIN_SOURCES[provider.namespace] = ep.value
        found.append(provider)
    return found


def _build() -> list[ToolProvider]:
    providers = list(_BUILTIN)
    for plugin in _load_plugins():
        for i, p in enumerate(providers):
            if p.namespace == plugin.namespace:
                log.warning(
                    "provider %r: plugin %s replaces the built-in",
                    plugin.namespace,
                    PLUGIN_SOURCES[plugin.namespace],
                )
                providers[i] = plugin
                break
        else:
            log.info(
                "provider %r: loaded from plugin %s",
                plugin.namespace,
                PLUGIN_SOURCES[plugin.namespace],
            )
            providers.append(plugin)
    disabled = {
        n.strip()
        for n in os.environ.get("RITESMITH_DISABLED_PROVIDERS", "").split(",")
        if n.strip()
    }
    if disabled:
        log.warning("providers disabled by RITESMITH_DISABLED_PROVIDERS: %s", sorted(disabled))
    return [p for p in providers if p.namespace not in disabled]


def provider_for(namespace: str) -> ToolProvider | None:
    return next((p for p in PROVIDERS if p.namespace == namespace), None)


PROVIDERS: list[ToolProvider] = _build()

__all__ = [
    "ENTRY_POINT_GROUP",
    "PLUGIN_SOURCES",
    "PROVIDERS",
    "HostFunctionDef",
    "MCPToolDef",
    "ToolProvider",
    "provider_for",
]
