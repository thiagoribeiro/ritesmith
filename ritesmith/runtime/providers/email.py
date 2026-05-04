"""email.* host functions — Gmail via OAuth2.

Same token.json as the calendar provider (combined scopes).
Provides read + label/archive operations; delete is intentionally excluded.
"""
from __future__ import annotations

import base64
import logging

from ritesmith.config import get_settings
from ritesmith.runtime.providers.base import HostFunctionDef, MCPToolDef, ToolProvider

logger = logging.getLogger(__name__)

_SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",  # read + labels + archive, no delete
]


def _get_credentials():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request

    token_path = get_settings().google_token_json
    if not token_path:
        raise RuntimeError("RITESMITH_GOOGLE_TOKEN_JSON not configured")

    creds = Credentials.from_authorized_user_file(token_path)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        import json, os
        with open(token_path, "w") as f:
            f.write(creds.to_json())
    return creds


def _service():
    from googleapiclient.discovery import build
    return build("gmail", "v1", credentials=_get_credentials(), cache_discovery=False)


def _decode_body(payload: dict) -> str:
    """Extract plain text from Gmail message payload."""
    mime = payload.get("mimeType", "")
    body_data = payload.get("body", {}).get("data", "")

    if mime == "text/plain" and body_data:
        return base64.urlsafe_b64decode(body_data + "==").decode("utf-8", errors="replace")

    for part in payload.get("parts", []):
        text = _decode_body(part)
        if text:
            return text
    return ""


def _format_message(msg: dict, full: bool = False) -> dict:
    headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
    result: dict = {
        "id": msg.get("id"),
        "thread_id": msg.get("threadId"),
        "subject": headers.get("subject"),
        "from": headers.get("from"),
        "to": headers.get("to"),
        "date": headers.get("date"),
        "snippet": msg.get("snippet"),
        "labels": msg.get("labelIds", []),
    }
    if full:
        result["body"] = _decode_body(msg.get("payload", {}))
    return result


def _search(query: str, max_results: int = 10) -> list[dict]:
    try:
        svc = _service()
        resp = svc.users().messages().list(userId="me", q=query, maxResults=min(max_results, 50)).execute()
        messages = resp.get("messages", [])
        results = []
        for m in messages:
            msg = svc.users().messages().get(userId="me", id=m["id"], format="metadata",
                                              metadataHeaders=["Subject", "From", "To", "Date"]).execute()
            results.append(_format_message(msg))
        return results
    except Exception as e:
        logger.warning("email.search failed: %s", e)
        return [{"error": str(e)}]


def _read_message(message_id: str) -> dict:
    try:
        svc = _service()
        msg = svc.users().messages().get(userId="me", id=message_id, format="full").execute()
        return _format_message(msg, full=True)
    except Exception as e:
        logger.warning("email.read_message failed: %s", e)
        return {"error": str(e)}


def _read_thread(thread_id: str) -> list[dict]:
    try:
        svc = _service()
        thread = svc.users().threads().get(userId="me", id=thread_id, format="full").execute()
        return [_format_message(m, full=True) for m in thread.get("messages", [])]
    except Exception as e:
        logger.warning("email.read_thread failed: %s", e)
        return [{"error": str(e)}]


def _list_recent(max_results: int = 10) -> list[dict]:
    return _search("in:inbox", max_results=max_results)


def _apply_label(message_id: str, label: str) -> dict:
    try:
        svc = _service()
        svc.users().messages().modify(
            userId="me", id=message_id,
            body={"addLabelIds": [label]},
        ).execute()
        return {"ok": True}
    except Exception as e:
        logger.warning("email.apply_label failed: %s", e)
        return {"ok": False, "error": str(e)}


def _archive(message_id: str) -> dict:
    try:
        svc = _service()
        svc.users().messages().modify(
            userId="me", id=message_id,
            body={"removeLabelIds": ["INBOX"]},
        ).execute()
        return {"ok": True}
    except Exception as e:
        logger.warning("email.archive failed: %s", e)
        return {"ok": False, "error": str(e)}


def _mark_read(message_id: str) -> dict:
    try:
        svc = _service()
        svc.users().messages().modify(
            userId="me", id=message_id,
            body={"removeLabelIds": ["UNREAD"]},
        ).execute()
        return {"ok": True}
    except Exception as e:
        logger.warning("email.mark_read failed: %s", e)
        return {"ok": False, "error": str(e)}


