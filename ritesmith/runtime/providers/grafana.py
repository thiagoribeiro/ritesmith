"""grafana.* host functions — dashboard listing, panel metadata, and PromQL queries."""

from __future__ import annotations

import asyncio
import logging

import httpx

from ritesmith.config import get_settings
from ritesmith.runtime.providers.base import HostFunctionDef, MCPToolDef, ToolProvider

logger = logging.getLogger(__name__)

_TIMEOUT = 10.0


def _headers() -> dict[str, str]:
    token = get_settings().grafana_token
    if token:
        return {"Authorization": f"Bearer {token}"}
    return {}


def _grafana_list_dashboards(query: str = "") -> list[dict]:
    url = f"{get_settings().grafana_url}/api/search"
    try:
        r = httpx.get(
            url,
            params={"query": query, "type": "dash-db", "limit": 50},
            headers=_headers(),
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        return [
            {
                "uid": d.get("uid", ""),
                "title": d.get("title", ""),
                "folderTitle": d.get("folderTitle", ""),
                "url": d.get("url", ""),
            }
            for d in r.json()
        ]
    except Exception as e:
        logger.warning("grafana.list_dashboards failed: %s", e)
        return [{"error": str(e)}]


def _grafana_get_dashboard(uid: str) -> dict:
    url = f"{get_settings().grafana_url}/api/dashboards/uid/{uid}"
    try:
        r = httpx.get(url, headers=_headers(), timeout=_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        db = data.get("dashboard", {})
        panels = [
            {
                "id": p.get("id"),
                "title": p.get("title", ""),
                "type": p.get("type", ""),
                "description": p.get("description", ""),
            }
            for p in db.get("panels", [])
            if p.get("type") != "row"
        ]
        return {
            "uid": uid,
            "title": db.get("title", ""),
            "tags": db.get("tags", []),
            "panels": panels,
        }
    except Exception as e:
        logger.warning("grafana.get_dashboard failed uid=%s: %s", uid, e)
        return {"error": str(e)}


def _grafana_query_prometheus(expr: str) -> list[dict]:
    url = f"{get_settings().prometheus_url}/api/v1/query"
    try:
        r = httpx.get(url, params={"query": expr}, timeout=_TIMEOUT)
        r.raise_for_status()
        results = r.json().get("data", {}).get("result", [])
        return [{"labels": row["metric"], "value": row["value"][1]} for row in results[:20]]
    except Exception as e:
        logger.warning("grafana.query_prometheus failed: %s", e)
        return [{"error": str(e)}]


class GrafanaProvider(ToolProvider):
    namespace = "grafana"
    profile = "readonly_network"
    risk_level = "low"
    side_effects = "external_read"

    def is_available(self) -> bool:
        s = get_settings()
        return bool(s.grafana_url or s.prometheus_url)

    def lua_functions(self) -> dict[str, HostFunctionDef]:
        return {
            "grafana.list_dashboards": HostFunctionDef(
                "grafana.list_dashboards",
                "readonly_network",
                _grafana_list_dashboards,
                description="List Grafana dashboards. Optional query to filter by title.",
                input_schema={
                    "type": "object",
                    "properties": {"query": {"type": "string", "default": ""}},
                },
                output_schema={
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "uid": {"type": "string"},
                            "title": {"type": "string"},
                            "folderTitle": {"type": "string"},
                            "url": {"type": "string"},
                        },
                    },
                },
            ),
            "grafana.get_dashboard": HostFunctionDef(
                "grafana.get_dashboard",
                "readonly_network",
                _grafana_get_dashboard,
                description="Get dashboard metadata and panel list by UID.",
                input_schema={
                    "type": "object",
                    "required": ["uid"],
                    "properties": {"uid": {"type": "string"}},
                },
                output_schema={
                    "type": "object",
                    "properties": {
                        "uid": {"type": "string"},
                        "title": {"type": "string"},
                        "tags": {"type": "array"},
                        "panels": {"type": "array"},
                    },
                },
            ),
            "grafana.query_prometheus": HostFunctionDef(
                "grafana.query_prometheus",
                "readonly_network",
                _grafana_query_prometheus,
                description="Execute a PromQL instant query. Returns up to 20 series with their labels and current value.",
                input_schema={
                    "type": "object",
                    "required": ["expr"],
                    "properties": {"expr": {"type": "string"}},
                },
                output_schema={
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "labels": {"type": "object"},
                            "value": {"type": "string"},
                        },
                    },
                },
            ),
        }

    def mcp_tools(self) -> list[MCPToolDef]:
        async def _list(query: str = "", **_) -> list[dict]:
            return await asyncio.to_thread(_grafana_list_dashboards, query)

        async def _get(uid: str, **_) -> dict:
            return await asyncio.to_thread(_grafana_get_dashboard, uid)

        async def _query(expr: str, **_) -> list[dict]:
            return await asyncio.to_thread(_grafana_query_prometheus, expr)

        return [
            MCPToolDef(
                name="grafana_list_dashboards",
                description="List Grafana dashboards. Optionally filter by title query.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Optional title filter.",
                            "default": "",
                        },
                    },
                },
                fn=_list,
            ),
            MCPToolDef(
                name="grafana_get_dashboard",
                description="Get Grafana dashboard metadata and panel list by dashboard UID.",
                input_schema={
                    "type": "object",
                    "required": ["uid"],
                    "properties": {
                        "uid": {
                            "type": "string",
                            "description": "Dashboard UID (from grafana_list_dashboards).",
                        },
                    },
                },
                fn=_get,
            ),
            MCPToolDef(
                name="grafana_query_prometheus",
                description=(
                    "Execute a PromQL instant query on Prometheus. Returns up to 20 series with labels and current value. "
                    "Example: 'up{job=\"node\"}', 'rate(http_requests_total[5m])'."
                ),
                input_schema={
                    "type": "object",
                    "required": ["expr"],
                    "properties": {
                        "expr": {"type": "string", "description": "PromQL expression."},
                    },
                },
                fn=_query,
            ),
        ]
