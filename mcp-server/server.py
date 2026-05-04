"""MCP server exposing domain tools and RiteSmith meta-tools to the Agent Core."""
from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

# Ensure the ritesmith package is importable when server.py is run directly
_repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

import _ritesmith as rs
from ritesmith.runtime.providers import PROVIDERS
from ritesmith.runtime.providers.base import MCPToolDef

server = Server("jarvis-ritesmith")

# Built lazily on first list_tools call so providers can finish initialising.
_tool_registry: dict[str, MCPToolDef] = {}
_tools_built = False


def _build_tool_registry() -> list[Tool]:
    global _tools_built
    _tool_registry.clear()

    tools: list[Tool] = []

    for provider in PROVIDERS:
        if not provider.is_available():
            continue
        for td in provider.mcp_tools():
            _tool_registry[td.name] = td
            tools.append(Tool(
                name=td.name,
                description=td.description,
                inputSchema=td.input_schema,
            ))

    # RiteSmith meta-tools
    _META_TOOLS: list[tuple[str, str, dict]] = [
        (
            "ritesmith_plan",
            "Ask RiteSmith to produce a full validated plan (capabilities + workflow graph) from a natural-language intent.",
            {
                "type": "object",
                "properties": {
                    "intent": {"type": "string", "description": "What you want to accomplish"},
                    "constraints": {"type": "object", "description": "Optional constraints dict"},
                },
                "required": ["intent"],
            },
        ),
        (
            "ritesmith_generate",
            "Generate a capability (Lua script or Trama workflow) from a natural-language intent. "
            "RiteSmith automatically decides the artifact type based on the intent.",
            {
                "type": "object",
                "properties": {
                    "intent": {"type": "string", "description": "What you want the capability to do"},
                    "save": {"type": "boolean", "description": "Persist the artifact in the registry"},
                    "context": {"type": "object", "description": "Extra generation context"},
                    "constraints": {"type": "object", "description": "Optional generation constraints"},
                },
                "required": ["intent"],
            },
        ),
        (
            "ritesmith_search_artifacts",
            "Full-text search over stored RiteSmith artifacts (scripts and workflows).",
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "artifact_type": {
                        "type": "string",
                        "enum": ["lua_script", "trama_workflow"],
                        "description": "Filter by type (optional)",
                    },
                    "limit": {"type": "integer", "default": 10},
                },
                "required": ["query"],
            },
        ),
        (
            "ritesmith_execute",
            "Execute a stored RiteSmith artifact by ID.",
            {
                "type": "object",
                "properties": {
                    "artifact_id": {"type": "string"},
                    "input": {"type": "object", "description": "Input data for the artifact"},
                    "idempotency_key": {"type": "string"},
                },
                "required": ["artifact_id", "input"],
            },
        ),
        (
            "ritesmith_get_execution",
            "Poll the status and result of a RiteSmith execution.",
            {
                "type": "object",
                "properties": {
                    "execution_id": {"type": "string"},
                },
                "required": ["execution_id"],
            },
        ),
        (
            "ritesmith_list_capabilities",
            "List capabilities registered in RiteSmith, optionally filtered by kind.",
            {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "description": "e.g. script_function, workflow"},
                    "limit": {"type": "integer", "default": 50},
                },
            },
        ),
    ]

    for name, description, schema in _META_TOOLS:
        tools.append(Tool(name=name, description=description, inputSchema=schema))

    _tools_built = True
    return tools


@server.list_tools()
async def list_tools() -> list[Tool]:
    return _build_tool_registry()


@server.call_tool()
async def call_tool(name: str, arguments: dict | None) -> list[TextContent]:
    args: dict[str, Any] = arguments or {}

    # Domain tools from providers
    if name in _tool_registry:
        td = _tool_registry[name]
        result = await td.fn(**args)
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, default=str))]

    # RiteSmith meta-tools
    match name:
        case "ritesmith_plan":
            result = await rs.plan(args["intent"], args.get("constraints"))
        case "ritesmith_generate":
            intent = args.pop("intent")
            result = await rs.generate(intent, **args)
        case "ritesmith_search_artifacts":
            result = await rs.search_artifacts(
                args["query"],
                artifact_type=args.get("artifact_type"),
                limit=args.get("limit", 10),
            )
        case "ritesmith_execute":
            result = await rs.execute(
                args["artifact_id"],
                args["input"],
                idempotency_key=args.get("idempotency_key"),
            )
        case "ritesmith_get_execution":
            result = await rs.get_execution(args["execution_id"])
        case "ritesmith_list_capabilities":
            result = await rs.list_capabilities(
                kind=args.get("kind"),
                limit=args.get("limit", 50),
            )
        case _:
            raise ValueError(f"Unknown tool: {name}")

    return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, default=str))]


async def main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    asyncio.run(main())