class EmailProvider(ToolProvider):
    namespace = "email"
    profile = "sensitive_personal"
    risk_level = "low"  # reads; label/archive = medium via policy
    side_effects = "none"  # reads; writes declare email_write

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
        _msg_list_output = {"type": "object", "properties": {"messages": {"type": "array"}}}
        _ok_output = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
        return {
            "email.search": HostFunctionDef("email.search", "sensitive_personal", _search,
                description="Search Gmail messages by query string. Returns matching message summaries.",
                input_schema={"type": "object", "required": ["query"], "properties": {"query": {"type": "string"}, "max_results": {"type": "integer", "default": 10}}},
                output_schema=_msg_list_output),
            "email.read_message": HostFunctionDef("email.read_message", "sensitive_personal", _read_message,
                description="Read the full content of a Gmail message by ID.",
                input_schema={"type": "object", "required": ["message_id"], "properties": {"message_id": {"type": "string"}}},
                output_schema={"type": "object", "properties": {"subject": {"type": "string"}, "body": {"type": "string"}, "from": {"type": "string"}}}),
            "email.read_thread": HostFunctionDef("email.read_thread", "sensitive_personal", _read_thread,
                description="Read all messages in a Gmail thread.",
                input_schema={"type": "object", "required": ["thread_id"], "properties": {"thread_id": {"type": "string"}}},
                output_schema=_msg_list_output),
            "email.list_recent": HostFunctionDef("email.list_recent", "sensitive_personal", _list_recent,
                description="List recent inbox messages.",
                input_schema={"type": "object", "properties": {"max_results": {"type": "integer", "default": 10}}},
                output_schema=_msg_list_output),
            "email.apply_label": HostFunctionDef("email.apply_label", "sensitive_personal", _apply_label,
                description="Apply a Gmail label to a message.",
                input_schema={"type": "object", "required": ["message_id", "label"], "properties": {"message_id": {"type": "string"}, "label": {"type": "string"}}},
                output_schema=_ok_output),
            "email.archive": HostFunctionDef("email.archive", "sensitive_personal", _archive,
                description="Archive a Gmail message (remove from inbox).",
                input_schema={"type": "object", "required": ["message_id"], "properties": {"message_id": {"type": "string"}}},
                output_schema=_ok_output),
            "email.mark_read": HostFunctionDef("email.mark_read", "sensitive_personal", _mark_read,
                description="Mark a Gmail message as read.",
                input_schema={"type": "object", "required": ["message_id"], "properties": {"message_id": {"type": "string"}}},
                output_schema=_ok_output),
        }

    def mcp_tools(self) -> list[MCPToolDef]:
        import asyncio

        async def _mcp_search(query: str, max_results: int = 10, **_) -> list[dict]:
            return await asyncio.to_thread(_search, query, max_results)

        async def _mcp_read(message_id: str, **_) -> dict:
            return await asyncio.to_thread(_read_message, message_id)

        async def _mcp_thread(thread_id: str, **_) -> list[dict]:
            return await asyncio.to_thread(_read_thread, thread_id)

        async def _mcp_recent(max_results: int = 10, **_) -> list[dict]:
            return await asyncio.to_thread(_list_recent, max_results)

        async def _mcp_label(message_id: str, label: str, **_) -> dict:
            return await asyncio.to_thread(_apply_label, message_id, label)

        async def _mcp_archive(message_id: str, **_) -> dict:
            return await asyncio.to_thread(_archive, message_id)

        async def _mcp_mark_read(message_id: str, **_) -> dict:
            return await asyncio.to_thread(_mark_read, message_id)

        return [
            MCPToolDef(
                name="email_search",
                description="Search Gmail using Gmail search syntax (e.g. 'from:boss@example.com subject:invoice').",
                input_schema={
                    "type": "object",
                    "required": ["query"],
                    "properties": {
                        "query": {"type": "string"},
                        "max_results": {"type": "integer", "default": 10, "maximum": 50},
                    },
                },
                fn=_mcp_search,
            ),
            MCPToolDef(
                name="email_read_message",
                description="Read the full content (headers + body) of a Gmail message by ID.",
                input_schema={
                    "type": "object",
                    "required": ["message_id"],
                    "properties": {"message_id": {"type": "string"}},
                },
                fn=_mcp_read,
            ),
            MCPToolDef(
                name="email_read_thread",
                description="Read all messages in a Gmail thread.",
                input_schema={
                    "type": "object",
                    "required": ["thread_id"],
                    "properties": {"thread_id": {"type": "string"}},
                },
                fn=_mcp_thread,
            ),
            MCPToolDef(
                name="email_list_recent",
                description="List recent inbox messages.",
                input_schema={
                    "type": "object",
                    "properties": {"max_results": {"type": "integer", "default": 10, "maximum": 50}},
                },
                fn=_mcp_recent,
            ),
            MCPToolDef(
                name="email_apply_label",
                description="Add a label to a Gmail message.",
                input_schema={
                    "type": "object",
                    "required": ["message_id", "label"],
                    "properties": {
                        "message_id": {"type": "string"},
                        "label": {"type": "string", "description": "Gmail label ID or name"},
                    },
                },
                fn=_mcp_label,
            ),
            MCPToolDef(
                name="email_archive",
                description="Archive a Gmail message (removes from inbox, keeps the message).",
                input_schema={
                    "type": "object",
                    "required": ["message_id"],
                    "properties": {"message_id": {"type": "string"}},
                },
                fn=_mcp_archive,
            ),
            MCPToolDef(
                name="email_mark_read",
                description="Mark a Gmail message as read.",
                input_schema={
                    "type": "object",
                    "required": ["message_id"],
                    "properties": {"message_id": {"type": "string"}},
                },
                fn=_mcp_mark_read,
            ),
        ]
