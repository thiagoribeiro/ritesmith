"""home.* host functions — LoomHarbor smart device control."""
from __future__ import annotations

import asyncio
import logging
import time
import uuid

import httpx

from ritesmith.config import get_settings
from ritesmith.runtime.providers.base import HostFunctionDef, MCPToolDef, ToolProvider

logger = logging.getLogger(__name__)

_TIMEOUT = 8.0
_READY_TIMEOUT = 2.0
_READY_CACHE_TTL = 30.0
_MAX_RETRIES = 3

_ready_cache: dict[str, float] = {}  # url → expiry monotonic timestamp


def _base_url() -> str:
    return get_settings().loomharbor_url or "http://loomharbor:8000"


def _headers(request_id: str | None = None) -> dict[str, str]:
    headers: dict[str, str] = {}
    secret = get_settings().loomharbor_secret
    if secret:
        headers["Authorization"] = f"Bearer {secret}"
    headers["X-Request-ID"] = request_id or str(uuid.uuid4())
    return headers


def _client(request_id: str | None = None) -> httpx.Client:
    return httpx.Client(base_url=_base_url(), timeout=_TIMEOUT, headers=_headers(request_id))


def _check_ready() -> bool:
    """Check /ready with short timeout; result cached for 30s."""
    url = _base_url()
    now = time.monotonic()
    expiry = _ready_cache.get(url, 0.0)
    if now < expiry:
        return True
    try:
        with httpx.Client(base_url=url, timeout=_READY_TIMEOUT) as c:
            r = c.get("/ready")
            ok = r.status_code == 200
    except Exception:
        ok = False
    if ok:
        _ready_cache[url] = now + _READY_CACHE_TTL
    return ok


def _execute(capability: str, target: str, input: dict | None = None) -> dict:
    request_id = str(uuid.uuid4())
    last_exc: Exception | None = None
    delay = 0.5
    for attempt in range(_MAX_RETRIES):
        try:
            with _client(request_id) as c:
                r = c.post("/capabilities/execute", json={
                    "capability": capability,
                    "target": target,
                    "input": input or {},
                })
                r.raise_for_status()
                return r.json()
        except (httpx.ConnectError, httpx.TimeoutException, httpx.RemoteProtocolError) as exc:
            last_exc = exc
            if attempt < _MAX_RETRIES - 1:
                time.sleep(delay)
                delay = min(delay * 2, 4.0)
        except Exception as exc:
            logger.warning("loomharbor execute failed: %s", exc)
            return {"success": False, "error": str(exc)}
    logger.warning("loomharbor execute failed after %d retries: %s", _MAX_RETRIES, last_exc)
    return {"success": False, "error": str(last_exc)}


def _list_devices() -> list[dict]:
    try:
        with _client() as c:
            r = c.get("/devices")
            r.raise_for_status()
            return r.json().get("devices", [])
    except Exception as exc:
        logger.warning("loomharbor devices failed: %s", exc)
        return []


def _get_state(target: str) -> dict:
    try:
        with _client() as c:
            r = c.get(f"/devices/{target}/state")
            r.raise_for_status()
            return r.json()
    except Exception as exc:
        logger.warning("loomharbor state failed: %s", exc)
        return {"error": str(exc)}


def _list_capabilities() -> list[dict]:
    try:
        with _client() as c:
            r = c.get("/capabilities")
            r.raise_for_status()
            return r.json()
    except Exception as exc:
        logger.warning("loomharbor capabilities failed: %s", exc)
        return []


def _alarm(target: str, mode: str) -> dict:
    """Shorthand for alarm.arm / alarm.disarm / alarm.home / alarm.sos."""
    valid_modes = {"arm", "disarm", "home", "sos"}
    if mode not in valid_modes:
        return {"success": False, "error": f"Invalid alarm mode '{mode}'. Use: {valid_modes}"}
    return _execute(f"alarm.{mode}", target)


def _sensor(target: str) -> dict:
    """Read presence or battery sensor state."""
    state = _get_state(target)
    result: dict = {}
    if "present" in state:
        result["present"] = state["present"]
    if "last_seen" in state:
        result["last_seen"] = state["last_seen"]
    if "battery" in state:
        result["battery"] = state["battery"]
    return result or state


def _gps_location(device: str) -> dict:
    """Return cached GPS location for a FindTag tracker device."""
    return _execute("gps.location", device)


def _findtag_zone_add(name: str, query: str = "") -> dict:
    """Add a geofence zone by place name. query is sent to Nominatim for polygon resolution."""
    return _execute("gps.zone_add", "findtag", {"name": name, "query": query or None})


