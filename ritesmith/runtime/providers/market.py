"""market.* host functions — cryptocurrency and exchange rate prices.

Uses CoinGecko public API (no key, 30 req/min free tier).
In-process rate limit guard (10 s minimum between requests per key).
"""

from __future__ import annotations

import logging
import time as _time
from datetime import UTC, datetime

import httpx

from ritesmith.runtime.providers.base import HostFunctionDef, MCPToolDef, ToolProvider

logger = logging.getLogger(__name__)

_COINGECKO = "https://api.coingecko.com/api/v3"
_TIMEOUT = 8.0
_MIN_INTERVAL = 10  # don't hit API more often than this per key

_last_fetch: dict[str, float] = {}  # key → unix timestamp

_HTTP_CLIENT = httpx.Client(
    timeout=_TIMEOUT,
    limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
)


def _fetch(cache_key: str, url: str, params: dict) -> dict:
    now = _time.time()
    last = _last_fetch.get(cache_key, 0)
    if now - last < _MIN_INTERVAL:
        return {"error": "rate_limited", "message": "Too many requests — wait a moment"}
    try:
        r = _HTTP_CLIENT.get(url, params=params, headers={"Accept": "application/json"})
        data = r.json()
        _last_fetch[cache_key] = now
        return data
    except Exception as e:
        logger.warning("market fetch failed: %s", e)
        return {"error": str(e)}


def _bitcoin_price(currency: str = "usd") -> dict:
    currency = currency.lower()
    data = _fetch(
        f"btc_{currency}",
        f"{_COINGECKO}/simple/price",
        {"ids": "bitcoin", "vs_currencies": currency, "include_24hr_change": "true"},
    )
    if "error" in data:
        return data
    btc = data.get("bitcoin", {})
    return {
        "symbol": "BTC",
        "price": btc.get(currency),
        "change_24h": btc.get(f"{currency}_24h_change"),
        "currency": currency.upper(),
        "timestamp": datetime.now(UTC).isoformat(),
    }


def _coin_price(symbol: str, currency: str = "usd") -> dict:
    _ID_MAP = {"btc": "bitcoin", "eth": "ethereum", "sol": "solana", "bnb": "binancecoin"}
    coin_id = _ID_MAP.get(symbol.lower(), symbol.lower())
    currency = currency.lower()
    data = _fetch(
        f"{coin_id}_{currency}",
        f"{_COINGECKO}/simple/price",
        {"ids": coin_id, "vs_currencies": currency, "include_24hr_change": "true"},
    )
    if "error" in data:
        return data
    info = data.get(coin_id, {})
    return {
        "symbol": symbol.upper(),
        "price": info.get(currency),
        "change_24h": info.get(f"{currency}_24h_change"),
        "currency": currency.upper(),
        "timestamp": datetime.now(UTC).isoformat(),
    }


def _exchange_rate(from_currency: str, to_currency: str) -> dict:
    data = _fetch(
        f"fx_{from_currency}_{to_currency}",
        f"{_COINGECKO}/exchange_rates",
        {},
    )
    if "error" in data:
        return data
    rates = data.get("rates", {})
    frm = rates.get(from_currency.lower(), {})
    to = rates.get(to_currency.lower(), {})
    if not frm or not to:
        return {"error": f"Unknown currency: {from_currency} or {to_currency}"}
    rate = to.get("value", 1) / frm.get("value", 1)
    return {
        "from": from_currency.upper(),
        "to": to_currency.upper(),
        "rate": round(rate, 6),
        "timestamp": datetime.now(UTC).isoformat(),
    }


class MarketProvider(ToolProvider):
    namespace = "market"
    profile = "readonly_network"
    risk_level = "low"
    side_effects = "external_read"

    def is_available(self) -> bool:
        return True  # CoinGecko free API requires no key

    def lua_functions(self) -> dict[str, HostFunctionDef]:
        _price_output = {
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "price": {"type": "number"},
                "change_24h": {"type": "number"},
                "currency": {"type": "string"},
                "timestamp": {"type": "string"},
            },
        }
        return {
            "market.bitcoin_price": HostFunctionDef(
                "market.bitcoin_price",
                "readonly_network",
                _bitcoin_price,
                description="Get the current Bitcoin price and 24h change from CoinGecko.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "currency": {"type": "string", "default": "usd"},
                    },
                },
                output_schema=_price_output,
            ),
            "market.coin_price": HostFunctionDef(
                "market.coin_price",
                "readonly_network",
                _coin_price,
                description="Get the current price of any cryptocurrency (BTC, ETH, SOL, or any CoinGecko ID).",
                input_schema={
                    "type": "object",
                    "required": ["symbol"],
                    "properties": {
                        "symbol": {"type": "string", "description": "Coin symbol or CoinGecko ID"},
                        "currency": {"type": "string", "default": "usd"},
                    },
                },
                output_schema=_price_output,
            ),
            "market.exchange_rate": HostFunctionDef(
                "market.exchange_rate",
                "readonly_network",
                _exchange_rate,
                description="Get the exchange rate between two fiat or crypto currencies via CoinGecko.",
                input_schema={
                    "type": "object",
                    "required": ["from_currency", "to_currency"],
                    "properties": {
                        "from_currency": {"type": "string", "description": "e.g. usd, brl, btc"},
                        "to_currency": {"type": "string", "description": "e.g. brl, eur, eth"},
                    },
                },
                output_schema={
                    "type": "object",
                    "properties": {
                        "from": {"type": "string"},
                        "to": {"type": "string"},
                        "rate": {"type": "number"},
                        "timestamp": {"type": "string"},
                    },
                },
            ),
        }

    def mcp_tools(self) -> list[MCPToolDef]:
        import asyncio

        async def _btc(currency: str = "usd", **_) -> dict:
            return await asyncio.to_thread(_bitcoin_price, currency)

        async def _coin(symbol: str, currency: str = "usd", **_) -> dict:
            return await asyncio.to_thread(_coin_price, symbol, currency)

        async def _fx(from_currency: str, to_currency: str, **_) -> dict:
            return await asyncio.to_thread(_exchange_rate, from_currency, to_currency)

        return [
            MCPToolDef(
                name="market_bitcoin_price",
                description="Get the current Bitcoin price and 24h change from CoinGecko.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "currency": {
                            "type": "string",
                            "default": "usd",
                            "description": "ISO 4217 currency code",
                        }
                    },
                },
                fn=_btc,
            ),
            MCPToolDef(
                name="market_coin_price",
                description="Get the current price of a cryptocurrency (BTC, ETH, SOL, BNB, or any CoinGecko ID).",
                input_schema={
                    "type": "object",
                    "required": ["symbol"],
                    "properties": {
                        "symbol": {"type": "string", "description": "Coin symbol or CoinGecko ID"},
                        "currency": {"type": "string", "default": "usd"},
                    },
                },
                fn=_coin,
            ),
            MCPToolDef(
                name="market_exchange_rate",
                description="Get the exchange rate between two fiat or crypto currencies via CoinGecko.",
                input_schema={
                    "type": "object",
                    "required": ["from_currency", "to_currency"],
                    "properties": {
                        "from_currency": {"type": "string", "description": "e.g. usd, brl, btc"},
                        "to_currency": {"type": "string", "description": "e.g. brl, eur, eth"},
                    },
                },
                fn=_fx,
            ),
        ]
