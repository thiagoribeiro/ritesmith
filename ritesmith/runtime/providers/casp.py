"""casp.* host functions — CASP protocol provider for semantic device control.

These host functions are registered under the 'trusted_internal' profile.
They call CASP provider endpoints (e.g. LoomHarbor) directly via Python
httpx — NOT through the Lua http.* functions. This means _BLOCKED_PATTERNS
does not apply to these calls; they are trusted structured internal calls
to configured provider URLs.

Lua usage:
  local resources = casp.query("smart-switch", {room="cozinha"}, {"turn-off"})
  local resolved  = casp.resolve("smart-switch", "turn-off", "led do painel")
  local result    = casp.execute(resolved.resource.id, "smart-switch", "turn-off", {})

  -- Always check status before using resolve result:
  if resolved.status == "resolved" then
    casp.execute(resolved.resource.id, "smart-switch", "turn-on", {})
  end
"""

from __future__ import annotations

import logging

from ritesmith.config import get_settings
from ritesmith.runtime.providers.base import HostFunctionDef, MCPToolDef, ToolProvider

log = logging.getLogger(__name__)


def _casp_query(
    resource_type: str, filters: dict, requires_capabilities: list | None = None
) -> dict:
    from ritesmith.runtime.casp.client import query

    caps = requires_capabilities or []
    result = query(resource_type, filters, caps)
    if "error" in result:
        log.warning("casp.query error: %s", result.get("message", result["error"]))
    return result


def _casp_resolve(resource_type: str, capability: str, hint: str) -> dict:
    from ritesmith.runtime.casp.client import resolve

    result = resolve(resource_type, capability, hint)
    if result.get("status") not in ("resolved", "ambiguous"):
        log.warning("casp.resolve unexpected status: %s", result)
    return result


def _casp_execute(
    resource_id: str, resource_type: str, capability: str, input_data: dict | None = None
) -> dict:
    from ritesmith.runtime.casp.client import execute

    result = execute(resource_id, resource_type, capability, input_data or {})
    if result.get("status") != "succeeded":
        log.warning(
            "casp.execute failed: resource=%s cap=%s error=%s",
            resource_id,
            capability,
            result.get("error"),
        )
    return result


def _casp_types() -> dict:
    from ritesmith.runtime.casp.client import all_types_from_providers

    types = all_types_from_providers()
    return {"resourceTypes": types}


class CASPProvider(ToolProvider):
    namespace = "casp"
    profile = "trusted_internal"
    risk_level = "medium"
    side_effects = "physical_world"

    def is_available(self) -> bool:
        return bool(get_settings().casp_providers)

    def lua_functions(self) -> dict[str, HostFunctionDef]:
        return {
            "casp.query": HostFunctionDef(
                name="casp.query",
                profile="trusted_internal",
                callable=_casp_query,
                description=(
                    "Find resources by type and filters. "
                    "USE when the user refers to a room or area (e.g. 'sala', 'cozinha'). "
                    "Returns {resources: [{id, type, displayName, metadata}]}. "
                    "Example: casp.query('smart-switch', {room='cozinha'}, {'turn-off'})"
                ),
                input_schema={
                    "type": "object",
                    "required": ["resource_type"],
                    "properties": {
                        "resource_type": {
                            "type": "string",
                            "description": "e.g. 'smart-switch', 'ac-unit'",
                        },
                        "filters": {"type": "object", "description": "e.g. {room='cozinha'}"},
                        "requires_capabilities": {"type": "array", "items": {"type": "string"}},
                    },
                },
            ),
            "casp.resolve": HostFunctionDef(
                name="casp.resolve",
                profile="trusted_internal",
                callable=_casp_resolve,
                description=(
                    "Resolve a natural-language hint to a resource ID. "
                    "USE when the user names a specific device (e.g. 'led do painel', 'ventilador da suíte'). "
                    "Returns {status, resource: {id, displayName}, confidence} or {status: 'ambiguous', candidates}. "
                    "ALWAYS check result.status == 'resolved' before using result.resource.id."
                ),
                input_schema={
                    "type": "object",
                    "required": ["resource_type", "capability", "hint"],
                    "properties": {
                        "resource_type": {"type": "string"},
                        "capability": {"type": "string"},
                        "hint": {
                            "type": "string",
                            "description": "Natural language name from user",
                        },
                    },
                },
            ),
            "casp.execute": HostFunctionDef(
                name="casp.execute",
                profile="trusted_internal",
                callable=_casp_execute,
                description=(
                    "Execute a capability on a resource. "
                    "Returns {status: 'succeeded'|'failed', output, error, durationMs}. "
                    "Example: casp.execute('luz_cozinha', 'smart-switch', 'turn-off', {})"
                ),
                input_schema={
                    "type": "object",
                    "required": ["resource_id", "resource_type", "capability"],
                    "properties": {
                        "resource_id": {
                            "type": "string",
                            "description": "From casp.query or casp.resolve",
                        },
                        "resource_type": {"type": "string"},
                        "capability": {"type": "string"},
                        "input_data": {"type": "object"},
                    },
                },
            ),
            "casp.types": HostFunctionDef(
                name="casp.types",
                profile="trusted_internal",
                callable=_casp_types,
                description="List all available CASP resource types and their capabilities.",
            ),
        }

    def mcp_tools(self) -> list[MCPToolDef]:
        return []  # CASP tools are Lua-only; MCP callers use home_* tools