def _findtag_zone_delete(zone_id: str) -> dict:
    """Soft-delete a geofence zone by its ID."""
    return _execute("gps.zone_delete", "findtag", {"zone_id": zone_id})


def _findtag_zone_list() -> dict:
    """List all geofence zones (active and soft-deleted)."""
    return _execute("gps.zone_list", "findtag")


# ---------------------------------------------------------------------------
# Async wrappers for MCP tools
# ---------------------------------------------------------------------------

async def _async_execute(**kwargs) -> dict:
    return await asyncio.to_thread(
        _execute,
        kwargs["capability"],
        kwargs["target"],
        kwargs.get("input"),
    )


async def _async_devices(**kwargs) -> list:
    return await asyncio.to_thread(_list_devices)


async def _async_state(**kwargs) -> dict:
    return await asyncio.to_thread(_get_state, kwargs["target"])


async def _async_capabilities(**kwargs) -> list:
    return await asyncio.to_thread(_list_capabilities)


async def _async_gps_location(**kwargs) -> dict:
    return await asyncio.to_thread(_gps_location, kwargs["device"])


async def _async_findtag_zone_add(**kwargs) -> dict:
    return await asyncio.to_thread(_findtag_zone_add, kwargs["name"], kwargs.get("query", ""))


async def _async_findtag_zone_delete(**kwargs) -> dict:
    return await asyncio.to_thread(_findtag_zone_delete, kwargs["zone_id"])


async def _async_findtag_zone_list(**kwargs) -> dict:
    return await asyncio.to_thread(_findtag_zone_list)


