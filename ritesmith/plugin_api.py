"""Base types for tool providers — the API plugins build on.

Kept outside ``ritesmith.runtime.providers`` on purpose: importing that package
builds the provider registry and loads plugins, so a plugin that imported its base
class from there would be loaded while it is still half-imported. Plugins import
from here::

    from ritesmith.plugin_api import HostFunctionDef, MCPToolDef, ToolProvider
"""

from __future__ import annotations

import inspect
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass
class HostFunctionDef:
    """A single host function exposed to the Lua sandbox."""

    name: str  # fully-qualified: "web.search"
    profile: str  # "transform_only" | "readonly_network" | "notification" | ...
    callable: Callable
    description: str = ""
    input_schema: dict | None = None
    output_schema: dict | None = None


def invoke_capability(fn, args: dict):
    """Call a provider function with named arguments.

    report.* functions take one `args` dict (`def f(args)`) instead of keyword arguments.
    """
    params = list(inspect.signature(fn).parameters.values())
    if len(params) == 1 and params[0].name == "args":
        return fn(args)
    return fn(**args)


def normalize_device(raw: dict) -> dict:
    """A home.devices entry in the contract's shape (docs/home-capability-contract.md)."""
    return {
        "target": raw.get("target") or raw.get("id"),
        "name": raw.get("name"),
        "type": raw.get("type"),
        "room": raw.get("room"),
        "capabilities": raw.get("capabilities"),
        "online": raw.get("online"),
        "state": raw.get("state"),
    }


@dataclass
class MCPToolDef:
    """A single tool exposed via the MCP server."""

    name: str
    description: str
    input_schema: dict  # JSON Schema object
    fn: Callable  # async callable(**kwargs) -> Any


class ToolProvider(ABC):
    """Base class for all tool providers.

    Each provider contributes:
      - lua_functions()  → host functions injected into the Lua sandbox
      - mcp_tools()      → async tool definitions registered in the MCP server
    """

    namespace: str
    profile: str  # lowest-privilege profile that can use this provider
    risk_level: str = "low"
    side_effects: str = "none"

    @abstractmethod
    def is_available(self) -> bool:
        """Return True if all required config/dependencies are present."""
        ...

    @abstractmethod
    def lua_functions(self) -> dict[str, HostFunctionDef]:
        """Return fully-qualified-name → HostFunctionDef mapping for the Lua sandbox."""
        ...

    @abstractmethod
    def mcp_tools(self) -> list[MCPToolDef]:
        """Return MCP tool definitions for the standalone MCP server."""
        ...

    # ---- Optional hooks for a `home` provider (device control). Defaults do nothing. ----

    #: device types that are not physical (people, trackers…): "online" does not apply.
    virtual_device_types: frozenset[str] = frozenset()

    def prompt_hints(self) -> dict[str, str]:
        """Deployment-specific examples for the generation prompts (see ritesmith.llm.hints).

        Static data only — called without checking is_available()."""
        return {}

    def normalize_device(self, raw: dict) -> dict:
        """One entry of home.devices → the row the agent sees (target, name, type, room,
        capabilities, online, state). The default reads those fields as-is."""
        return normalize_device(raw)

    def reset_caches(self) -> None:
        """Clear module-level caches (tests)."""

    def manifest(self) -> dict[str, Any]:
        from ritesmith.runtime.providers import PLUGIN_SOURCES

        return {
            "namespace": self.namespace,
            "plugin": PLUGIN_SOURCES.get(self.namespace),
            "profile": self.profile,
            "risk_level": self.risk_level,
            "side_effects": self.side_effects,
            "available": self.is_available(),
            "lua_functions": list(self.lua_functions().keys()) if self.is_available() else [],
            "mcp_tools": [t.name for t in self.mcp_tools()] if self.is_available() else [],
        }

    def capabilities(self) -> list[dict[str, Any]]:
        return [
            {
                "name": name,
                "kind": "script_function",
                "description": f"{name} via {self.namespace} provider",
                "risk_level": self.risk_level,
                "side_effects": self.side_effects,
                "tags": [self.namespace],
            }
            for name in self.lua_functions()
        ]
