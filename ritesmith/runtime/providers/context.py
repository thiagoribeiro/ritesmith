"""runtime.* host functions — execution context helpers.

Named `runtime` (not `context`) to avoid shadowing the `context`
parameter of `run(input, context)` inside generated Lua scripts.
"""

from __future__ import annotations

import time as _time
from datetime import UTC, datetime

from ritesmith.runtime.providers.base import HostFunctionDef, MCPToolDef, ToolProvider


class ContextProvider(ToolProvider):
    namespace = "runtime"
    profile = "transform_only"
    risk_level = "low"
    side_effects = "none"

    def is_available(self) -> bool:
        return True

    def lua_functions(self) -> dict[str, HostFunctionDef]:
        def _now() -> str:
            return datetime.now(UTC).isoformat()

        def _timezone() -> str:
            import time

            return time.tzname[0]

        def _timestamp() -> float:
            return _time.time()

        return {
            "runtime.now": HostFunctionDef(
                "runtime.now",
                "transform_only",
                _now,
                description="Return the current UTC time as an ISO-8601 string.",
                input_schema={"type": "object", "properties": {}},
                output_schema={"type": "object", "properties": {"now": {"type": "string"}}},
            ),
            "runtime.timezone": HostFunctionDef(
                "runtime.timezone",
                "transform_only",
                _timezone,
                description="Return the local timezone name (e.g. 'America/Sao_Paulo').",
                input_schema={"type": "object", "properties": {}},
                output_schema={"type": "object", "properties": {"timezone": {"type": "string"}}},
            ),
            "runtime.timestamp": HostFunctionDef(
                "runtime.timestamp",
                "transform_only",
                _timestamp,
                description="Return the current Unix epoch as a float.",
                input_schema={"type": "object", "properties": {}},
                output_schema={"type": "object", "properties": {"result": {"type": "number"}}},
            ),
        }

    def mcp_tools(self) -> list[MCPToolDef]:
        async def _now(**_) -> dict:
            return {"now": datetime.now(UTC).isoformat()}

        return [
            MCPToolDef(
                name="runtime_now",
                description="Return the current UTC timestamp as ISO 8601.",
                input_schema={"type": "object", "properties": {}},
                fn=_now,
            ),
        ]