class LoomHarborProvider(ToolProvider):
    namespace = "home"
    profile = "side_effects"
    risk_level = "medium"
    side_effects = "physical_world"

    def is_available(self) -> bool:
        if get_settings().loomharbor_url is None:
            return False
        return _check_ready()

    def lua_functions(self) -> dict[str, HostFunctionDef]:
        return {
            "home.execute": HostFunctionDef(
                name="home.execute",
                profile="side_effects",
                callable=_execute,
                description="Execute any home automation capability on a physical device",
                input_schema={
                    "type": "object",
                    "required": ["capability", "target"],
                    "properties": {
                        "capability": {"type": "string"},
                        "target": {"type": "string"},
                        "input": {"type": "object"},
                    },
                },
            ),
            "home.switch_on": HostFunctionDef(
                name="home.switch_on",
                profile="side_effects",
                callable=lambda target: _execute("switch.on", target),
                description="Turn a switch or socket on",
            ),
            "home.switch_off": HostFunctionDef(
                name="home.switch_off",
                profile="side_effects",
                callable=lambda target: _execute("switch.off", target),
                description="Turn a switch or socket off",
            ),
            "home.devices": HostFunctionDef(
                name="home.devices",
                profile="readonly_network",
                callable=_list_devices,
                description="List all registered home devices",
            ),
            "home.state": HostFunctionDef(
                name="home.state",
                profile="readonly_network",
                callable=_get_state,
                description="Read the current state of a device",
            ),
            "home.capabilities": HostFunctionDef(
                name="home.capabilities",
                profile="readonly_network",
                callable=_list_capabilities,
                description="List all available home automation capabilities",
            ),
            "home.alarm": HostFunctionDef(
                name="home.alarm",
                profile="side_effects",
                callable=_alarm,
                description="Control an alarm: arm, disarm, home, or sos",
                input_schema={
                    "type": "object",
                    "required": ["target", "mode"],
                    "properties": {
                        "target": {"type": "string"},
                        "mode": {"type": "string", "enum": ["arm", "disarm", "home", "sos"]},
                    },
                },
            ),
            "home.sensor": HostFunctionDef(
                name="home.sensor",
                profile="readonly_network",
                callable=_sensor,
                description="Read presence or battery sensor state",
            ),
            "home.gps_location": HostFunctionDef(
                name="home.gps_location",
                profile="readonly_network",
                callable=_gps_location,
                description=(
                    "Return the cached GPS location for a FindTag tracker. "
                    "Returns: lat, lon, address, zones_active, moving, last_seen_ago_s, battery_level."
                ),
                input_schema={
                    "type": "object",
                    "required": ["device"],
                    "properties": {
                        "device": {"type": "string", "description": "Tracker alias, e.g. 'mochila'"},
                    },
                },
            ),
            "home.findtag_zone_add": HostFunctionDef(
                name="home.findtag_zone_add",
                profile="side_effects",
                callable=_findtag_zone_add,
                description=(
                    "Add a geofence zone. Provide a human-readable name and a query string "
                    "for Nominatim to resolve into a polygon (e.g. 'Shopping Iguatemi Campinas SP'). "
                    "Returns the created zone with its generated ID."
                ),
                input_schema={
                    "type": "object",
                    "required": ["name"],
                    "properties": {
                        "name": {"type": "string", "description": "Zone display name"},
                        "query": {"type": "string", "description": "Place name for Nominatim polygon search"},
                    },
                },
            ),
            "home.findtag_zone_delete": HostFunctionDef(
                name="home.findtag_zone_delete",
                profile="side_effects",
                callable=_findtag_zone_delete,
                description="Soft-delete a geofence zone by its ID (reversible).",
                input_schema={
                    "type": "object",
                    "required": ["zone_id"],
                    "properties": {
                        "zone_id": {"type": "string", "description": "ID returned by findtag_zone_list or findtag_zone_add"},
                    },
                },
            ),
            "home.findtag_zone_list": HostFunctionDef(
                name="home.findtag_zone_list",
                profile="readonly_network",
                callable=_findtag_zone_list,
                description="List all geofence zones including soft-deleted ones.",
            ),
        }

    def mcp_tools(self) -> list[MCPToolDef]:
        return [
            MCPToolDef(
                name="home_execute",
                description=(
                    "Execute a home automation capability on a physical device. "
                    "Use to control lights, switches, alarms, fans, AC, and IR devices. "
                    "Common capabilities: switch.on, switch.off, switch.toggle, "
                    "ac.setTemperature, ac.powerOn, ac.powerOff, alarm.arm, alarm.disarm, "
                    "camera.snapshot, presence.read."
                ),
                input_schema={
                    "type": "object",
                    "required": ["capability", "target"],
                    "properties": {
                        "capability": {
                            "type": "string",
                            "description": "e.g. 'switch.on', 'ac.setTemperature', 'alarm.arm'",
                        },
                        "target": {
                            "type": "string",
                            "description": "Device alias, channel alias, or device ID",
                        },
                        "input": {
                            "type": "object",
                            "description": "Capability-specific parameters (e.g. {temperature: 22} for ac.setTemperature)",
                        },
                    },
                },
                fn=_async_execute,
            ),
            MCPToolDef(
                name="home_devices",
                description="List all registered home automation devices with their status and capabilities.",
                input_schema={"type": "object", "properties": {}},
                fn=_async_devices,
            ),
            MCPToolDef(
                name="home_state",
                description="Read the current state (switch status, temperature, etc.) of a specific device.",
                input_schema={
                    "type": "object",
                    "required": ["target"],
                    "properties": {
                        "target": {"type": "string", "description": "Device alias or ID"},
                    },
                },
                fn=_async_state,
            ),
            MCPToolDef(
                name="home_capabilities",
                description="List all available home automation capabilities by adapter.",
                input_schema={"type": "object", "properties": {}},
                fn=_async_capabilities,
            ),
            MCPToolDef(
                name="home_gps_location",
                description=(
                    "Get the current GPS location of a FindTag tracker. "
                    "Returns lat, lon, address, active geofence zones, movement status, "
                    "and seconds since last update."
                ),
                input_schema={
                    "type": "object",
                    "required": ["device"],
                    "properties": {
                        "device": {"type": "string", "description": "Tracker alias, e.g. 'mochila'"},
                    },
                },
                fn=_async_gps_location,
            ),
            MCPToolDef(
                name="home_findtag_zone_add",
                description=(
                    "Add a geofence zone for GPS tracking. "
                    "Nominatim resolves the query into a real polygon (neighborhood, building, etc.). "
                    "Examples: query='Parque Taquaral Campinas', query='Shopping Iguatemi Campinas SP'."
                ),
                input_schema={
                    "type": "object",
                    "required": ["name"],
                    "properties": {
                        "name": {"type": "string", "description": "Display name for the zone"},
                        "query": {"type": "string", "description": "Place name to resolve via Nominatim"},
                    },
                },
                fn=_async_findtag_zone_add,
            ),
            MCPToolDef(
                name="home_findtag_zone_delete",
                description="Soft-delete a geofence zone by ID. Use home_findtag_zone_list to find the ID first.",
                input_schema={
                    "type": "object",
                    "required": ["zone_id"],
                    "properties": {
                        "zone_id": {"type": "string"},
                    },
                },
                fn=_async_findtag_zone_delete,
            ),
            MCPToolDef(
                name="home_findtag_zone_list",
                description="List all geofence zones, including soft-deleted ones (deleted_at is set when inactive).",
                input_schema={"type": "object", "properties": {}},
                fn=_async_findtag_zone_list,
            ),
        ]
