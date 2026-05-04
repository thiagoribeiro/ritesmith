"""Base types shared by all tool providers."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class HostFunctionDef:
    """A single host function exposed to the Lua sandbox."""
    name: str      # fully-qualified: "web.search"
    profile: str   # "transform_only" | "readonly_network" | "notification" | ...
    callable: Callable
    description: str = ""
    input_schema: dict | None = None
    output_schema: dict | None = None


@dataclass
class MCPToolDef:
    """A single tool exposed via the MCP server."""
    name: str
    description: str
    input_schema: dict  # JSON Schema object
    fn: Callable        # async callable(**kwargs) -> Any


class ToolProvider(ABC):
    """Base class for all tool providers.

    Each provider contributes:
      - lua_functions()  → host functions injected into the Lua sandbox
      - mcp_tools()      → async tool definitions registered in the MCP server
    """

    namespace: str
    profile: str        # lowest-privilege profile that can use this provider
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

    def manifest(self) -> dict[str, Any]:
        return {
            "namespace": self.namespace,
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
