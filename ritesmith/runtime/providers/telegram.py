"""telegram.* host functions — send Telegram messages via Bot API."""

from __future__ import annotations

import logging

import httpx

from ritesmith.config import get_settings
from ritesmith.runtime.providers.base import HostFunctionDef, MCPToolDef, ToolProvider

logger = logging.getLogger(__name__)

_TIMEOUT = 8.0

_HTTP_CLIENT = httpx.Client(
    timeout=_TIMEOUT,
    limits=httpx.Limits(max_connections=5, max_keepalive_connections=2),
)


def _send(text: str, chat_id: str | None = None, parse_mode: str | None = None) -> dict:
    s = get_settings()
    token = s.telegram_bot_token
    target = chat_id or s.telegram_chat_id
    if not token or not target:
        return {"ok": False, "error": "TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not configured"}
    try:
        payload: dict = {"chat_id": target, "text": text}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        r = _HTTP_CLIENT.post(f"https://api.telegram.org/bot{token}/sendMessage", json=payload)
        data = r.json()
        return {"ok": data.get("ok", False), "message_id": data.get("result", {}).get("message_id")}
    except Exception as e:
        logger.warning("telegram.send failed: %s", e)
        return {"ok": False, "error": str(e)}


def _send_markdown(text: str, chat_id: str | None = None) -> dict:
    return _send(text, chat_id=chat_id, parse_mode="MarkdownV2")


class TelegramProvider(ToolProvider):
    namespace = "telegram"
    profile = "notification"
    risk_level = "low"
    side_effects = "notification"

    def is_available(self) -> bool:
        s = get_settings()
        return bool(s.telegram_bot_token and s.telegram_chat_id)

    def lua_functions(self) -> dict[str, HostFunctionDef]:
        _send_output = {
            "type": "object",
            "properties": {"ok": {"type": "boolean"}, "message_id": {"type": "integer"}},
        }
        return {
            "telegram.send": HostFunctionDef(
                "telegram.send",
                "notification",
                _send,
                description="Send a plain-text message to the configured Telegram chat.",
                input_schema={
                    "type": "object",
                    "required": ["text"],
                    "properties": {
                        "text": {"type": "string"},
                        "chat_id": {
                            "type": "string",
                            "description": "Override default chat (optional)",
                        },
                    },
                },
                output_schema=_send_output,
            ),
            "telegram.send_markdown": HostFunctionDef(
                "telegram.send_markdown",
                "notification",
                _send_markdown,
                description="Send a MarkdownV2-formatted message to the configured Telegram chat.",
                input_schema={
                    "type": "object",
                    "required": ["text"],
                    "properties": {
                        "text": {"type": "string", "description": "MarkdownV2 formatted text"},
                        "chat_id": {"type": "string"},
                    },
                },
                output_schema=_send_output,
            ),
        }

    def mcp_tools(self) -> list[MCPToolDef]:
        import asyncio

        async def _mcp_send(
            text: str, markdown: bool = False, chat_id: str | None = None, **_
        ) -> dict:
            fn = _send_markdown if markdown else _send
            return await asyncio.to_thread(fn, text, chat_id)

        return [
            MCPToolDef(
                name="telegram_send",
                description="Send a Telegram message to the configured chat. Set markdown=true for MarkdownV2 formatting.",
                input_schema={
                    "type": "object",
                    "required": ["text"],
                    "properties": {
                        "text": {"type": "string"},
                        "markdown": {"type": "boolean", "default": False},
                        "chat_id": {"type": "string", "description": "Override default chat ID"},
                    },
                },
                fn=_mcp_send,
            ),
        ]
