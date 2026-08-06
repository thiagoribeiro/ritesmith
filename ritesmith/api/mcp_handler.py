"""MCP over HTTP/SSE — same tool surface as mcp-server/server.py, embedded in FastAPI.

Exposes:
  GET  /mcp/sse        — SSE stream (client connects here to receive events)
  POST /mcp/messages/  — JSON-RPC messages from client

agent_core connects as a client to GET /mcp/sse.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx
from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.types import TextContent, Tool
from starlette.responses import Response
from starlette.routing import get_route_path

from ritesmith.runtime.providers import PROVIDERS
from ritesmith.runtime.providers.base import MCPToolDef

log = logging.getLogger(__name__)

_BASE = os.environ.get("RITESMITH_BASE_URL", "http://localhost:8081")
_TIMEOUT = 60.0

_mcp_server: Server = Server("ritesmith")
sse_transport = SseServerTransport("/messages/")

_tool_registry: dict[str, MCPToolDef] = {}


# ── HTTP client for RiteSmith self-calls (meta-tools) ────────────────────────


async def _post(path: str, body: dict) -> dict:
    async with httpx.AsyncClient(base_url=_BASE, timeout=_TIMEOUT) as c:
        r = await c.post(path, json=body)
        r.raise_for_status()
        return r.json()


async def _get(path: str, params: dict | None = None) -> Any:
    async with httpx.AsyncClient(base_url=_BASE, timeout=_TIMEOUT) as c:
        r = await c.get(path, params=params or {})
        r.raise_for_status()
        return r.json()


# ── Tool registry build ───────────────────────────────────────────────────────


def _build_tools() -> list[Tool]:
    _tool_registry.clear()
    tools: list[Tool] = []

    for provider in PROVIDERS:
        if not provider.is_available():
            continue
        for td in provider.mcp_tools():
            _tool_registry[td.name] = td
            tools.append(
                Tool(name=td.name, description=td.description, inputSchema=td.input_schema)
            )

    _META: list[tuple[str, str, dict]] = [
        (
            "ritesmith_plan",
            "Ask RiteSmith to produce a validated plan and execute it from a natural-language intent.",
            {
                "type": "object",
                "properties": {
                    "intent": {"type": "string"},
                    "mode": {
                        "type": "string",
                        "enum": ["propose", "generate", "validate_only", "persist", "execute"],
                        "default": "execute",
                    },
                    "callback_url": {"type": "string"},
                },
                "required": ["intent"],
            },
        ),
        (
            "ritesmith_generate",
            "Generate a Lua script or Trama workflow artifact from a natural-language intent.",
            {
                "type": "object",
                "properties": {
                    "intent": {"type": "string"},
                    "save": {"type": "boolean"},
                    "context": {"type": "object"},
                    "constraints": {"type": "object"},
                },
                "required": ["intent"],
            },
        ),
        (
            "ritesmith_search_artifacts",
            "Full-text search over stored RiteSmith artifacts.",
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "artifact_type": {"type": "string", "enum": ["lua_script", "trama_workflow"]},
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
                    "input": {"type": "object"},
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
                "properties": {"execution_id": {"type": "string"}},
                "required": ["execution_id"],
            },
        ),
        (
            "ritesmith_list_capabilities",
            "List capabilities registered in RiteSmith.",
            {
                "type": "object",
                "properties": {
                    "kind": {"type": "string"},
                    "limit": {"type": "integer", "default": 50},
                },
            },
        ),
    ]
    for name, description, schema in _META:
        tools.append(Tool(name=name, description=description, inputSchema=schema))

    return tools


# ── MCP server handlers ───────────────────────────────────────────────────────


@_mcp_server.list_tools()
async def _list_tools() -> list[Tool]:
    return _build_tools()


@_mcp_server.call_tool()
async def _call_tool(name: str, arguments: dict | None) -> list[TextContent]:
    args: dict[str, Any] = arguments or {}

    if name in _tool_registry:
        result = await _tool_registry[name].fn(**args)
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, default=str))]

    intent = args.get("intent", "")
    match name:
        case "ritesmith_plan":
            # "plan" was an old alias; map to "propose" for the REST API
            mode = args.get("mode", "execute")
            if mode == "plan":
                mode = "propose"
            body: dict = {"intent": intent, "mode": mode, "replan_budget": 1}
            if cb := args.get("callback_url"):
                body["callback_url"] = cb
            result = await _post("/plans", body)
        case "ritesmith_generate":
            result = await _post(
                "/generate", {"intent": intent, **{k: v for k, v in args.items() if k != "intent"}}
            )
        case "ritesmith_search_artifacts":
            params: dict = {"query": args["query"], "limit": args.get("limit", 10)}
            if at := args.get("artifact_type"):
                params["artifact_type"] = at
            result = await _get("/artifacts", params)
        case "ritesmith_execute":
            body = {"artifact_id": args["artifact_id"], "input": args["input"]}
            if ik := args.get("idempotency_key"):
                body["idempotency_key"] = ik
            result = await _post("/executions", body)
        case "ritesmith_get_execution":
            result = await _get(f"/executions/{args['execution_id']}")
        case "ritesmith_list_capabilities":
            params = {"limit": args.get("limit", 50)}
            if k := args.get("kind"):
                params["kind"] = k
            result = await _get("/capabilities", params)
        case _:
            raise ValueError(f"Unknown MCP tool: {name}")

    return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, default=str))]


# ── ASGI sub-app (mounted outside FastAPI middleware stack) ───────────────────
# Raw ASGI class avoids Starlette Route's assumption that handlers return Response objects.
# SSE transport sends the HTTP response itself (headers + body stream); wrapping it in a
# Starlette Route causes a TypeError when the handler returns None after the stream closes.


class _MCPApp:
    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            return
        # Starlette 1.x passes the full path in scope["path"]; use get_route_path()
        # to get the remaining path relative to the mount point.
        path: str = get_route_path(scope)
        log.info("MCP dispatch: path=%r", path)
        if path == "/sse":
            async with sse_transport.connect_sse(scope, receive, send) as streams:
                await _mcp_server.run(
                    streams[0], streams[1], _mcp_server.create_initialization_options()
                )
        elif path.startswith("/messages/"):
            await sse_transport.handle_post_message(scope, receive, send)
        else:
            log.warning("MCP: unmatched path %r (full path: %r)", path, scope.get("path"))
            await Response(status_code=404)(scope, receive, send)


mcp_app = _MCPApp()
