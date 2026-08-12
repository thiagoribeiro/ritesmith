"""calendar.* host functions — Google Calendar via OAuth2.

Auth: stored refresh token in RITESMITH_GOOGLE_TOKEN_JSON.
Run `python -m ritesmith.tools.google_auth` once to obtain the token.

Calendar targets are configured as RITESMITH_GOOGLE_CALENDAR_IDS:
  personal:primary,family:calendarid@group.calendar.google.com
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from ritesmith.config import get_settings
from ritesmith.runtime.providers.base import HostFunctionDef, MCPToolDef, ToolProvider

logger = logging.getLogger(__name__)

_SCOPES = ["https://www.googleapis.com/auth/calendar"]


def _parse_calendar_ids() -> dict[str, str]:
    raw = get_settings().google_calendar_ids or ""
    result: dict[str, str] = {}
    for part in raw.split(","):
        part = part.strip()
        if ":" in part:
            name, cal_id = part.split(":", 1)
            result[name.strip()] = cal_id.strip()
    if not result:
        result["personal"] = "primary"
    return result


def _get_credentials():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    token_path = get_settings().google_token_json
    if not token_path:
        raise RuntimeError("RITESMITH_GOOGLE_TOKEN_JSON not configured")

    creds = Credentials.from_authorized_user_file(token_path, _SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        # persist refreshed token
        with open(token_path, "w") as f:
            f.write(creds.to_json())
    return creds


def _service():
    from googleapiclient.discovery import build

    return build("calendar", "v3", credentials=_get_credentials(), cache_discovery=False)


def _resolve_target(target: str) -> list[str]:
    ids = _parse_calendar_ids()
    if target == "all":
        return list(ids.values())
    return [ids.get(target, target)]


def _format_event(e: dict) -> dict:
    start = e.get("start", {})
    end = e.get("end", {})
    return {
        "id": e.get("id"),
        "summary": e.get("summary"),
        "description": e.get("description"),
        "start": start.get("dateTime") or start.get("date"),
        "end": end.get("dateTime") or end.get("date"),
        "location": e.get("location"),
        "attendees": [a.get("email") for a in e.get("attendees", [])],
        "html_link": e.get("htmlLink"),
    }


def _get_range(target: str, start: str, end: str) -> list[dict]:
    try:
        svc = _service()
        results = []
        for cal_id in _resolve_target(target):
            events = (
                svc.events()
                .list(
                    calendarId=cal_id,
                    timeMin=start,
                    timeMax=end,
                    singleEvents=True,
                    orderBy="startTime",
                    maxResults=50,
                )
                .execute()
            )
            results.extend([_format_event(e) for e in events.get("items", [])])
        results.sort(key=lambda e: e.get("start") or "")
        return results
    except Exception as e:
        logger.warning("calendar.get_range failed: %s", e)
        return [{"error": str(e)}]


def _get_today(target: str = "personal") -> list[dict]:
    now = datetime.now(UTC)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    end = (now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)).isoformat()
    return _get_range(target, start, end)


def _get_tomorrow(target: str = "personal") -> list[dict]:
    now = datetime.now(UTC)
    tomorrow = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    start = tomorrow.isoformat()
    end = (tomorrow + timedelta(days=1)).isoformat()
    return _get_range(target, start, end)


def _search_events(target: str, query: str) -> list[dict]:
    try:
        svc = _service()
        results = []
        for cal_id in _resolve_target(target):
            events = (
                svc.events()
                .list(
                    calendarId=cal_id,
                    q=query,
                    singleEvents=True,
                    orderBy="startTime",
                    maxResults=20,
                )
                .execute()
            )
            results.extend([_format_event(e) for e in events.get("items", [])])
        return results
    except Exception as e:
        logger.warning("calendar.search_events failed: %s", e)
        return [{"error": str(e)}]


def _find_free_time(
    targets: list, duration_minutes: int, after: str | None = None, before: str | None = None
) -> list[dict]:
    try:
        svc = _service()
        ids = _parse_calendar_ids()
        cal_ids = [ids.get(t, t) for t in (targets if isinstance(targets, list) else [targets])]
        now = datetime.now(UTC)
        time_min = after or now.isoformat()
        time_max = before or (now + timedelta(days=7)).isoformat()
        body = {
            "timeMin": time_min,
            "timeMax": time_max,
            "items": [{"id": c} for c in cal_ids],
        }
        result = svc.freebusy().query(body=body).execute()
        # Return busy slots so caller can infer free time
        busy = {}
        for cal_id in cal_ids:
            busy[cal_id] = result.get("calendars", {}).get(cal_id, {}).get("busy", [])
        return [{"calendar": k, "busy_slots": v} for k, v in busy.items()]
    except Exception as e:
        logger.warning("calendar.find_free_time failed: %s", e)
        return [{"error": str(e)}]


def _create_event(target: str, title: str, start: str, end: str, description: str = "") -> dict:
    try:
        svc = _service()
        ids = _parse_calendar_ids()
        cal_id = ids.get(target, target)
        body = {
            "summary": title,
            "description": description,
            "start": {"dateTime": start},
            "end": {"dateTime": end},
        }
        event = svc.events().insert(calendarId=cal_id, body=body).execute()
        return _format_event(event)
    except Exception as e:
        logger.warning("calendar.create_event failed: %s", e)
        return {"error": str(e)}


def _delete_event(event_id: str, target: str = "personal") -> dict:
    try:
        svc = _service()
        ids = _parse_calendar_ids()
        cal_id = ids.get(target, target)
        svc.events().delete(calendarId=cal_id, eventId=event_id).execute()
        return {"ok": True, "deleted": event_id}
    except Exception as e:
        logger.warning("calendar.delete_event failed: %s", e)
        return {"ok": False, "error": str(e)}


class CalendarProvider(ToolProvider):
    namespace = "calendar"
    profile = "sensitive_personal"
    risk_level = "low"  # reads; writes escalate to medium via policy
    side_effects = "none"  # reads; writes declare calendar_write

    def is_available(self) -> bool:
        import os

        token = get_settings().google_token_json
        if not token or not os.path.exists(token):
            return False
        try:
            from google.oauth2.credentials import Credentials  # noqa: F401
            from googleapiclient.discovery import build  # noqa: F401

            return True
        except ImportError:
            return False

    def lua_functions(self) -> dict[str, HostFunctionDef]:
        _events_output = {
            "type": "object",
            "properties": {
                "events": {"type": "array", "description": "List of calendar event objects"}
            },
        }
        return {
            "calendar.get_today": HostFunctionDef(
                "calendar.get_today",
                "sensitive_personal",
                _get_today,
                description="Get today's calendar events.",
                input_schema={
                    "type": "object",
                    "properties": {"target": {"type": "string", "default": "personal"}},
                },
                output_schema=_events_output,
            ),
            "calendar.get_tomorrow": HostFunctionDef(
                "calendar.get_tomorrow",
                "sensitive_personal",
                _get_tomorrow,
                description="Get tomorrow's calendar events.",
                input_schema={
                    "type": "object",
                    "properties": {"target": {"type": "string", "default": "personal"}},
                },
                output_schema=_events_output,
            ),
            "calendar.get_range": HostFunctionDef(
                "calendar.get_range",
                "sensitive_personal",
                _get_range,
                description="Get calendar events within a date range.",
                input_schema={
                    "type": "object",
                    "required": ["start", "end"],
                    "properties": {
                        "start": {"type": "string"},
                        "end": {"type": "string"},
                        "target": {"type": "string"},
                    },
                },
                output_schema=_events_output,
            ),
            "calendar.search_events": HostFunctionDef(
                "calendar.search_events",
                "sensitive_personal",
                _search_events,
                description="Search calendar events by text query.",
                input_schema={
                    "type": "object",
                    "required": ["query"],
                    "properties": {
                        "query": {"type": "string"},
                        "max_results": {"type": "integer", "default": 10},
                    },
                },
                output_schema=_events_output,
            ),
            "calendar.find_free_time": HostFunctionDef(
                "calendar.find_free_time",
                "sensitive_personal",
                _find_free_time,
                description="Find free time slots in the calendar.",
                input_schema={
                    "type": "object",
                    "required": ["date"],
                    "properties": {
                        "date": {"type": "string"},
                        "duration_minutes": {"type": "integer", "default": 60},
                    },
                },
                output_schema={"type": "object", "properties": {"slots": {"type": "array"}}},
            ),
            "calendar.create_event": HostFunctionDef(
                "calendar.create_event",
                "sensitive_personal",
                _create_event,
                description="Create a new calendar event.",
                input_schema={
                    "type": "object",
                    "required": ["title", "start", "end"],
                    "properties": {
                        "title": {"type": "string"},
                        "start": {"type": "string"},
                        "end": {"type": "string"},
                        "description": {"type": "string"},
                    },
                },
                output_schema={
                    "type": "object",
                    "properties": {"event_id": {"type": "string"}, "ok": {"type": "boolean"}},
                },
            ),
            "calendar.delete_event": HostFunctionDef(
                "calendar.delete_event",
                "sensitive_personal",
                _delete_event,
                description="Delete a calendar event by ID.",
                input_schema={
                    "type": "object",
                    "required": ["event_id"],
                    "properties": {"event_id": {"type": "string"}},
                },
                output_schema={"type": "object", "properties": {"ok": {"type": "boolean"}}},
            ),
        }

    def mcp_tools(self) -> list[MCPToolDef]:
        import asyncio

        async def _mcp_get_today(target: str = "personal", **_) -> list[dict]:
            return await asyncio.to_thread(_get_today, target)

        async def _mcp_get_tomorrow(target: str = "personal", **_) -> list[dict]:
            return await asyncio.to_thread(_get_tomorrow, target)

        async def _mcp_get_range(target: str, start: str, end: str, **_) -> list[dict]:
            return await asyncio.to_thread(_get_range, target, start, end)

        async def _mcp_search(target: str, query: str, **_) -> list[dict]:
            return await asyncio.to_thread(_search_events, target, query)

        async def _mcp_free_time(
            targets: list,
            duration_minutes: int,
            after: str | None = None,
            before: str | None = None,
            **_,
        ) -> list[dict]:
            return await asyncio.to_thread(
                _find_free_time, targets, duration_minutes, after, before
            )

        async def _mcp_create(
            target: str, title: str, start: str, end: str, description: str = "", **_
        ) -> dict:
            return await asyncio.to_thread(_create_event, target, title, start, end, description)

        async def _mcp_delete(event_id: str, target: str = "personal", **_) -> dict:
            return await asyncio.to_thread(_delete_event, event_id, target)

        _target_desc = '"personal", "family", "all", or a calendar ID'

        return [
            MCPToolDef(
                name="calendar_get_today",
                description="List today's calendar events.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "target": {
                            "type": "string",
                            "default": "personal",
                            "description": _target_desc,
                        }
                    },
                },
                fn=_mcp_get_today,
            ),
            MCPToolDef(
                name="calendar_get_tomorrow",
                description="List tomorrow's calendar events.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "target": {
                            "type": "string",
                            "default": "personal",
                            "description": _target_desc,
                        }
                    },
                },
                fn=_mcp_get_tomorrow,
            ),
            MCPToolDef(
                name="calendar_get_range",
                description="List calendar events in a date/time range (ISO 8601 strings).",
                input_schema={
                    "type": "object",
                    "required": ["target", "start", "end"],
                    "properties": {
                        "target": {"type": "string"},
                        "start": {"type": "string", "description": "ISO 8601 datetime"},
                        "end": {"type": "string", "description": "ISO 8601 datetime"},
                    },
                },
                fn=_mcp_get_range,
            ),
            MCPToolDef(
                name="calendar_search_events",
                description="Search calendar events by text query.",
                input_schema={
                    "type": "object",
                    "required": ["target", "query"],
                    "properties": {"target": {"type": "string"}, "query": {"type": "string"}},
                },
                fn=_mcp_search,
            ),
            MCPToolDef(
                name="calendar_find_free_time",
                description="Return busy slots for one or more calendars so free time can be inferred.",
                input_schema={
                    "type": "object",
                    "required": ["targets", "duration_minutes"],
                    "properties": {
                        "targets": {"type": "array", "items": {"type": "string"}},
                        "duration_minutes": {"type": "integer"},
                        "after": {"type": "string", "description": "ISO 8601 start bound"},
                        "before": {"type": "string", "description": "ISO 8601 end bound"},
                    },
                },
                fn=_mcp_free_time,
            ),
            MCPToolDef(
                name="calendar_create_event",
                description="Create a new calendar event. Requires approval for shared/family calendars.",
                input_schema={
                    "type": "object",
                    "required": ["target", "title", "start", "end"],
                    "properties": {
                        "target": {"type": "string"},
                        "title": {"type": "string"},
                        "start": {"type": "string", "description": "ISO 8601 datetime"},
                        "end": {"type": "string", "description": "ISO 8601 datetime"},
                        "description": {"type": "string", "default": ""},
                    },
                },
                fn=_mcp_create,
            ),
            MCPToolDef(
                name="calendar_delete_event",
                description="Delete a calendar event by ID. Irreversible — confirm before calling.",
                input_schema={
                    "type": "object",
                    "required": ["event_id"],
                    "properties": {
                        "event_id": {"type": "string"},
                        "target": {"type": "string", "default": "personal"},
                    },
                },
                fn=_mcp_delete,
            ),
        ]
